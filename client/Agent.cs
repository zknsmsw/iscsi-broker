// iscsi-broker-agent —— 无盘 Windows 客户机的常驻客户端。
//
// 功能：
//   1) 控制通道：每隔几秒向服务器 /agent/poll 心跳，取回并执行指令（关机 / 重启 /
//      启停 VNC / 挂载卸载网盘），执行结果 POST /agent/result 回报；
//   2) 网盘挂载：把服务器个人网盘通过 WebDAV 映射成盘符（自动配好 Windows
//      WebDAV 重定向器需要的注册表与 WebClient 服务，并把 net use 跑在交互用户
//      会话里，这样“我的电脑”里能看到盘）；登录会话变化后自动重新挂载；
//   3) VNC：按 agent.ini 拉起 / 停止 VNC 服务端（推荐装成服务，可看登录界面）。
//
// 编译：client\build.bat（用系统自带 csc.exe，不需要装 SDK；产物是单个小 exe，
//       目标机不需要装 .NET 运行时——Win10/11 自带 .NET Framework 4.x）。
//
// 用法：
//   iscsi-broker-agent.exe install     安装成“开机以 SYSTEM 运行”的计划任务并立即启动
//   iscsi-broker-agent.exe uninstall   删除计划任务
//   iscsi-broker-agent.exe run         前台运行（调试用）
//   iscsi-broker-agent.exe once        只跑一轮心跳（排错用）
//   iscsi-broker-agent.exe test        打印读到的配置、MAC、VNC 状态后退出
//   加 --dry-run 只打印将要执行的指令，不真的关机/重启/映射盘

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace IscsiBrokerAgent
{
    /// <summary>极简 INI 读取（[section] + key=value，; 或 # 开头为注释）。</summary>
    internal class Ini
    {
        private readonly Dictionary<string, Dictionary<string, string>> _data =
            new Dictionary<string, Dictionary<string, string>>(StringComparer.OrdinalIgnoreCase);

        public Ini(string path)
        {
            string section = "";
            _data[section] = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (!File.Exists(path)) return;
            foreach (string raw in File.ReadAllLines(path))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == ';' || line[0] == '#') continue;
                if (line[0] == '[' && line[line.Length - 1] == ']')
                {
                    section = line.Substring(1, line.Length - 2).Trim();
                    if (!_data.ContainsKey(section))
                        _data[section] = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                    continue;
                }
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                _data[section][line.Substring(0, eq).Trim()] = line.Substring(eq + 1).Trim();
            }
        }

        public string Get(string section, string key, string def)
        {
            Dictionary<string, string> sec;
            if (_data.TryGetValue(section, out sec))
            {
                string v;
                if (sec.TryGetValue(key, out v) && v.Length > 0) return v;
            }
            return def;
        }
    }

    internal static class Program
    {
        private const string VERSION = "1.0";
        private const string TASK_NAME = "iSCSI-Broker-Agent";
        private const uint INVALID_SESSION = 0xFFFFFFFF;
        private const int MAXIMUM_ALLOWED = 0x02000000;
        private const int SecurityImpersonation = 2;
        private const int TokenPrimary = 1;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint CREATE_NO_WINDOW = 0x08000000;

        private static string _exeDir;
        private static string _iniPath;
        private static string _statePath;
        private static string _logPath;
        private static Ini _ini;
        private static string _url = "";
        private static string _token = "";
        private static string _mac = "";
        private static string _letter = "Z";
        private static string _vncExe = "";
        private static string _vncArgs = "";
        private static string _vncService = "";
        private static int _vncPort = 5900;
        private static int _interval = 3;
        private static bool _dryRun;
        private static bool _mountRequested = true;   // 默认就给客户机挂 Z 盘（只读通用文件）
        private static bool _mountOk;
        private static int _trayPid;                  // SYSTEM 进程拉起的托盘进程（用户会话里）
        private static uint _traySession = INVALID_SESSION;
        // ---- 以下只在托盘进程里用 ----
        private static string _davSession = "";       // 登录换来的网盘会话令牌（只在内存里）
        private static string _davUser = "";          // 当前登录的网盘账号
        private static string _lastAccount = "";      // 服务器给的“默认用户名”，用于预填登录框
        private static NotifyIcon _trayIcon;
        private static ToolStripMenuItem _trayStatus;
        private static ToolStripMenuItem _trayLogin;
        private static ToolStripMenuItem _trayLogout;
        private static readonly object _logLock = new object();

        // ---------------- 入口 ----------------
        [STAThread]
        private static int Main(string[] args)
        {
            _exeDir = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
            _iniPath = Path.Combine(_exeDir, "agent.ini");
            _statePath = Path.Combine(_exeDir, "agent.state");
            _logPath = Path.Combine(_exeDir, "agent.log");

            string action = "run";
            foreach (string a in args)
            {
                string low = a.Trim().ToLowerInvariant();
                if (low == "--dry-run" || low == "dry-run" || low == "dryrun") _dryRun = true;
                else if (low == "install" || low == "uninstall" || low == "run" || low == "tray"
                         || low == "once" || low == "test" || low == "selftest") action = low;
            }

            if (action == "install") return Install();
            if (action == "uninstall") return Uninstall();
            if (action == "selftest") return SelfTest(args);

            LoadConfig();
            if (action == "tray") return TrayMain();
            if (action == "test")
            {
                Console.WriteLine("ini      : " + _iniPath);
                Console.WriteLine("server   : " + _url);
                Console.WriteLine("token    : " + (_token.Length > 8 ? _token.Substring(0, 8) + "..." : _token));
                Console.WriteLine("mac      : " + _mac);
                Console.WriteLine("interval : " + _interval + "s");
                Console.WriteLine("vnc      : " + (_vncExe.Length > 0 ? _vncExe : "(未配置)")
                                  + " running=" + VncRunning());
                Console.WriteLine("drive    : " + _letter + ": mounted=" + _mountOk
                                  + " requested=" + _mountRequested);
                Console.WriteLine("icon     : " + DescribeIcon());
                return 0;
            }

            // 只允许一个实例（避免双份心跳和重复执行指令）
            bool created;
            using (var mutex = new Mutex(true, "Global\\iscsi-broker-agent", out created))
            {
                if (!created)
                {
                    Log("另一个实例已在运行，本实例退出");
                    return 1;
                }
                if (action == "once") { PollOnce(); return 0; }
                Log("=== 启动 v" + VERSION + " mac=" + _mac + " server=" + _url
                    + (_dryRun ? " (dry-run)" : "") + " ===");
                PrepareWebClient();     // SYSTEM 身份：配好 WebDAV 重定向器（托盘是普通用户改不了 HKLM）
                EnsureTray();
                while (true)
                {
                    PollOnce();
                    Thread.Sleep(Math.Max(1, _interval) * 1000);
                }
            }
        }

        private static void LoadConfig()
        {
            _ini = new Ini(_iniPath);
            _url = _ini.Get("server", "url", "").TrimEnd('/');
            _token = _ini.Get("server", "token", "");
            _interval = ParseInt(_ini.Get("server", "interval", "3"), 3);
            _letter = _ini.Get("dav", "letter", "Z").TrimEnd(':');
            if (_letter.Length == 0) _letter = "Z";
            _vncExe = _ini.Get("vnc", "exe", "");
            _vncArgs = _ini.Get("vnc", "args", "");
            _vncService = _ini.Get("vnc", "service", "");
            _vncPort = ParseInt(_ini.Get("vnc", "port", "5900"), 5900);
            string macOverride = _ini.Get("server", "mac", "");
            _mac = macOverride.Length > 0 ? NormalizeMac(macOverride) : DetectMac();
            LoadState();
        }

        private static int ParseInt(string s, int def)
        {
            int v;
            return int.TryParse(s, out v) ? v : def;
        }

        // ---------------- 状态持久化（挂载请求跨重启保留） ----------------
        private static void LoadState()
        {
            try
            {
                if (!File.Exists(_statePath)) return;
                foreach (string line in File.ReadAllLines(_statePath))
                {
                    string[] kv = line.Split(new char[] { '=' }, 2);
                    if (kv.Length != 2) continue;
                    string k = kv[0].Trim().ToLowerInvariant(), v = kv[1].Trim();
                    if (k == "mount") _mountRequested = (v == "1");
                    else if (k == "mount_ok") _mountOk = (v == "1");
                }
            }
            catch (Exception e) { Log("读取 agent.state 失败：" + e.Message); }
        }

        private static void SaveState()
        {
            try
            {
                File.WriteAllText(_statePath,
                    "mount=" + (_mountRequested ? "1" : "0") + "\r\n" +
                    "mount_ok=" + (_mountOk ? "1" : "0") + "\r\n");
            }
            catch (Exception e) { Log("写 agent.state 失败：" + e.Message); }
        }

        // ---------------- 日志 ----------------
        private static void Log(string msg)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg;
            Console.WriteLine(line);
            try
            {
                lock (_logLock)
                {
                    if (File.Exists(_logPath) && new FileInfo(_logPath).Length > 1024 * 1024)
                        File.Delete(_logPath + ".old");   // 保留一份旧日志
                    File.AppendAllText(_logPath, line + "\r\n", Encoding.UTF8);
                }
            }
            catch { /* 日志写不了不影响主流程 */ }
        }

        // ---------------- MAC 探测 ----------------
        private static string NormalizeMac(string mac)
        {
            StringBuilder sb = new StringBuilder();
            foreach (char c in mac.ToLowerInvariant())
                if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) sb.Append(c);
            return sb.ToString();
        }

        /// <summary>取“默认网关所在网卡”的 MAC（无网关则取第一个已启用物理网卡）。</summary>
        private static string DetectMac()
        {
            try
            {
                string fallback = "";
                foreach (NetworkInterface ni in NetworkInterface.GetAllNetworkInterfaces())
                {
                    if (ni.NetworkInterfaceType == NetworkInterfaceType.Loopback
                        || ni.NetworkInterfaceType == NetworkInterfaceType.Tunnel) continue;
                    if (ni.OperationalStatus != OperationalStatus.Up) continue;
                    string mac = NormalizeMac(ni.GetPhysicalAddress().ToString());
                    if (mac.Length != 12) continue;
                    if (ni.NetworkInterfaceType.ToString().IndexOf("Wireless", StringComparison.OrdinalIgnoreCase) >= 0
                        && fallback.Length == 0) fallback = mac;
                    bool hasGw = false;
                    try
                    {
                        foreach (GatewayIPAddressInformation g in ni.GetIPProperties().GatewayAddresses)
                            if (g.Address != null && g.Address.ToString() != "0.0.0.0") { hasGw = true; break; }
                    }
                    catch { }
                    if (hasGw) return mac;
                    if (fallback.Length == 0) fallback = mac;
                }
                return fallback;
            }
            catch (Exception e)
            {
                Log("探测 MAC 失败：" + e.Message);
                return "";
            }
        }

        // ---------------- 与服务器通信 ----------------
        private static Dictionary<string, object> PostJson(string path, Dictionary<string, object> payload)
        {
            var js = new JavaScriptSerializer();
            byte[] data = Encoding.UTF8.GetBytes(js.Serialize(payload));
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(_url + path);
            req.Method = "POST";
            req.ContentType = "application/json; charset=utf-8";
            req.ContentLength = data.Length;
            req.Timeout = 10000;
            req.ReadWriteTimeout = 15000;
            req.Proxy = null;              // 内网直连，不走系统代理
            req.KeepAlive = false;
            using (Stream s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (Stream rs = resp.GetResponseStream())
            using (StreamReader sr = new StreamReader(rs, Encoding.UTF8))
            {
                string text = sr.ReadToEnd();
                return (Dictionary<string, object>)js.DeserializeObject(text);
            }
        }

        private static string GetString(Dictionary<string, object> d, string key)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v) && v != null) return v.ToString();
            return "";
        }

        private static Dictionary<string, object> GetDict(Dictionary<string, object> d, string key)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v)) return v as Dictionary<string, object>;
            return null;
        }

        // ---------------- 心跳主循环 ----------------
        private static void PollOnce()
        {
            if (_url.Length == 0 || _token.Length == 0 || _mac.Length != 12)
            {
                Log("配置不完整（[server] url/token 必须填，MAC 必须能探测到），10 秒后重试");
                Thread.Sleep(10000);
                return;
            }
            try
            {
                // 托盘（用户会话里）负责挂 Z 盘和显示图标；这里只保证它该在的时候在
                if (_mountRequested) EnsureTray();
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["hostname"] = Environment.MachineName;
                info["agent_ver"] = VERSION;
                info["vnc_running"] = VncRunning();
                info["vnc_port"] = _vncPort;
                // 注意：不报 drive/dav_user —— 那两个由托盘上报，不然会互相覆盖
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac;
                body["token"] = _token;
                body["ver"] = VERSION;
                body["info"] = info;

                Dictionary<string, object> resp = PostJson("/agent/poll", body);
                // 服务器记住“这台机器该不该挂网盘”，无盘客户机每次开机都按它对齐
                object wantObj;
                if (resp != null && resp.TryGetValue("mount", out wantObj) && wantObj is bool)
                {
                    bool want = (bool)wantObj;
                    if (want != _mountRequested) ReconcileMount(want);
                }
                Dictionary<string, object> cmd = GetDict(resp, "cmd");
                if (cmd != null) Execute(cmd);
            }
            catch (WebException we)
            {
                HttpWebResponse r = we.Response as HttpWebResponse;
                Log("心跳失败：" + we.Message + (r != null ? (" HTTP " + (int)r.StatusCode) : ""));
                if (r != null && (int)r.StatusCode == 403)
                    Log("→ 接入令牌不对：请把服务器“客户机控制”页上的令牌抄进 agent.ini");
            }
            catch (Exception e)
            {
                Log("心跳异常：" + e.Message);
            }
        }

        private static void Report(int id, bool ok, string msg)
        {
            try
            {
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac;
                body["token"] = _token;
                body["id"] = id;
                body["ok"] = ok;
                body["msg"] = msg;
                PostJson("/agent/result", body);
            }
            catch (Exception e) { Log("回报结果失败：" + e.Message); }
        }

        private static void Execute(Dictionary<string, object> cmd)
        {
            int id = ParseInt(GetString(cmd, "id"), 0);
            string type = GetString(cmd, "type");
            Log("收到指令 #" + id + " " + type);
            bool ok;
            string msg;
            if (type == "shutdown" || type == "reboot")
            {
                bool reboot = (type == "reboot");
                // 先回报再执行：关机后就没机会回报了
                Report(id, true, _dryRun ? ("[dry-run] 不执行" + (reboot ? "重启" : "关机"))
                                         : ((reboot ? "重启" : "关机") + "指令已收到，3 秒后执行"));
                if (_dryRun) { Log("[dry-run] 不执行 " + type); return; }
                msg = RunShutdown(reboot);
                Log(msg);
                return;
            }
            switch (type)
            {
                case "vnc_start": ok = VncStart(out msg); break;
                case "vnc_stop": ok = VncStop(out msg); break;
                case "mount":
                    ReconcileMount(true);
                    ok = true; msg = "已要求客户机挂载 Z 盘（托盘负责，稍后生效）"; break;
                case "unmount":
                    ReconcileMount(false);
                    ok = true; msg = "已停用 Z 盘并卸载"; break;
                default: ok = false; msg = "未知指令：" + type; break;
            }
            Log("指令 #" + id + " 结果：" + (ok ? "成功" : "失败") + " " + msg);
            Report(id, ok, msg);
        }

        // ---------------- 关机 / 重启 ----------------
        private static string RunShutdown(bool reboot)
        {
            string outText;
            RunShell("shutdown.exe " + (reboot ? "/r" : "/s") + " /t 3 /f /c \"iscsi-broker remote " +
                     (reboot ? "restart" : "shutdown") + "\"", 15000, out outText);
            return (reboot ? "重启" : "关机") + "命令已执行 " + outText;
        }

        // ---------------- 网盘挂载（Z 盘） ----------------
        // 实际映射由“用户会话里的托盘进程”负责（盘符映射是每个登录会话一份，SYSTEM 会话里做没用）；
        // 这里只负责按服务器意图把托盘拉起 / 关掉。
        private static void ReconcileMount(bool want)
        {
            _mountRequested = want;
            if (want)
            {
                EnsureTray();
            }
            else
            {
                DisableTrayAndDrive();
            }
            SaveState();
        }

        /// <summary>用户登录会话里没有托盘进程就拉起来（登录切换后重新拉起）。</summary>
        private static void EnsureTray()
        {
            uint sid = WTSGetActiveConsoleSessionId();
            if (sid == INVALID_SESSION)
                return;                              // 没人登录：等用户登录后下一轮再说
            if (_trayPid != 0 && _traySession == sid && ProcessAlive(_trayPid))
                return;                              // 已经在跑
            if (_trayPid != 0)
                TryKill(_trayPid);                   // 会话切换：干掉旧会话里的托盘
            string err;
            int pid = LaunchInSession(sid, Process.GetCurrentProcess().MainModule.FileName, "tray", out err);
            if (pid > 0)
            {
                _trayPid = pid;
                _traySession = sid;
                Log("已在会话 " + sid + " 启动托盘 pid=" + pid);
            }
            else
            {
                Log("启动托盘失败：" + err);
            }
        }

        /// <summary>停用网盘：关掉托盘并卸掉该会话里的盘符。</summary>
        private static void DisableTrayAndDrive()
        {
            if (_trayPid != 0)
            {
                TryKill(_trayPid);
                _trayPid = 0;
                _traySession = INVALID_SESSION;
            }
            uint sid = WTSGetActiveConsoleSessionId();
            if (sid == INVALID_SESSION)
                return;
            if (_dryRun)
            {
                Log("[dry-run] net use " + _letter + ": /delete /y");
                return;
            }
            int rc;
            string outp;
            RunInSession(sid, "net.exe use " + _letter + ": /delete /y", 20000, out rc, out outp);
            Log("停用网盘：net use /delete rc=" + rc + " " + outp);
        }

        private static bool ProcessAlive(int pid)
        {
            try
            {
                using (Process p = Process.GetProcessById(pid))
                    return !p.HasExited;
            }
            catch
            {
                return false;
            }
        }

        private static void TryKill(int pid)
        {
            try
            {
                using (Process p = Process.GetProcessById(pid))
                    p.Kill();
            }
            catch { /* 已经不在了 */ }
        }

        /// <summary>在指定会话的登录用户身份下启动一个进程（不等待），返回 pid。</summary>
        private static int LaunchInSession(uint sessionId, string exe, string arguments, out string err)
        {
            err = "";
            IntPtr hToken = IntPtr.Zero, hDup = IntPtr.Zero;
            try
            {
                if (!WTSQueryUserToken(sessionId, out hToken))
                {
                    err = "WTSQueryUserToken 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return 0;
                }
                if (!DuplicateTokenEx(hToken, MAXIMUM_ALLOWED, IntPtr.Zero, SecurityImpersonation,
                                      TokenPrimary, out hDup))
                {
                    err = "DuplicateTokenEx 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return 0;
                }
                STARTUPINFO si = new STARTUPINFO();
                si.cb = Marshal.SizeOf(si);
                si.lpDesktop = "winsta0\\default";
                PROCESS_INFORMATION pi;
                string cmd = "\"" + exe + "\"" + (arguments.Length > 0 ? " " + arguments : "");
                if (!CreateProcessAsUser(hDup, null, cmd, IntPtr.Zero, IntPtr.Zero, false,
                                         CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                                         IntPtr.Zero, null, ref si, out pi))
                {
                    err = "CreateProcessAsUser 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return 0;
                }
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                return pi.dwProcessId;
            }
            catch (Exception e)
            {
                err = e.Message;
                return 0;
            }
            finally
            {
                if (hDup != IntPtr.Zero) CloseHandle(hDup);
                if (hToken != IntPtr.Zero) CloseHandle(hToken);
            }
        }

        /// <summary>
        /// Windows 自带的 WebDAV 重定向器默认不允许明文 http 上用 Basic 认证、
        /// 还有 50MB 文件大小上限；SYSTEM 身份的进程启动时改好注册表并拉起 WebClient 服务
        /// （托盘是普通用户身份，改不了 HKLM）。
        /// </summary>
        private static void PrepareWebClient()
        {
            try
            {
                using (Microsoft.Win32.RegistryKey k = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
                    @"SYSTEM\CurrentControlSet\Services\WebClient\Parameters", true))
                {
                    if (k != null)
                    {
                        k.SetValue("BasicAuthLevel", 2, Microsoft.Win32.RegistryValueKind.DWord);
                        k.SetValue("FileSizeLimitInBytes", unchecked((int)0xFFFFFFFF),
                                   Microsoft.Win32.RegistryValueKind.DWord);
                        string host = _url;
                        int i = host.IndexOf("://");
                        if (i > 0) host = host.Substring(i + 3);
                        // 只放行本服务器（不要用 "*"：那等于让重定向器把凭据发给任何服务器）
                        k.SetValue("AuthForwardServerList", new string[] { host },
                                   Microsoft.Win32.RegistryValueKind.MultiString);
                    }
                }
                string outp;
                // 设成自动启动：Windows 默认是"手动"，开机后 WebClient 一直是 STOPPED，
                // 这时 net use http://… 就报“系统错误 67 找不到网络名”，Z 盘自然挂不上。
                int rc1 = RunShell("sc config WebClient start= auto", 10000, out outp);
                Log("WebClient: sc config start=auto rc=" + rc1 + " " + outp.Replace("\r\n", " "));
                string state;
                if (WebClientRunning(out state))
                {
                    Log("WebClient: 服务在运行，WebDAV 盘可以挂载");
                }
                else
                {
                    int rc2 = RunShell("net start WebClient", 20000, out outp);
                    Log("WebClient: net start rc=" + rc2 + " " + outp.Replace("\r\n", " "));
                    if (!WebClientRunning(out state))
                        Log("WebClient: 服务仍不是 RUNNING —— Z 盘会挂不上（net use 报“找不到网络名”）。"
                            + "请以管理员在客户机上执行 net start WebClient 排查：" + state.Replace("\r\n", " "));
                }
            }
            catch (Exception e)
            {
                Log("配置 WebClient 失败（可能不影响已配好的机器）：" + e.Message);
                Log("→ 这一般是因为进程不是 SYSTEM 身份（手动双击运行就是这样）："
                    + "写 HKLM 和拉起用户会话托盘都需要 SYSTEM，请用 install 装的计划任务启动。");
            }
        }

        /// <summary>Windows 的 WebDAV 重定向器（WebClient 服务）是否在运行。挂 Z 盘前必须为真。</summary>
        private static bool WebClientRunning(out string info)
        {
            string outp;
            int rc = RunShell("sc query WebClient", 10000, out outp);
            info = outp;
            return rc == 0 && outp.IndexOf("RUNNING", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        // ---------------- VNC ----------------
        private static bool VncRunning()
        {
            try
            {
                if (_vncService.Length > 0)
                {
                    string outp;
                    RunShell("sc query \"" + _vncService + "\"", 8000, out outp);
                    return outp.IndexOf("RUNNING", StringComparison.OrdinalIgnoreCase) >= 0;
                }
                if (_vncExe.Length == 0) return false;
                string name = Path.GetFileNameWithoutExtension(_vncExe);
                return Process.GetProcessesByName(name).Length > 0;
            }
            catch { return false; }
        }

        private static bool VncStart(out string msg)
        {
            msg = "";
            if (_vncExe.Length == 0 && _vncService.Length == 0)
            {
                msg = "agent.ini 里没配 [vnc] exe/service，母盘请先装 VNC 服务端";
                return false;
            }
            if (VncRunning()) { msg = "VNC 已在运行"; return true; }
            if (_dryRun) { msg = "[dry-run] 启动 VNC"; return true; }
            try
            {
                if (_vncService.Length > 0)
                {
                    string outp;
                    int rc = RunShell("net start \"" + _vncService + "\"", 20000, out outp);
                    msg = outp;
                    return rc == 0;
                }
                ProcessStartInfo psi = new ProcessStartInfo(_vncExe, _vncArgs);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WorkingDirectory = Path.GetDirectoryName(_vncExe);
                Process.Start(psi);
                Thread.Sleep(1500);
                if (VncRunning()) { msg = "VNC 已启动（端口 " + _vncPort + "）"; return true; }
                msg = "VNC 进程没起来，检查 [vnc] exe/args 是否正确";
                return false;
            }
            catch (Exception e)
            {
                msg = "启动 VNC 失败：" + e.Message;
                return false;
            }
        }

        private static bool VncStop(out string msg)
        {
            msg = "";
            if (_dryRun) { msg = "[dry-run] 停止 VNC"; return true; }
            try
            {
                if (_vncService.Length > 0)
                {
                    string outp;
                    int rc = RunShell("net stop \"" + _vncService + "\"", 20000, out outp);
                    msg = outp;
                    return rc == 0;
                }
                if (_vncExe.Length == 0) { msg = "agent.ini 未配置 VNC"; return false; }
                string name = Path.GetFileNameWithoutExtension(_vncExe);
                int n = 0;
                foreach (Process p in Process.GetProcessesByName(name))
                {
                    try { p.Kill(); n++; } catch { }
                }
                msg = "已结束 " + n + " 个 VNC 进程";
                return true;
            }
            catch (Exception e)
            {
                msg = "停止 VNC 失败：" + e.Message;
                return false;
            }
        }

        // ---------------- 托盘进程（跑在用户会话里：右下角图标 + 右键菜单 + 登录 + 挂 Z 盘） ----------------
        // 为什么必须是独立进程：盘符映射是“每个登录会话一份”，SYSTEM 会话里映射用户看不到；
        // 而且 SYSTEM 服务的桌面不是用户桌面，画不出托盘图标。SYSTEM 进程负责把它拉起来。
        private static int TrayMain()
        {
            bool created;
            using (var mutex = new Mutex(true, "iscsi-broker-agent-tray", out created))
            {
                if (!created) return 0;                  // 本会话已有托盘在跑
                try { FreeConsole(); } catch { }         // 从控制台/命令行启动时收掉黑窗口
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);

                ContextMenuStrip menu = new ContextMenuStrip();
                _trayStatus = new ToolStripMenuItem("未登录（Z 盘只读通用文件）");
                _trayStatus.Enabled = false;
                menu.Items.Add(_trayStatus);
                menu.Items.Add(new ToolStripSeparator());
                _trayLogin = new ToolStripMenuItem("登录网盘…", null, delegate { ShowLoginDialog(); });
                _trayLogout = new ToolStripMenuItem("注销网盘", null, delegate { DoLogout(); });
                menu.Items.Add(_trayLogin);
                menu.Items.Add(_trayLogout);
                menu.Items.Add(new ToolStripMenuItem("打开 Z 盘", null, delegate { OpenDrive(); }));
                menu.Items.Add(new ToolStripMenuItem("重新挂载", null, delegate { Remount(); }));
                menu.Items.Add(new ToolStripMenuItem("查看日志", null, delegate { OpenLog(); }));
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add(new ToolStripMenuItem("退出托盘", null, delegate { ExitTray(); }));
                menu.Opening += delegate { UpdateTrayMenu(); };

                _trayIcon = new NotifyIcon();
                _trayIcon.Icon = LoadTrayIcon();
                _trayIcon.Text = "iSCSI Broker 网盘";
                _trayIcon.ContextMenuStrip = menu;
                _trayIcon.Visible = true;
                _trayIcon.DoubleClick += delegate { OpenDrive(); };

                Log("托盘启动：mac=" + _mac + " letter=" + _letter + " icon=" + DescribeIcon());
                UpdateTrayMenu();
                Remount();                                // 开机先挂只读公共盘
                PostStatus();
                int tick = 0;
                System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
                timer.Interval = 20000;
                timer.Tick += delegate
                {
                    PostStatus();
                    tick++;
                    // 挂载失败过（WebClient 还没起来、网络还没通等）就每分钟自动重试一次
                    if (!_mountOk && (tick % 3) == 0) Remount();
                };
                timer.Start();
                Application.Run();                        // 跑消息循环，直到“退出托盘”
                _trayIcon.Visible = false;
            }
            return 0;
        }

        private static void UpdateTrayMenu()
        {
            if (_trayIcon == null || _trayStatus == null) return;
            bool logged = _davUser.Length > 0;
            _trayStatus.Text = logged ? ("已登录：" + _davUser + "（Z 盘可写）")
                                      : "未登录（Z 盘只读通用文件）";
            _trayLogin.Enabled = !logged;
            _trayLogout.Enabled = logged;
            string tip = "iSCSI Broker 网盘\r\n"
                + (logged ? ("已登录 " + _davUser + "，Z 盘可写") : "未登录，Z 盘为只读通用文件");
            try { _trayIcon.Text = tip.Length > 63 ? tip.Substring(0, 63) : tip; } catch { }
        }

        private static void NotifyTip(string msg)
        {
            Log("[托盘] " + msg);
            try
            {
                if (_trayIcon != null) _trayIcon.ShowBalloonTip(4000, "iSCSI Broker 网盘", msg, ToolTipIcon.Info);
            }
            catch { }
        }

        /// <summary>挂 Z 盘：未登录用 MAC+接入令牌（只读公共盘），登录后用账号+会话令牌（个人网盘）。</summary>
        private static bool MapDrive(string user, string pass, out string msg)
        {
            string url = _url + "/dav/";
            msg = "";
            if (_dryRun)
            {
                msg = "[dry-run] net use " + _letter + ": " + url + " /user:" + user;
                _mountOk = true;
                return true;
            }
            string outp;
            RunShell("net use " + _letter + ": /delete /y", 20000, out outp);   // 先卸掉旧的
            int rc = RunShell("net use " + _letter + ": \"" + url + "\" /user:" + user + " " + pass
                              + " /persistent:no", 30000, out outp);
            _mountOk = (rc == 0);
            if (rc == 0) { msg = "已挂载 " + _letter + ": → " + url; return true; }
            msg = "net use 失败(rc=" + rc + ")：" + outp;
            string wc;
            if (!WebClientRunning(out wc))
                msg += "  [提示] Windows 的 WebDAV 重定向器（WebClient 服务）没在运行，"
                     + "报“找不到网络名”就是这个原因；请以管理员执行：net start WebClient";
            return false;
        }

        private static void Remount()
        {
            string msg;
            if (_davUser.Length > 0 && _davSession.Length > 0)
            {
                if (MapDrive(_davUser, _davSession, out msg)) NotifyTip("Z 盘已挂载：你的网盘（可写）");
                else NotifyTip("挂载你的网盘失败：" + msg);
            }
            else
            {
                if (!MapDrive(_mac, _token, out msg)) NotifyTip("只读公共盘挂载失败：" + msg);
            }
            UpdateTrayMenu();
        }

        private static void ShowLoginDialog()
        {
            Form f = new Form();
            f.Text = "登录网盘";
            f.FormBorderStyle = FormBorderStyle.FixedDialog;
            f.StartPosition = FormStartPosition.CenterScreen;
            f.ClientSize = new Size(330, 150);
            f.MaximizeBox = false;
            f.MinimizeBox = false;
            Label lu = new Label(); lu.Text = "用户名："; lu.SetBounds(16, 18, 70, 22);
            TextBox tu = new TextBox(); tu.SetBounds(92, 16, 220, 24); tu.Text = _lastAccount;
            Label lp = new Label(); lp.Text = "密码："; lp.SetBounds(16, 54, 70, 22);
            TextBox tp = new TextBox(); tp.SetBounds(92, 52, 220, 24); tp.UseSystemPasswordChar = true;
            Button ok = new Button(); ok.Text = "登录"; ok.SetBounds(140, 96, 80, 28); ok.DialogResult = DialogResult.OK;
            Button cancel = new Button(); cancel.Text = "取消"; cancel.SetBounds(232, 96, 80, 28); cancel.DialogResult = DialogResult.Cancel;
            f.Controls.Add(lu); f.Controls.Add(tu); f.Controls.Add(lp); f.Controls.Add(tp);
            f.Controls.Add(ok); f.Controls.Add(cancel);
            f.AcceptButton = ok;
            f.CancelButton = cancel;
            if (f.ShowDialog() != DialogResult.OK) return;
            if (tu.Text.Trim().Length == 0) { NotifyTip("用户名不能为空"); return; }
            DoLogin(tu.Text.Trim(), tp.Text);
        }

        private static void DoLogin(string user, string pwd)
        {
            try
            {
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac;
                body["token"] = _token;
                body["user"] = user;
                body["pwd"] = pwd;
                body["letter"] = _letter;
                Dictionary<string, object> resp = PostJson("/agent/login", body);
                if (GetString(resp, "ok") != "True")
                {
                    NotifyTip("登录失败：" + GetString(resp, "err"));
                    return;
                }
                _davSession = GetString(resp, "token");
                _davUser = GetString(resp, "user");
                string msg;
                if (MapDrive(_davUser, _davSession, out msg))
                    NotifyTip("已登录 " + _davUser + "，Z 盘现在是你的网盘（通用文件仍只读）");
                else
                    NotifyTip("登录成功，但挂载失败：" + msg);
            }
            catch (Exception e)
            {
                NotifyTip("登录失败：" + e.Message);
            }
            UpdateTrayMenu();
            PostStatus();
        }

        private static void DoLogout()
        {
            try
            {
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac;
                body["token"] = _token;
                body["session"] = _davSession;
                PostJson("/agent/logout", body);
            }
            catch (Exception e) { Log("注销请求失败：" + e.Message); }
            _davSession = "";
            _davUser = "";
            string msg;
            if (!MapDrive(_mac, _token, out msg)) NotifyTip("已注销，但只读公共盘没挂上：" + msg);
            else NotifyTip("已注销，Z 盘恢复为只读通用文件");
            UpdateTrayMenu();
            PostStatus();
        }

        /// <summary>把托盘状态报给服务器（Z 盘挂没挂、登录了谁），顺手取服务器当前的意图。</summary>
        private static void PostStatus()
        {
            try
            {
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["drive"] = _mountOk ? (_letter + ":") : "";
                info["dav_user"] = _davUser;
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac;
                body["token"] = _token;
                body["info"] = info;
                Dictionary<string, object> resp = PostJson("/agent/status", body);
                if (resp == null) return;
                object mo;
                if (resp.TryGetValue("mount", out mo) && mo is bool && !(bool)mo)
                {
                    NotifyTip("管理员已停用本机网盘，托盘退出");
                    string outp;
                    RunShell("net use " + _letter + ": /delete /y", 15000, out outp);
                    ExitTray();
                    return;
                }
                _lastAccount = GetString(resp, "account");    // 默认用户名：登录框预填
            }
            catch (Exception e)
            {
                Log("托盘状态上报失败：" + e.Message);
            }
        }

        private static void OpenDrive()
        {
            try { Process.Start("explorer.exe", _letter + ":\\"); }
            catch (Exception e) { NotifyTip("打不开 " + _letter + " 盘：" + e.Message); }
        }

        private static void OpenLog()
        {
            try { Process.Start("notepad.exe", _logPath); }
            catch { }
        }

        private static void ExitTray()
        {
            try { if (_trayIcon != null) _trayIcon.Visible = false; } catch { }
            Application.Exit();
        }

        /// <summary>托盘图标：优先用 exe 里内置的 ico，取不到就退回系统图标。</summary>
        private static Icon LoadTrayIcon()
        {
            try
            {
                Assembly asm = Assembly.GetExecutingAssembly();
                foreach (string n in asm.GetManifestResourceNames())
                {
                    if (n.EndsWith("agent.ico", StringComparison.OrdinalIgnoreCase))
                    {
                        using (Stream s = asm.GetManifestResourceStream(n))
                        {
                            if (s != null) return new Icon(s);
                        }
                    }
                }
            }
            catch { }
            return SystemIcons.Application;
        }

        private static string DescribeIcon()
        {
            try
            {
                Icon ic = LoadTrayIcon();
                return ic.Width + "x" + ic.Height
                    + (ReferenceEquals(ic, SystemIcons.Application) ? " (fallback)" : " (embedded)");
            }
            catch (Exception e) { return "error: " + e.Message; }
        }

        // ---------------- 自检（命令行，不弹界面、不挂盘） ----------------
        private static string ArgValue(string[] args, string name)
        {
            for (int i = 0; i < args.Length - 1; i++)
                if (args[i].Equals(name, StringComparison.OrdinalIgnoreCase)) return args[i + 1];
            return "";
        }

        private static int SelfTest(string[] args)
        {
            LoadConfig();
            string user = ArgValue(args, "--user");
            string pwd = ArgValue(args, "--pwd");
            Console.WriteLine("mac      : " + _mac);
            Console.WriteLine("url      : " + _url);
            Console.WriteLine("letter   : " + _letter);
            Console.WriteLine("icon     : " + DescribeIcon());
            try
            {
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["hostname"] = Environment.MachineName;
                info["agent_ver"] = VERSION;
                info["vnc_running"] = VncRunning();
                info["vnc_port"] = _vncPort;
                Dictionary<string, object> body = new Dictionary<string, object>();
                body["mac"] = _mac; body["token"] = _token; body["ver"] = VERSION; body["info"] = info;
                Dictionary<string, object> resp = PostJson("/agent/poll", body);
                Console.WriteLine("poll     : " + (GetString(resp, "ok") == "True" ? "ok"
                                  : ("fail: " + GetString(resp, "err"))) + " mount=" + GetString(resp, "mount"));

                Dictionary<string, object> si = new Dictionary<string, object>();
                si["drive"] = _letter + ":"; si["dav_user"] = "selftest";
                Dictionary<string, object> st = new Dictionary<string, object>();
                st["mac"] = _mac; st["token"] = _token; st["info"] = si;
                Dictionary<string, object> sresp = PostJson("/agent/status", st);
                Console.WriteLine("status   : " + (GetString(sresp, "ok") == "True" ? "ok"
                                  : ("fail: " + GetString(sresp, "err"))) + " account=" + GetString(sresp, "account"));

                if (user.Length > 0)
                {
                    Dictionary<string, object> lb = new Dictionary<string, object>();
                    lb["mac"] = _mac; lb["token"] = _token; lb["user"] = user; lb["pwd"] = pwd;
                    Dictionary<string, object> lresp = PostJson("/agent/login", lb);
                    string tok = GetString(lresp, "token");
                    Console.WriteLine("login    : " + (GetString(lresp, "ok") == "True"
                                      ? ("ok user=" + GetString(lresp, "user") + " token_len=" + tok.Length)
                                      : ("fail: " + GetString(lresp, "err"))));
                    Dictionary<string, object> ob = new Dictionary<string, object>();
                    ob["mac"] = _mac; ob["token"] = _token; ob["session"] = tok;
                    Dictionary<string, object> oresp = PostJson("/agent/logout", ob);
                    Console.WriteLine("logout   : " + (GetString(oresp, "ok") == "True" ? "ok" : "fail"));
                }
            }
            catch (Exception e)
            {
                Console.WriteLine("error    : " + e.Message);
                return 1;
            }
            return 0;
        }

        // ---------------- 执行外部命令（不用管道，避免受限环境拿不到子进程输出） ----------------
        private static string _cmdOutFile;

        private static string CmdOutFile()
        {
            if (_cmdOutFile == null)
                _cmdOutFile = Path.Combine(Path.GetTempPath(), "iscsi-broker-agent-cmd.txt");
            return _cmdOutFile;
        }

        private static int RunShell(string cmdline, int timeoutMs, out string output)
        {
            string tmp = CmdOutFile();
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
            ProcessStartInfo psi = new ProcessStartInfo(
                "cmd.exe", "/c " + cmdline + " > \"" + tmp + "\" 2>&1");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            return RunProcess(psi, timeoutMs, out output);
        }

        /// <summary>在指定会话的交互用户身份下执行命令（映射盘必须这么做才在“我的电脑”里可见）。</summary>
        private static int RunInSession(uint sessionId, string cmdline, int timeoutMs,
                                        out int rc, out string output)
        {
            string tmp = CmdOutFile();
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
            string full = "cmd.exe /c " + cmdline + " > \"" + tmp + "\" 2>&1";

            IntPtr hToken = IntPtr.Zero, hDup = IntPtr.Zero;
            try
            {
                if (!WTSQueryUserToken(sessionId, out hToken))
                {
                    rc = -1;
                    output = "WTSQueryUserToken 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return rc;
                }
                if (!DuplicateTokenEx(hToken, MAXIMUM_ALLOWED, IntPtr.Zero, SecurityImpersonation,
                                      TokenPrimary, out hDup))
                {
                    rc = -1;
                    output = "DuplicateTokenEx 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return rc;
                }
                STARTUPINFO si = new STARTUPINFO();
                si.cb = Marshal.SizeOf(si);
                si.lpDesktop = "winsta0\\default";
                PROCESS_INFORMATION pi;
                bool ok = CreateProcessAsUser(hDup, null, full, IntPtr.Zero, IntPtr.Zero, false,
                                              CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                                              IntPtr.Zero, null, ref si, out pi);
                if (!ok)
                {
                    rc = -1;
                    output = "CreateProcessAsUser 失败（错误码 " + Marshal.GetLastWin32Error() + "）";
                    return rc;
                }
                CloseHandle(pi.hThread);
                uint wait = WaitForSingleObject(pi.hProcess, (uint)timeoutMs);
                if (wait != 0)
                {
                    try { TerminateProcess(pi.hProcess, 1); } catch { }
                    CloseHandle(pi.hProcess);
                    rc = -1;
                    output = "命令超时";
                    return rc;
                }
                uint code;
                GetExitCodeProcess(pi.hProcess, out code);
                CloseHandle(pi.hProcess);
                rc = unchecked((int)code);
                output = ReadText(tmp);
                return rc;
            }
            catch (Exception e)
            {
                rc = -1;
                output = "会话内执行失败：" + e.Message;
                return rc;
            }
            finally
            {
                if (hDup != IntPtr.Zero) CloseHandle(hDup);
                if (hToken != IntPtr.Zero) CloseHandle(hToken);
            }
        }

        private static int RunProcess(ProcessStartInfo psi, int timeoutMs, out string output)
        {
            try
            {
                using (Process p = Process.Start(psi))
                {
                    if (!p.WaitForExit(timeoutMs))
                    {
                        try { p.Kill(); } catch { }
                        output = "命令超时";
                        return -1;
                    }
                    output = ReadText(CmdOutFile());
                    return p.ExitCode;
                }
            }
            catch (Exception e)
            {
                output = "执行失败：" + e.Message;
                return -1;
            }
        }

        private static string ReadText(string path)
        {
            try
            {
                if (!File.Exists(path)) return "";
                string t = File.ReadAllText(path, Encoding.Default).Trim();
                return t.Length > 400 ? t.Substring(0, 400) : t;
            }
            catch { return ""; }
        }

        // ---------------- 安装 / 卸载（开机以 SYSTEM 运行的计划任务） ----------------
        private static int Install()
        {
            string exe = Process.GetCurrentProcess().MainModule.FileName;
            if (!File.Exists(Path.Combine(Path.GetDirectoryName(exe), "agent.ini")))
                Console.WriteLine("警告：还没写 agent.ini，装好后请把服务器地址与令牌填进去再重启任务");
            string outp;
            string tr = "\\\"" + exe + "\\\" run";
            int rc = RunShell("schtasks /Create /TN \"" + TASK_NAME + "\" /TR \"" + tr +
                              "\" /SC ONSTART /RU SYSTEM /RL HIGHEST /F", 30000, out outp);
            Console.WriteLine("创建开机任务：rc=" + rc + " " + outp);
            if (rc != 0) return rc;
            rc = RunShell("schtasks /Run /TN \"" + TASK_NAME + "\"", 30000, out outp);
            Console.WriteLine("立即启动任务：rc=" + rc + " " + outp);
            Console.WriteLine("安装完成。日志：" + Path.Combine(Path.GetDirectoryName(exe), "agent.log"));
            return 0;
        }

        private static int Uninstall()
        {
            string outp;
            int rc = RunShell("schtasks /End /TN \"" + TASK_NAME + "\"", 20000, out outp);
            rc = RunShell("schtasks /Delete /TN \"" + TASK_NAME + "\" /F", 20000, out outp);
            Console.WriteLine("删除开机任务：rc=" + rc + " " + outp);
            return 0;
        }

        // ---------------- P/Invoke ----------------
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute;
            public int dwFlags;
            public short wShowWindow, cbReserved2;
            public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess, hThread;
            public int dwProcessId, dwThreadId;
        }

        [DllImport("kernel32.dll")]
        private static extern uint WTSGetActiveConsoleSessionId();

        [DllImport("wtsapi32.dll", SetLastError = true)]
        private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr phToken);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DuplicateTokenEx(IntPtr hExistingToken, int dwDesiredAccess,
            IntPtr lpTokenAttributes, int impersonationLevel, int tokenType, out IntPtr phNewToken);

        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool CreateProcessAsUser(IntPtr hToken, string lpApplicationName,
            string lpCommandLine, IntPtr lpProcessAttributes, IntPtr lpThreadAttributes,
            bool bInheritHandles, uint dwCreationFlags, IntPtr lpEnvironment,
            string lpCurrentDirectory, ref STARTUPINFO lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool FreeConsole();
    }
}

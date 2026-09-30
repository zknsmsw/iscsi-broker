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
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

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
        private static bool _mountRequested;
        private static bool _mountOk;
        private static uint _appliedSession = INVALID_SESSION;
        private static readonly object _logLock = new object();

        // ---------------- 入口 ----------------
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
                else if (low == "install" || low == "uninstall" || low == "run"
                         || low == "once" || low == "test") action = low;
            }

            if (action == "install") return Install();
            if (action == "uninstall") return Uninstall();

            LoadConfig();
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
                EnsureDriveOnNewSession();     // 登录会话变化后自动补挂网盘
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["hostname"] = Environment.MachineName;
                info["agent_ver"] = VERSION;
                info["vnc_running"] = VncRunning();
                info["vnc_port"] = _vncPort;
                info["drive"] = (_mountRequested && _mountOk) ? (_letter + ":") : "";
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
                case "mount": ok = SetMount(true, out msg); break;
                case "unmount": ok = SetMount(false, out msg); break;
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

        // ---------------- 网盘挂载 ----------------
        private static bool SetMount(bool mount, out string msg)
        {
            if (mount)
            {
                _mountRequested = true;
                _appliedSession = INVALID_SESSION;         // 强制下一次重挂
                bool ok = MapDrive(out msg);
                _mountOk = ok;
                SaveState();
                return ok;
            }
            _mountRequested = false;
            _mountOk = false;
            SaveState();
            bool ok2 = UnmapDrive(out msg);
            return ok2;
        }

        /// <summary>按服务器的意图把网盘状态对齐（开机后自动补挂 / 自动卸载）。</summary>
        private static void ReconcileMount(bool want)
        {
            string msg;
            _mountRequested = want;
            if (want)
            {
                _appliedSession = INVALID_SESSION;
                _mountOk = MapDrive(out msg);
                Log("按服务器要求挂载网盘" + (_mountOk ? "成功：" : "失败：") + msg);
            }
            else
            {
                bool ok = UnmapDrive(out msg);
                _mountOk = false;
                Log("按服务器要求卸载网盘" + (ok ? "成功" : "失败：" + msg));
            }
            SaveState();
        }

        /// <summary>会话变化（用户登录/切换）后自动补挂网盘。</summary>
        private static void EnsureDriveOnNewSession()
        {
            if (!_mountRequested) return;
            uint sid = WTSGetActiveConsoleSessionId();
            if (sid == INVALID_SESSION || sid == _appliedSession) return;
            string msg;
            if (MapDrive(out msg))
            {
                _mountOk = true;
                _appliedSession = sid;
                SaveState();
                Log("会话 " + sid + " 已挂载网盘 " + _letter + ": " + msg);
            }
            else
            {
                Log("会话 " + sid + " 挂载网盘失败：" + msg);
            }
        }

        private static bool MapDrive(out string msg)
        {
            string url = _url + "/dav/";
            msg = "";
            if (_dryRun)
            {
                msg = "[dry-run] net use " + _letter + ": " + url + " /user:" + _mac;
                return true;
            }
            PrepareWebClient();
            uint sid = WTSGetActiveConsoleSessionId();
            if (sid == INVALID_SESSION)
            {
                msg = "当前没有已登录的交互用户，等用户登录后自动挂载";
                return false;
            }
            int rc;
            string outp;
            RunInSession(sid, "net.exe use " + _letter + ": \"" + url + "\" /user:" + _mac
                              + " " + _token + " /persistent:no", 30000, out rc, out outp);
            if (rc == 0)
            {
                msg = "已挂载 " + _letter + ": → " + url;
                return true;
            }
            msg = "net use 失败(rc=" + rc + ")：" + outp;
            return false;
        }

        private static bool UnmapDrive(out string msg)
        {
            string outp;
            msg = "";
            if (_dryRun) { msg = "[dry-run] net use " + _letter + ": /delete /y"; return true; }
            uint sid = WTSGetActiveConsoleSessionId();
            if (sid == INVALID_SESSION) { msg = "当前没有已登录的交互用户"; return false; }
            int rc;
            RunInSession(sid, "net.exe use " + _letter + ": /delete /y", 20000, out rc, out outp);
            _appliedSession = INVALID_SESSION;
            if (rc == 0) { msg = "已卸载 " + _letter + ":"; return true; }
            msg = "net use /delete 失败(rc=" + rc + ")：" + outp;
            return false;
        }

        /// <summary>
        /// Windows 自带的 WebDAV 重定向器默认不允许明文 http 上用 Basic 认证、
        /// 还有 50MB 文件大小上限；这里把注册表改好并重启 WebClient 服务。
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
                RunShell("sc config WebClient start= demand", 10000, out outp);
                RunShell("net start WebClient", 20000, out outp);
            }
            catch (Exception e)
            {
                Log("配置 WebClient 失败（可能不影响已配好的机器）：" + e.Message);
            }
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
    }
}

# 客户机客户端（agent）

给无盘 Windows 客户机用的常驻小程序，实现三件事：

| 功能 | 说明 |
|------|------|
| 远程关机 / 重启 | 后台「客户机控制」页点一下，客户机 3 秒后关机 / 重启 |
| 网盘挂载 | 把服务器个人网盘（WebDAV）映射成客户机上的盘符，重启/重新登录后自动挂回来 |
| VNC 控制 | 后台点「启用 VNC」拉起客户机上的 VNC 服务端，网页里直接看画面并操作 |

设计上客户机**不开放任何入站端口**：agent 每隔几秒主动向服务器心跳（`POST /agent/poll`），
管理员的指令随心跳下发，执行完再回报（`POST /agent/result`）。所以客户机在 NAT / 防火墙
后面也能被控制。

```
母盘里：C:\iscsi-broker-agent\
        ├── iscsi-broker-agent.exe     开机由计划任务以 SYSTEM 身份运行
        ├── agent.ini                  服务器地址 + 接入令牌 + VNC 路径
        ├── agent.state                运行期状态（挂载意图缓存）
        └── agent.log                  运行日志
浏览器（管理员） --8080--> 服务器 --TCP 5900--> 客户机 VNC 服务端
                        └--HTTP WebDAV--------> 客户机映射的盘符
```

---

## 1. 编译

在任意 Windows 10/11 上双击 `build.bat`（或命令行执行）。它用系统自带的
`.NET Framework` 编译器，**不需要装 Visual Studio / .NET SDK**。

产物：`client\dist\iscsi-broker-agent.exe`（单个小 exe，目标机**不需要**装 .NET 运行时，
Win10/11 自带）。`client\dist\` 是构建产物目录，不进仓库。

> 想改代码：只改 `Agent.cs`（C# 5 语法，因为用的是系统内置的旧版 csc），重新跑 `build.bat`。

## 2. 在母盘里装一次

客户机是无盘的，普通模式下改动都落在叠加盘、空闲后会被删掉，所以客户端必须做进母盘：

1. iPXE 菜单选 **Admin Mode**（回写模式）启动一台客户机，把系统配置做完；
2. 建议先装一个 VNC 服务端（TightVNC / UltraVNC 都行），**装成 Windows 服务**
   （服务模式才能看到登录界面；直接拉起 exe 只能看到当前桌面）；
3. 新建目录 `C:\iscsi-broker-agent\`，把 `iscsi-broker-agent.exe` 和 `agent.ini` 放进去；
4. 编辑 `agent.ini`：
   - `[server] url=` 填服务器地址（**Web 后台端口 8080**，不是 iPXE 的 5000）；
   - `[server] token=` 抄服务器后台「客户机控制」页上的**接入令牌**；
   - `[vnc] service=`（推荐，填 VNC 注册的服务名，例如 `tvnserver`）或 `[vnc] exe=`；
5. 管理员身份执行（在客户机里）：

   ```cmd
   C:\iscsi-broker-agent\iscsi-broker-agent.exe test      :: 检查配置/MAC 读得对不对
   C:\iscsi-broker-agent\iscsi-broker-agent.exe install   :: 装成“开机以 SYSTEM 运行”并立即启动
   ```

6. 关机（母盘就绪，之后所有普通模式启动的客户机都自带 agent）。

### agent.ini

```ini
[server]
url=http://10.1.1.1:8080    ; 服务器 Web 后台地址
token=                      ; 后台「客户机控制」页上的接入令牌
interval=3                  ; 心跳间隔（秒），指令随心跳下发
mac=                        ; 一般留空；自动取“默认网关网卡”的 MAC。多网卡/测试机才写

[vnc]
exe=                        ; VNC 服务端 exe 全路径（与 service 二选一）
args=-run                   ; 启动参数
service=tvnserver           ; VNC 注册的 Windows 服务名（推荐，可看登录界面）
port=5900                   ; 只用于后台展示

[dav]
letter=Z                    ; 网盘映射到哪个盘符
```

## 3. 后台怎么用（/web/clients 「客户机控制」）

- **接入令牌**：页首显示当前令牌，可一键重置（重置后所有客户机都要改 `agent.ini`）。
- **客户机表格**：MAC / 主机名 / IP / agent 在线状态 / 网盘盘符 / VNC 状态 /
  最近一条指令结果；只有 iPXE 记录、没有 agent 心跳的机器会标成「未装 agent」。
- **关机 / 重启**：立即执行。
- **启用 VNC / 停用 VNC**：VNC 起来后该行出现「VNC 画面」链接，打开就是内嵌的 noVNC 页面
  （需要 VNC 自己的密码时页面会提示输入）。
- **网盘账号**：填该机器对应的网盘用户名，保存后才允许挂载。
- **挂载 / 卸载网盘**：挂载意图记在服务器上，客户机每次开机心跳会自动对齐，
  所以**重启后不用再点一次**；客户机没登录交互用户时会等登录后自动补挂。

## 4. 注意与排错

- **令牌等于控制权**：拿到令牌就能让任意 MAC 的客户机执行关机/重启/挂 VNC。
  只在内网使用；怀疑泄漏就在后台重置。WebDAV 也用同一个令牌认证（用户名填 MAC），
  所以客户机上**不需要保存任何网盘密码**。
- **MAC 是自报的**：局域网二层本来就挡不住伪造（和联网控制一样），按内网信任使用。
- **VNC 密码**：由 VNC 服务端自己管（agent 不改它），在网页里按提示输入即可。
- **网盘是明文 http 的 WebDAV**：Windows 默认禁止在 http 上用 Basic 认证、且限制单文件
  50MB；agent 挂载前会自动改好注册表（`BasicAuthLevel=2`、`FileSizeLimitInBytes`、
  `AuthForwardServerList` 只放行本服务器）并重启 WebClient 服务。
- **日志**：`C:\iscsi-broker-agent\agent.log`。
- **排错命令**：

  ```cmd
  iscsi-broker-agent.exe test              :: 打印配置、MAC、VNC 状态
  iscsi-broker-agent.exe once              :: 只跑一轮心跳
  iscsi-broker-agent.exe run               :: 前台运行（看实时日志）
  iscsi-broker-agent.exe run --dry-run     :: 只打印将要执行的指令，不真关机/真映射盘
  iscsi-broker-agent.exe uninstall         :: 删掉开机任务
  ```

- 后台「客户机控制」页看不到机器：确认 `agent.ini` 里的 url 通（浏览器打开
  `http://<服务器>:8080/` 试）、令牌没抄错、任务在跑（`schtasks /Query /TN iSCSI-Broker-Agent`）。

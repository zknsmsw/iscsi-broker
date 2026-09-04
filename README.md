# iSCSI Broker

网络启动（iPXE）+ iSCSI 磁盘供给 + 账号体系 + 个人网盘 一体化服务器。

客户机通过 PXE/iPXE 从网络引导，选择镜像后由服务器把磁盘镜像导出为 iSCSI LUN，客户机 `sanboot` 直接连盘启动（无盘启动）；同时提供 Web 账号管理与个人网盘（云盘）服务。

---

## 一、功能总览

### 1. iPXE 无盘启动（端口 5000）
- 启动菜单自动扫描 `images/` 下的 `*.raw` 母盘生成，按镜像名选择、超时自动默认。
- **两种叠加模式**（启动时自动检测，可用 `FORCE_MODE` 强制）：
  - 路线 A（推荐）：文件系统支持 reflink（XFS/btrfs）时，`cp --reflink=always` 秒级生成叠加盘直出 raw，**无 qemu**，性能最好、母盘只读。
  - 路线 B（回退）：不支持 reflink（如 ext4）时退回 `qemu-nbd + qcow2` 叠加盘，带参数降级重试。
- **管理员回写模式**：输入管理员账号（admin）密码后，可直接把母盘导出为可写盘（修改写入母盘），同一母盘同一时刻只允许一台回写。

### 2. 账号系统
- **Web 登录**：必须使用账号 `admin`（用户名 + 密码）。
- **iPXE 后台登录**：同样只接受账号 `admin`。
- **开放注册**：Web 端 `/web/register` 注册，仅需用户名 + 密码（无重复确认）。
- 注册账号拥有个人网盘；`admin` 拥有管理后台。

### 3. 个人网盘（云盘）
- 普通用户功能（**仅**）：上传文件、下载文件、创建文件夹。
- 每个账号独立空间，受**配额**限制（默认 1 GiB，管理员可改）。
- **通用文件**：管理员上传的文件出现在所有账号网盘的"通用文件"目录下，**只读**。

### 4. Web 管理后台（端口 8080，仅 admin）
- 客户机名单（在线状态、回写占用）、创建空白盘、修改管理员密码。
- **iSCSI 挂载**：把任意一张“未在使用的镜像（母盘 .raw）”直接导出为 iSCSI target（**写直达母盘**，等同回写语义），页面**写出 IQN** 供任意 iSCSI 发起端（Windows 发起程序 / iscsiadm）手动连接，并支持一键卸载；挂载中的镜像不可再被 PXE 叠加启动或回写，反之亦然（同一镜像同一时刻仅一种使用方式）。
- **用户与配额**：逐用户调整存储配额。
- **默认配额**：修改新注册账号的默认空间大小。
- **通用文件**：上传 / 下载 / 删除通用文件。
- **联网控制**：默认行为（允许/禁止）+ 逐客户机 允许/禁止/恢复默认，页面底部只读展示当前 FORWARD/NETCTRL/NAT 规则。

### 5. 空闲自动清理
- 客户机正常 logout 后 target 空闲超时（默认 5 分钟）自动回收；
- 无流量检测（`ss` lastrcv + `arping` L2 确认）识别关机/断电不主动断连的客户机，防资源泄漏。

### 6. 联网控制（按 MAC 控制客户机上网）
- 服务器担任客户机网关（FORWARD 转发 + MASQUERADE），本功能在 FORWARD 最前面挂专用链 **NETCTRL**，按客户机 **MAC** 放行/拒绝“内网→外网”流量；
- **默认行为**：允许或禁止（对未手动设置的客户机生效）；
- **逐机开关**：Web 后台可对每台客户机单独 允许 / 禁止 / 恢复默认，改动立即生效；
- 客户机开机（iPXE 请求供给）自动建立/覆写规则，关机规则空转、巡检（默认 30 秒）自动清理“离线且无手动设置”的机器；
- 被禁机器仍可 PXE/iPXE 无盘启动并使用 iSCSI 盘（只禁外网，不碰服务器自身服务）；客户机互访不受影响；
- 规则改动前自动 `iptables-save` 快照备份到 `netctrl_backup/`（保留最近 20 份）。

---

## 二、文件说明

| 文件 | 用途 |
|------|------|
| `iscsi_broker.py` | **主程序**：两个 HTTP 服务（端口 5000 iPXE 供给脚本 / 端口 8080 Web 管理后台）+ iSCSI 供给、叠加盘、空闲清理、账号与网盘页面。 |
| `users_auth.py` | **账号认证模块**：注册 / 登录校验 / 配额管理 / 默认配额，用户数据持久化到 `users.conf`、`cloud.conf`。 |
| `cloud_store.py` | **网盘存储模块**：目录列表、上传（流式 multipart 解析）、下载、建文件夹、配额统计、通用文件管理，含路径穿越与符号链接防护。 |
| `netctrl.py` | **联网控制模块**：`netctrl.conf` 状态读写、FORWARD/NAT 规则托管（iptables 按 MAC 过滤 + MASQUERADE）、开机/巡检规则对齐、改动前自动备份。 |
| `test_cloud_store.py` | 网盘模块自测脚本（75 项断言），回归用。 |
| `test_netctrl.py` | 联网控制模块逻辑自测（纯逻辑，不依赖 root/iptables）。 |
| `test_iscsi_export.py` | 后台手动 iSCSI 挂载模块逻辑自测（纯逻辑，不依赖 root/tgtadm）。 |

---

## 三、目录结构（运行时自动生成）

```
/home/prts/server/                  ← BASE_DIR（OVERLAY_DIR = BASE_DIR，可改）
├── images/                          ← 母盘目录，手动放置 xxx.raw（不会自动创建！）
├── admin.conf                       ← 管理员密码（加盐 SHA256 哈希）
├── users.conf                       ← 注册用户列表（每行 用户名$sha256$salt$digest$配额）
├── cloud.conf                       ← 默认配额（一行 default_quota=<字节>）
├── netctrl.conf                     ← 联网控制配置（一行 default=allow|deny + 每 MAC 一行 <mac>=allow|deny）
├── netctrl_backup/                  ← iptables-save 快照（接管/改动前自动备份，保留最近 20 份）
├── cloud/                           ← 网盘数据根
│   ├── <用户名>/                    ← 每个用户的私有目录
│   ├── _common/                     ← 通用文件（对所有账号只读展示为"通用文件"）
│   └── .tmp/                        ← 上传临时目录（失败自动清理）
├── overlay_<mac>_<镜像>.raw|.qcow2  ← 叠加盘（运行期生成，空闲后自动删除）
└── overlay_*.qcow2 / overlay_*.raw  ← 启动清理的遗留叠加盘
```

> `BASE_DIR`、`images/`、`users.conf`、`cloud.conf`、`cloud/` 及其子目录中：**除 `images/` 外全部自动创建**。`images/` 需手动创建并放入母盘 `.raw` 文件（不放则启动菜单显示"无镜像"）。

---

## 四、依赖

### 系统依赖（Linux，需 root）
| 依赖 | 用途 |
|------|------|
| Python 3.10+ | 运行脚本（代码使用 `str \| None` 等 3.10 语法；3.12 验证通过） |
| tgt（`tgtadm`） | iSCSI target 管理（创建/删除 target、LUN） |
| qemu-utils（`qemu-img`、`qemu-nbd`） | 路线 B qcow2 叠加盘（路线 A 不需要） |
| Linux `nbd` 内核模块 | 路线 B 的 `/dev/nbdX` 块设备（`modprobe nbd max_part=8 nbds_max=16`） |
| `iproute2`（`ss`、`ip`） | 无流量检测、路由选网卡 |
| `arping`（iputils-arping） | L2 层在线确认 |
| `iptables`（iptables-nft） | 联网控制：FORWARD 按 MAC 过滤 + NAT MASQUERADE |
| `findmnt`（util-linux） | 检测文件系统类型（reflink 支持） |
| `sysctl` | 启动时网络缓冲调优 |
| XFS/btrfs 文件系统 | 路线 A reflink 直出（不支持自动回退路线 B） |

### Python 依赖
**纯标准库**（无第三方包）：`http.server`、`urllib.parse`、`subprocess`、`os`、`datetime`、`hashlib`、`threading`、`glob`、`time`、`re`、`secrets`、`html`、`ssl`、`tempfile`。

> 注意：Windows 上可编译、可 import、可跑模块测试（`test_cloud_store.py`），但完整运行（tgt/qemu-nbd/modprobe）仅限 Linux。

---

## 五、部署与使用

### 1. 部署
```bash
# 1) 修改 iscsi_broker.py 顶部 BASE_DIR 为实际绝对路径
# 2) 安装依赖（以 Debian/Ubuntu 为例）
apt install python3 tgt qemu-utils iproute2 iputils-arping util-linux
# 3) 准备母盘目录并放入镜像
mkdir -p /home/prts/server/images
#    把 xxx.raw 母盘放进去（如 win11.raw）
# 4) 启动（需 root）
sudo python3 iscsi_broker.py
```
启动后日志会打印当前模式（reflink / qcow2）与 Web 后台地址。

### 2. DHCP / PXE 指向
- DHCP 下发的引导文件指向 iPXE，首个链地址为 `http://<服务器IP>:5000/boot.ipxe`。
- 若启用 HTTPS（`HTTPS_ENABLED`），该链地址需同步改为 `https://...`。

### 3. Web 使用流程
浏览器访问 `http://<服务器IP>:8080/`：
- **管理员**：用户名 `admin` + 密码（默认 `admin123`，**部署前务必修改**）→ 管理后台（客户机名单 / 创建空白盘 / iSCSI 挂载 / 修改密码 / 用户与配额 / 默认配额 / 通用文件 / 联网控制）。
  - **iSCSI 挂载**页：选一张“空闲”的母盘点挂载 → 页面返回 IQN → 在任意机器上用 iSCSI 发起端连接该 IQN（服务器 IP:3260）即得到一块写直达母盘的可写盘；用完回后台点“卸载”。
- **普通用户**：先"注册账号"（用户名 + 密码）→ 登录 → 我的网盘：
  - 上传文件（单文件，受配额限制）、下载文件、新建文件夹；
  - 根目录可见"通用文件"文件夹（只读，内容由管理员维护）。

### 4. iPXE 使用流程
1. 客户机从网络引导进入启动菜单，选择镜像 → 服务器生成叠加盘并返回 `sanboot iscsi:...` 指令 → 连盘启动。
2. 菜单中选 **Admin Mode** → 提示输入用户名（必须是 `admin`）与密码 → 选择镜像以**回写模式**启动（直接写母盘）。

---

## 六、主要配置项（`iscsi_broker.py` 顶部）

| 配置 | 默认 | 说明 |
|------|------|------|
| `BASE_DIR` | `/home/prts/server` | 服务器数据根目录（**部署前必改**） |
| `PORT` / `WEB_PORT` | 5000 / 8080 | iPXE 供给端口 / Web 后台端口 |
| `DEFAULT_IMAGE` | `win11` | 启动菜单默认高亮镜像 |
| `FORCE_MODE` | `auto` | `auto` / `reflink` / `qcow2` |
| `ADMIN_PASSWORD` | `admin123` | 管理员初始密码（**部署前必改**；存为 `admin.conf` 哈希） |
| `THIN_ON_REFLINK` | `False` | reflink 模式是否开启 TRIM（默认关） |
| `NBD_MAX` | 16 | 路线 B 最大并发客户机数 |
| `WEB_ENABLED` | `True` | 是否启用 Web 管理后台 |
| `HTTPS_ENABLED` / `HTTPS_CERT` / `HTTPS_KEY` | `False` | 可选 HTTPS |
| `NETCTRL_ENABLED` | `True` | 是否启用联网控制 |
| `NETCTRL_LAN_IF` / `NETCTRL_WAN_IF` | 自动探测 | 内网卡（接交换机）/ 外网卡（接外网）；留空自动探测 |
| `NETCTRL_FULL_TAKEOVER` | `True` | `True`=启动时清空并重建 FORWARD/POSTROUTING（旧转发/NAT 手工规则被替换）；`False`=只管理自己的链，与现有规则共存 |
| `NETCTRL_REJECT` | `True` | `True`=REJECT（客户机立即报“无法连接”）；`False`=DROP（静默丢弃，卡到超时） |
| `NETCTRL_RECONCILE_INTERVAL` | `30` | 规则巡检间隔（秒） |
| 默认配额 | 1 GiB | 新注册账号配额，管理员可在后台修改 |

**账号规则**：用户名 3-32 位 `[A-Za-z0-9_-]`（保留 `admin`）；密码 6-32 位同字符集；配额 1 字节 ~ 8 TiB。

---

## 六点五、联网控制说明

- **原理**：客户机以服务器为网关，出外网流量必经 FORWARD。本功能在 FORWARD 第 1 条挂专用链 `NETCTRL`，按客户机 MAC 判定“内网→外网”流量：手动设置优先，其余按默认行为（allow/deny）兜底；NAT 回程（`RELATED,ESTABLISHED`）与 `MASQUERADE` 一并托管。
- **规则生命周期**：客户机开机（PXE/iPXE 请求、ARP 邻居表出现、iSCSI 连接）→ 立即建立/覆写规则；关机 → 规则空转无害；巡检线程（`NETCTRL_RECONCILE_INTERVAL` 秒）自动清理“离线且无手动设置”的机器；Web 改策略立即生效并写入 `netctrl.conf`。
- **接管模式**：`NETCTRL_FULL_TAKEOVER=True`（默认）时，启动会**清空 FORWARD 与 POSTROUTING 并重建托管规则**——旧的转发/NAT 手工规则（如原来手写的 `-A FORWARD ... ACCEPT`、`MASQUERADE`）会被替换，无需手动清理；如需与其他规则共存，设为 `False`（只管理自己的链）。
- **REJECT vs DROP**：`NETCTRL_REJECT=True` 时被禁客户机**立即**收到“无法连接”（推荐）；`False` 时静默丢包，客户机卡到超时才失败。
- **IPv6**：客户机不分配 IPv6；启动时关闭 IPv6 转发（`net.ipv6.conf.all.forwarding=0`），防止走 IPv6 绕过。
- **限制**：MAC 可被伪造（二层局域网通病）；只控制“出外网”方向，不控制客户机互访；需 root 运行（项目本身要求）。
- **备份**：接管/重建前自动 `iptables-save` 快照到 `netctrl_backup/`（保留 20 份），随时可还原。

---

## 七、安全说明

- 管理员密码与用户密码均以**加盐 SHA256** 存储（`admin.conf` / `users.conf`，权限 600），常量时间比较防时序侧信道。
- Web 后台会话 Cookie `HttpOnly + SameSite=Lax`，所有需登录的 POST 均校验 **CSRF**（流式上传令牌放查询串）。
- 网盘路径经 `resolve()` 三层防护：非法字符 / `..` 段 / 绝对路径拒绝 → `commonpath` 包含校验 → `realpath` 防符号链接逃逸。
- 上传配额在流式写入过程中强制，超限立即中止并清理临时文件。
- 普通用户无法写入"通用文件"区；管理员页面有角色守卫。
- Web 页面**兼容 IE11**：零 JS（或仅 ES5）、`X-UA-Compatible=IE=edge`、单文件上传（IE11 不支持多选）、表格布局无 flex/grid。

---

## 八、测试

```bash
python test_cloud_store.py   # 网盘模块 75 项断言
python test_netctrl.py       # 联网控制模块逻辑断言（不依赖 root/iptables）
python test_iscsi_export.py  # 后台手动 iSCSI 挂载模块逻辑断言（不依赖 root/tgtadm）
python -m py_compile iscsi_broker.py users_auth.py cloud_store.py netctrl.py
```

开发期验证记录：模块单测 75/75、Web 端到端（真实 HTTP 服务）35/35、账号/网盘/配额/通用文件冒烟 34/34、联网控制逻辑 30 项全部通过。

---

## 九、已知限制

- `images/` 母盘目录不会自动创建，需手动放置 `.raw`。
- 母盘 `images/*.raw` 会被视为启动菜单镜像；网盘数据在 `cloud/` 下，与母盘隔离。
- Web 登录目前仅 `sleep(1)` 防爆破，无 IP 级窗口限速。

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
| Python 3.10+ | 运行脚本（代码使用 `str \| None` 等 3.10 语法） |
| tgt（`tgtadm`） | iSCSI target 管理（创建/删除 target、LUN） |
| qemu-utils（`qemu-img`、`qemu-nbd`） | 路线 B qcow2 叠加盘（路线 A 不需要） |
| Linux `nbd` 内核模块 | 路线 B 的 `/dev/nbdX` 块设备（`modprobe nbd max_part=8 nbds_max=16`） |
| `iproute2`（`ss`、`ip`） | 无流量检测、路由选网卡 |
| `arping`（iputils-arping） | L2 层在线确认 |
| `iptables`（iptables-nft） | 联网控制：FORWARD 按 MAC 过滤 + NAT MASQUERADE |
| `dnsmasq` | 客户机 DHCP + TFTP，把 iPXE 引导器推给客户机（见第五节） |
| `ipxe`（提供 `undionly.kpxe` / `ipxe.efi`） | 客户机 PXE 阶段加载的 iPXE 引导器文件 |
| `findmnt`（util-linux） | 检测文件系统类型（reflink 支持） |
| `sysctl` | 启动时网络缓冲调优 |
| XFS/btrfs 文件系统 | 路线 A reflink 直出（不支持自动回退路线 B） |

### Python 依赖
**纯标准库**（无第三方包）：`http.server`、`urllib.parse`、`subprocess`、`os`、`datetime`、`hashlib`、`threading`、`glob`、`time`、`re`、`secrets`、`html`、`ssl`、`tempfile`。

> 注意：Windows 上可编译、可 import，但完整运行（tgt/qemu-nbd/modprobe）仅限 Linux。

---

## 五、网络架构与部署

### 1. 架构：客户机只连服务器，服务器连外网

```
              ┌─────────── 外网 / 上级路由 ───────────┐
              │                                       │
         [ WAN 口 ]
   ┌──────────────────── 服务器（Linux，root 运行） ───────────────────┐
   │  dnsmasq：DHCP(67) + TFTP(69)，只监听 LAN 口                      │
   │  iPXE 供给 HTTP(5000)   Web 后台(8080)   iSCSI target(3260)       │
   │  FORWARD + MASQUERADE（客户机唯一出口）+ NETCTRL（按 MAC 放行/禁止）│
   │  images/*.raw 母盘、叠加盘、cloud/ 网盘数据                       │
   └──────[ LAN 口 ]───────────┬───────────────────────┬─────────────┘
                              │ 交换机                 │
                       ┌──────┴──────┐         ┌──────┴──────┐
                       │  客户机 A   │         │  客户机 B   │
                       └─────────────┘         └─────────────┘
```

- **客户机**：不接外网、也不需要外网。开机只做三件事——向服务器要 IP 和引导文件（DHCP）、取 iPXE（TFTP）、取启动脚本并连 iSCSI 盘（HTTP 5000 + iSCSI 3260）。**客户机的所有流量都只到服务器 LAN 口**。
- **客户机的默认网关必须指向服务器 LAN 口 IP**（由 dnsmasq 下发 `option:router`）。这样客户机出外网的流量才会经过服务器的 FORWARD + MASQUERADE，按 MAC 的联网控制（NETCTRL）才有意义；网关留空则客户机不能上网，但无盘启动照常。
- **服务器**：WAN 口接外网 / 上级路由（默认路由所在的网卡）；LAN 口接交换机，配静态 IP（示例 `10.1.1.1/24`）。脚本会自动探测网卡（带默认路由的=外网卡，另一张有 IPv4 且 UP 的=内网卡），多网卡或探测不准时用 `NETCTRL_LAN_IF` / `NETCTRL_WAN_IF` 显式指定。
- 脚本启动时会开启 `net.ipv4.ip_forward=1`、关闭 IPv6 转发（客户机不分配 IPv6，防止绕过联网控制），并按配置清空 / 重建 FORWARD、POSTROUTING 规则。
- 用到的端口：`67/udp` DHCP、`69/udp` TFTP（均由 dnsmasq 提供，只开在 LAN 口）、`5000/tcp` iPXE 供给、`8080/tcp` Web 后台、`3260/tcp` iSCSI。若服务器开了 ufw / firewalld，需在 LAN 口放行这些端口。

### 2. 服务器准备与启动

```bash
# 1) 修改 iscsi_broker.py 顶部 BASE_DIR 为实际绝对路径
# 2) 安装依赖（以 Debian/Ubuntu 为例）
apt install python3 tgt qemu-utils iproute2 iputils-arping util-linux dnsmasq ipxe
# 3) 准备母盘目录并放入镜像
mkdir -p /home/prts/server/images
#    把 xxx.raw 母盘放进去（如 win11.raw）；母盘怎么做见下一节
# 4) 给 LAN 口配静态 IP（示例，网卡名按实际改）
ip addr add 10.1.1.1/24 dev enp3s0
ip link set enp3s0 up
# 5) 启动（需 root）
sudo python3 iscsi_broker.py
```

启动后日志会打印当前模式（reflink / qcow2）、Web 后台地址和联网控制用的内/外网卡。

### 3. 母盘（镜像）制作：Linux / Windows

“制作母盘”= 把一套系统**装进一块整盘镜像**，并让它在 **iSCSI 上能自己启动**。先把原理说清楚，否则装出来的盘一定起不来：

- iPXE 的 `sanboot iscsi:...` 会把 SAN 盘挂成 0x80、给系统留一份 **iBFT**（iSCSI Boot Firmware Table，iPXE 文档明确说 SAN 盘会通过 iBFT 描述），随后**撤掉自己的 hook**；内核起来后，这块盘要靠**系统自己的 iSCSI 发起端**重新连上。
- 所以系统里**必须**有能在“内核启动阶段”就跑起来的 iSCSI 发起端和网卡驱动，并且注册表/内核参数要让它们随内核启动。否则 iPXE 一交棒盘就“消失”：Windows 卡在 `INACCESSIBLE_BOOT_DEVICE` / 0x7B，Linux 是 initramfs 找不到 root。
- 因此 Windows 母盘**不能**在普通虚拟机里对着本地盘装完再转过来——必须让安装程序**直接装在 iSCSI 盘上**，安装阶段才会把网卡和 iSCSI 发起端登记成启动设备。
- 另外的前提：成品是**整盘 raw**；固件要匹配（BIOS→**MBR+活动分区**，UEFI→**GPT+ESP**）；**客户机网卡驱动必须进系统**（Windows 要手动注入，Linux 一般在 initramfs 里带上）。

#### Windows 母盘：在 iPXE 引导的 WinPE 里装到 iSCSI 盘

**1）准备 PE**：直接用现成的**带网卡驱动的 PE**（网络版 PE 遍地都是，微PE / 优启通这类就行），**不用**自己拿 ADK + imagex/dism 去做，也不用往 `boot.wim` 里塞驱动。把 PE 文件按你平时的做法放到服务器（丢进 `images/` 就行），iPXE 菜单里选它启动进 PE。

**2）PE 里连上服务器的 iSCSI 目标**：

```cmd
wpeinit
iscsicli QAddTargetPortal 10.1.1.1
iscsicli QLoginTarget iqn.2026-07.storage:web-win11
diskpart                       :: list disk → 应能看到这块 iSCSI 盘
```

（PE 里有图形版 iSCSI 发起程序的话，点两下也一样；也可以在 iPXE 侧先 `sanhook --drive 0x80 iscsi:10.1.1.1:::1:iqn...` 把盘挂上再进 PE。）

**3）把 Windows 装到这块盘上**：

- **推荐：直接跑安装程序**。在 PE 里执行 `\\server\installers\win11\setup.exe`，"安装到哪里"选那块 iSCSI 盘（分区按固件来：BIOS 建 MBR+活动分区，UEFI 建 GPT+ESP）。这么装，**PE 里已经加载的网卡驱动会自动带进新系统**，不用另外注入。
- **或者离线展开**：用 **Dism++**"文件 → 释放映像"把 `install.wim` 释放到那块盘，再用 Dism++ 的"引导修复"写引导（命令行等价物是 `dism /Apply-Image` + `bcdboot`）。离线展开时网卡驱动不会自动带进去，需要在 Dism++ 里打开刚释放的系统 → 驱动管理 → 添加驱动（等价命令：`dism /Image:C:\ /Add-Driver /Driver:D:\drivers\nic /Recurse`），否则内核启动阶段没网卡驱动，照样连不上 iSCSI。

**4）装完先别重启**，在 PE 里把 iSCSI 发起端设成随内核启动（PE 里用 regedit 挂载目标系统的 `C:\Windows\System32\config\SYSTEM`，或直接敲下面命令）：

```cmd
reg load HKLM\OFF C:\Windows\System32\config\SYSTEM
:: iSCSI 发起端：Windows 默认是 3（按需启动），改成 0（随内核启动）
reg add "HKLM\OFF\ControlSet001\Services\msiscsi" /v Start /t REG_DWORD /d 0 /f
:: 客户机用的那块网卡驱动也要随内核加载（一般装驱动后就是 boot-start，不是再改成 0）
reg add "HKLM\OFF\ControlSet001\Services\<网卡驱动服务名>" /v Start /t REG_DWORD /d 0 /f
reg unload HKLM\OFF
```

- 网卡驱动服务名（如 `e1i63x64`、`rt640x64`、`mlx5`）可以在 PE 里 `reg query "HKLM\OFF\ControlSet001\Services" /s /v ImagePath` 找，或看已装系统的“设备管理器 → 网卡 → 属性 → 驱动程序 → 服务名”。
- **不需要**在注册表里预置 TCP/IP（IP / 网关 / DNS）：iPXE 在引导阶段已经 DHCP 好，并把**网卡地址、目标 portal、target IQN** 一起写进 **iBFT** 交给 Windows，Windows 启动时直接从这里取（DHCP 客户端服务还没起来也无所谓）。微软文档的说法是：用网卡做 iSCSI 启动时，**iBFT 必须在安装时和每次重启时都存在**——iPXE 的 `sanboot` 正好负责这件事（微软把这种归为“第三方程序生成 iBFT、通过 PXE 传给本机”的那类）。

**5）第一次启动**：用客户机的 iPXE `sanboot` 起（不是本地盘），能进系统就说明母盘成立。进系统后再做模板化收尾：

```cmd
powercfg /h off                          :: 关快速启动/休眠，避免克隆机被当成异常关机
sysprep /oobe /generalize /shutdown      :: 泛化：每台克隆机重新识别硬件、生成自己的 SID/机器名
```

关机后母盘就绪，放进 `images/` 即可。

#### Linux 母盘：装到 iSCSI 盘 + initramfs 里带 iSCSI

**1）装到 iSCSI 盘上**：iPXE 引导安装 ISO（或先 `sanhook` 挂好盘），安装器里先登录目标再选盘：

```bash
iscsiadm -m discovery -t sendtargets -p 10.1.1.1
iscsiadm -m node -T iqn.2026-07.storage:web-debian -p 10.1.1.1 --login
```

分区按固件来（BIOS→MBR+活动分区，UEFI→GPT+ESP），把系统装到这块盘。

**2）让 initramfs 支持从 iSCSI 启动**（关键，否则内核换成自己的驱动栈后找不到 root）：

- dracut（RHEL / Fedora / openSUSE）：内核参数加
  ```
  rd.neednet=1 rd.iscsi.firmware=1 ip=ibft
  ```
  `rd.iscsi.firmware=1` = 从 iPXE 提供的 iBFT 读 iSCSI 参数；也可以显式写
  `rd.iscsi.target.name=iqn.2026-07.storage:web-debian rd.iscsi.target.ip=10.1.1.1 rd.iscsi.target.port=3260 rd.iscsi.initiator=iqn.2026-07.storage:client`
- initramfs-tools（Debian / Ubuntu）：装 `open-iscsi`，内核参数用经典写法
  ```
  iscsi_target_name=iqn.2026-07.storage:web-debian iscsi_target_ip=10.1.1.1 iscsi_target_port=3260 iscsi_initiator=iqn.2026-07.storage:client
  ```
  然后 `update-initramfs -u`（dracut 用 `dracut -f`）。
- 客户机网卡驱动要在 initramfs 里：一般发行版自带；特殊网卡用 `dracut --add-drivers <模块>` 或写进 `/etc/initramfs-tools/modules`。

**3）系统里的收尾**：`fstab` 和内核 `root=` 用 **UUID**（`blkid` 查）；网络走 DHCP、别写死网卡名和静态 IP；清掉机器专属信息：

```bash
truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
```

**4）第一次启动**同样用客户机 iPXE `sanboot` 验证能进系统，再关机定稿。

#### 母盘自检

```bash
qemu-img info /home/prts/server/images/win11.raw   # raw 的 virtual size 就是客户机看到的盘大小
fdisk -l /home/prts/server/images/win11.raw        # 确认分区表、活动分区 / ESP
```

能不能真正启动，**只能用一台客户机走一遍 PXE/iSCSI 启动**来验证；用本机 `virt-install --import` 试没意义（本地盘没有 iSCSI 启动这条路，Windows 母盘还会因为启动网卡/发起端没接管而失败）。

#### 几个容易踩的坑

- **母盘只是模板**：客户机默认从它 reflink 出各自的叠加盘，谁都不写母盘；但“回写模式”和后台“iSCSI 挂载”是**直接写母盘**的，别拿唯一一份去试，留备份。
- **容量一次定好**：母盘多大，客户机看到的盘就多大（如 64G）；盘内别塞满，留空间给客户机自己用。
- **一种固件一张盘**：BIOS 母盘只能给 BIOS 客户机，UEFI 母盘只能给 UEFI 客户机。
- **装机时机器上别接本地盘**（微软 iSCSI Boot 文档明确写了：有本地盘时引导文件可能落到本地盘，做出来就是一块起不来的盘）。
- **网卡要匹配**：iSCSI 启动绑定安装时那块网卡，微软文档的说法是“**换网卡就得重装**”；所以母盘只部署到同型号网卡的机器，机型杂就把常用网卡驱动都注入进去。
- **启动网卡上别装网络过滤驱动**（VPN / 防火墙 / 带网络过滤的杀软），微软文档明确不支持。
- **iSCSI 启动不能休眠/睡眠**（微软文档），所以装完就 `powercfg /h off`。
- **装完别急着重启**：注册表（Windows）/ initramfs（Linux）没配好就重启，等于给自己造一块启动不了的盘。

### 4. dnsmasq 部署（DHCP + TFTP + iPXE 推送）

dnsmasq 一台机器同时干三件事：给客户机分配 IP、用 TFTP 把 **iPXE 引导器**推给客户机的网卡 PXE，iPXE 起来后再 chain 到本程序的 HTTP 接口取启动菜单。

**（1）先确认内网只有一个 DHCP**：同一广播域里如果有第二个 DHCP（systemd-networkd / NetworkManager 自带的 dnsmasq / isc-dhcp-server / kea / udhcpd / 上级路由器），会和你的 dnsmasq 抢答——客户机可能拿到别的网段 IP、拿不到 iPXE 引导文件，表现就是 PXE 启动时好时坏。按下面四步查一遍，确认应答的只有你的 dnsmasq（示例网卡 `enp3s0`、服务器 LAN IP `10.1.1.1`）：

```bash
# ① 本机谁在监听 67/UDP：应该只有你自己的 dnsmasq 一个
sudo ss -lunp | grep ':67'

# ② 本机有几个 dnsmasq 实例（NetworkManager 会另起一个，参数里带 --conf-file=/var/lib/NetworkManager/...）
ps -ef | grep '[d]nsmasq'

# ③ 其他 DHCP 服务是否开着；systemd-networkd 的 DHCPServer= 是否被打开
systemctl is-active isc-dhcp-server kea-dhcp4-server udhcpd 2>/dev/null
grep -rs 'DHCPServer' /etc/systemd/network/ /run/systemd/network/ 2>/dev/null

# ④ 广播域里到底有几台 DHCP 在应答（nmap / dhcpdump 需 apt 安装）
sudo nmap --script broadcast-dhcp-discover -e enp3s0   # 打印所有应答的 Server Identifier
sudo dhcpdump -i enp3s0                                # 实时看 DHCP 交互，含 option 54
sudo journalctl -u dnsmasq -f                          # 同时确认是你的 dnsmasq 在发 OFFER
```

**判定标准**：第 ④ 步只应出现一个 `Server Identifier`，且等于服务器 LAN IP（`10.1.1.1`），dnsmasq 日志里能看到对应 MAC 的 `DHCPDISCOVER`/`DHCPOFFER`（配置里加 `log-dhcp` 日志更全）。

查出来有第二个时，二选一处理：

- **关掉它**：路由器/交换机在管理页关 DHCP；`systemctl disable --now isc-dhcp-server`；NetworkManager 自带的把该连接改成静态（`nmcli con mod <连接名> ipv4.method manual`，别用 `ipv4.method shared`，它会在本机起一个 DHCP）；systemd-networkd 把对应 `.network` 里的 `DHCPServer=yes` 改成 `no`。
- **隔离开**：让客户机网络（接服务器 LAN 口的交换机）与上级路由器的 LAN 不在同一广播域（换网段 + VLAN 或物理分开），上级路由器 DHCP 就影响不到客户机。

另外服务器 LAN 口自己别再跑 DHCP 客户端（`ip addr` 显示的地址不该是 dynamic）：`nmcli con mod <连接名> ipv4.method manual` 或直接删掉该接口的 dhclient 配置。

**（2）把 iPXE 引导文件放进 TFTP 根目录**：

```bash
mkdir -p /srv/tftp
# Debian/Ubuntu 的 ipxe 包自带（装了 ipxe 就有）：
cp /usr/lib/ipxe/undionly.kpxe /srv/tftp/     # 传统 BIOS 客户机
cp /usr/lib/ipxe/ipxe.efi      /srv/tftp/     # UEFI 客户机
# 也可以自己编译：
#   git clone https://github.com/ipxe/ipxe && cd ipxe
#   make bin/undionly.kpxe            # BIOS
#   make bin-x86_64-efi/ipxe.efi      # UEFI
```

**（3）写 dnsmasq 配置**（`/etc/dnsmasq.d/pxe.conf`，把 IP / 网卡名换成自己的）：

```conf
# 只服务内网口（网卡名按实际改，别在 WAN 口开 DHCP）
interface=enp3s0
bind-dynamic
# 客户机地址池：起止 + 掩码 + 租期
dhcp-range=10.1.1.100,10.1.1.200,255.255.255.0,12h
# 客户机默认网关 = 服务器 LAN 口 IP（必须，联网控制依赖它）
dhcp-option=option:router,10.1.1.1
# 客户机 DNS：走服务器（dnsmasq 同时做 DNS 时）或直接给上游 DNS
dhcp-option=option:dns-server,10.1.1.1
# 上游 DNS：系统用了 systemd-resolved 时，/etc/resolv.conf 里只有 127.0.0.53（本地 stub），
# dnsmasq 不能拿它当上游（等于自己问自己，解析直接不工作）→ 必须读真实的上游文件
resolv-file=/run/systemd/resolve/resolv.conf
# TFTP 服务
enable-tftp
tftp-root=/srv/tftp
# 按客户机固件架构下发不同的 iPXE 引导器（!ipxe = 还没跑到 iPXE 的网卡 PXE）
dhcp-match=set:bios,option:client-arch,0
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efi64,option:client-arch,9
dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe
dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi
# 二次启动：已经是 iPXE 的客户机，直接把 HTTP 脚本地址当"引导文件"发下去
dhcp-userclass=set:ipxe,iPXE
dhcp-boot=tag:ipxe,http://10.1.1.1:5000/boot.ipxe
```

> `tag:!ipxe` 这个排除条件别省：网卡自带的 PXE 只认 TFTP、不认 HTTP URL，必须先把它引导到 iPXE，再由 iPXE 去取 `http://...` 脚本。

> `resolv-file` 这行也别省：Debian/Ubuntu 默认跑 systemd-resolved，`/etc/resolv.conf` 里只有一个 `nameserver 127.0.0.53`（本地 stub）。dnsmasq 拿它当上游就是自己问自己——`journalctl -u dnsmasq` 里会出现 `ignoring nameserver 127.0.0.53` 之类提示，客户机即使拿到 IP 也解析不了域名。除了指向 `/run/systemd/resolve/resolv.conf`，也可以改用 `no-resolv` + 直接写上游：`server=223.5.5.5`、`server=119.29.29.29`。

**（4）iPXE 推送流程（两跳）**：

1. **第一跳**：客户机网卡 PXE 向 dnsmasq 要 IP → 下发的引导文件是 `undionly.kpxe`（BIOS）或 `ipxe.efi`（UEFI）→ 客户机用 TFTP 取回并运行 iPXE。
2. **第二跳**：iPXE 会再发一次 DHCP，报上自己的 user-class `iPXE` → dnsmasq 命中 `tag:ipxe`，把**启动脚本 URL** `http://10.1.1.1:5000/boot.ipxe` 当引导文件发下去 → iPXE 用 HTTP 取回脚本并执行（本程序的 `/boot.ipxe` 会生成菜单，选择镜像后返回 `sanboot iscsi:...` 连盘启动）。

所以 DHCP 里配置的**唯一** chain 地址就是 `http(s)://<服务器IP>:5000/boot.ipxe`；若开了 `HTTPS_ENABLED`，这里要同步改成 `https://`（TLS 只影响 HTTP 这一段，TFTP 不受影响）。

> 如果 iPXE 没按 user-class 命中（老版本或想固定写死），可以给 iPXE 内置脚本：把 `#!ipxe`、`dhcp`、`chain http://10.1.1.1:5000/boot.ipxe` 三行存成 `embed.ipxe`，然后 `make bin/undionly.kpxe EMBED=embed.ipxe` 编译，用这个引导器替换 TFTP 根目录里的文件即可。

**（5）沿用现有 DHCP 服务器（不改 DHCP）**：让现有 DHCP 下发 `next-server=<服务器IP>` + `filename=undionly.kpxe`（BIOS）/ `ipxe.efi`（UEFI），服务器上只跑 TFTP（dnsmasq 可只开 TFTP：`port=0` 关 DNS、不配 `dhcp-range`），第二跳用上面的 `EMBED=` 内置脚本方式，或在支持按 user-class / option 77 下发的 DHCP 上照第（4）步配置。若现有 DHCP 完全不能改，也可让 dnsmasq 以 **proxy-DHCP** 模式只回答 PXE 引导选项（`dhcp-range=10.1.1.0,proxy`），IP 仍由原 DHCP 分配。

**（6）验证**：

```bash
dnsmasq --test                     # 校验配置语法
systemctl restart dnsmasq
journalctl -u dnsmasq -f           # 看 DHCP/TFTP 日志：DHCPDISCOVER / TFTP 传输
curl -s tftp://10.1.1.1/undionly.kpxe -o /dev/null && echo "TFTP OK"
dig @10.1.1.1 www.baidu.com +short   # 客户机 DNS 走服务器，这里能解析出来才算通
```

排查顺序：客户机是否拿到 IP（说明 DHCP 通）→ 是否取到 iPXE（TFTP 通）→ iPXE 是否拉到 `/boot.ipxe`（HTTP 5000 通）→ 是否连上 `3260` 的 iSCSI 盘。

### 5. Web 使用流程
浏览器访问 `http://<服务器IP>:8080/`：
- **管理员**：用户名 `admin` + 密码（默认 `admin123`，**部署前务必修改**）→ 管理后台（客户机名单 / 创建空白盘 / iSCSI 挂载 / 修改密码 / 用户与配额 / 默认配额 / 通用文件 / 联网控制）。
  - **iSCSI 挂载**页：选一张“空闲”的母盘点挂载 → 页面返回 IQN → 在任意机器上用 iSCSI 发起端连接该 IQN（服务器 IP:3260）即得到一块写直达母盘的可写盘；用完回后台点“卸载”。
- **普通用户**：先"注册账号"（用户名 + 密码）→ 登录 → 我的网盘：
  - 上传文件（单文件，受配额限制）、下载文件、新建文件夹；
  - 根目录可见"通用文件"文件夹（只读，内容由管理员维护）。

> Web 页面兼容 IE11（零 JS / 仅 ES5、单文件上传、表格布局，无 flex/grid）。

### 6. iPXE 使用流程
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

## 七、已知限制

- `images/` 母盘目录不会自动创建，需手动放置 `.raw`。
- 母盘 `images/*.raw` 会被视为启动菜单镜像；网盘数据在 `cloud/` 下，与母盘隔离。
- Web 登录目前仅 `sleep(1)` 防爆破，无 IP 级窗口限速。

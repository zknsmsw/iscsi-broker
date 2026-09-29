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
- 被禁机器仍可 PXE/iPXE 无盘启动并使用 iSCSI 盘（只禁外网，不碰服务器自身服务）；客户机互访不受影响；MAC 可被伪造（二层局域网通病）；
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

> 除 `images/` 外全部自动创建；`images/` 要手动建并放入母盘 `.raw`（不放则启动菜单显示“无镜像”）。

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

- **客户机**：不接外网，只跟服务器打交道——DHCP 要 IP 和引导文件、TFTP 取 iPXE、HTTP 5000 取启动脚本、3260 连 iSCSI 盘。
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

“制作母盘”= 把系统**装进一块整盘 raw**，并让它在 iSCSI 上能自己启动：

- 固件要匹配：BIOS→**MBR+活动分区**，UEFI→**GPT+ESP**；
- Windows 必须用**官方安装程序直接装到 iSCSI 盘上**（盘由 iPXE 通过 iBFT 交给 Setup），装完系统里才会有 iSCSI 启动的登记；用 Dism++/dism 释放镜像装出来起不来。也不能先装到本地盘再转过来；
- iPXE 交棒后盘由**系统自己的 iSCSI 发起端**接管，所以要让 iSCSI 发起端随内核启动（Windows 起不来时查 `msiscsi` 的 `Start`，Linux 配 initramfs），否则卡 0x7B / `INACCESSIBLE_BOOT_DEVICE` / initramfs 找不到 root。

#### Windows 母盘：PE 里装到 iSCSI 盘

**1）准备 PE（必须是带网卡驱动的 PE）**：官方 `boot.wim` 在 KVM/Proxmox 的 virtio 网卡上没有驱动，PE 里连不到 iSCSI 盘，所以用 **FirPE** / **LefPE for KVM** 这类，**把它的 ISO 挂到客户机（虚拟机）的光驱**——不用提 WIM、不用 wimboot、也不用另起 HTTP 服务。

**2）后台创建并挂载母盘**：Web 后台「创建空白盘」填名字和大小（如 `win11` / `64G`）→ 到「iSCSI 挂载」页把它挂载，页面给出的 IQN 就是下面要挂的目标。

**3）iPXE 里把要装的盘挂成 0x80，然后 `exit`，让 BIOS 接着从光驱启动 PE**：

```
dhcp
sanhook --drive 0x80 iscsi:10.1.1.1:::1:iqn.2026-07.storage:web-win11
exit
```

- `exit` = 退出 iPXE、继续走虚拟机的启动顺序，所以虚拟机的启动顺序要设成 **网络 → 光驱**。
- PE 不能占 SAN 盘：走菜单进 PE 会让 PE 自己占一块 SAN 盘，和要装的盘冲突（抢盘号报 `0x032320`，错开又读盘 I/O 错 `0x1d8520`）。
- 不用在注册表里配 IP / 网关 / DNS：iPXE 已经 DHCP 好，并通过 iBFT 一起交给 Windows。

**4）进 PE 后确认盘、跑官方安装程序**：

```cmd
net start msiscsi            :: 服务起来后，磁盘管理里应能看到那块 iSCSI 盘（iBFT 给的）
```

然后挂上 Windows 安装 ISO，跑它的 `sources\setup.exe`，选那块盘安装（BIOS 建 MBR+活动分区，UEFI 建 GPT+ESP）。

- **只能用官方安装程序装**：别用 Dism++/dism 释放 `install.wim` 造这块盘——不经 Setup 就不会登记 iSCSI 启动信息，装出来起不来。母盘做完要批量发，再 `sysprep` + 捕获镜像，且只能发给同型号网卡的机器。
- 用 `iscsicli`（QAddTargetPortal → ListTargets → QLoginTarget）手动连上来的盘能看见，但 Setup 会拒装（报"硬件可能不支持启动到此磁盘"）——所以盘必须由 iPXE sanhook 挂、带 iBFT。

**5）装完先别重启，在 PE 里改两处注册表**：

```cmd
reg load HKLM\OFF C:\Windows\System32\config\SYSTEM

:: ① 关掉页面文件：iSCSI 盘上放 pagefile，Win10 第一次启动会 PAGE_FAULT_IN_NONPAGED_AREA 蓝屏
reg delete "HKLM\OFF\ControlSet001\Control\Session Manager\Memory Management" /v PagingFiles /f
reg delete "HKLM\OFF\ControlSet001\Control\Session Manager\Memory Management" /v ExistingPageFiles /f

:: ② iSCSI 发起端随内核启动（默认 3=按需；官方安装程序一般已登记好，起不来再改）
reg add "HKLM\OFF\ControlSet001\Services\msiscsi" /v Start /t REG_DWORD /d 0 /f

reg unload HKLM\OFF
```

- 页面文件那条是**必做**（等价于 PE 里 regedit 把 `PagingFiles` 清空并删掉 `ExistingPageFiles`）；客户机有本地盘的话，更好的做法是把页面文件放本地盘：`PagingFiles` 写成 `D:\pagefile.sys 4096 8192`。
- 还起不来，再把 iBFT 里那块网卡的驱动也设成 `Start=0`。服务名查法：PE 里 `wmic nic where "NetEnabled=true" get Name,ServiceName`；或离线 `reg query "HKLM\OFF\ControlSet001\Enum\PCI" /s /v Driver | findstr /i 4d36e972`，再看该设备实例的 `Service`（本机实测网卡 `rtwlane6` 就是这么查出来的）。

**6）回后台「iSCSI 挂载」页点卸载**，把这块盘从 iSCSI 上摘下来。

**7）第一次启动用 Admin Mode（回写模式）**：普通菜单启动会给客户机生成叠加盘，第一次是要写母盘本身，所以 iPXE 菜单里选 **Admin Mode**（输 admin 密码）→ 选 `win11` 启动，把系统配置做完（装软件、驱动、系统设置），然后关机：

```cmd
powercfg /h off                          :: iSCSI 启动不能休眠/睡眠，关掉快速启动
```

母盘就绪。之后客户机照常用普通模式启动，就是从这块母盘克隆的叠加盘，改动不落母盘。

#### Linux 母盘：装到 iSCSI 盘 + initramfs 里带 iSCSI

**1）后台创建并挂载母盘**：Web 后台「创建空白盘」（如 `debian` / `32G`）→「iSCSI 挂载」页挂载拿到 IQN。

**2）装到这块盘上**：iPXE 引导安装 ISO 进安装器，先登录目标再选盘：

```bash
iscsiadm -m discovery -t sendtargets -p 10.1.1.1
iscsiadm -m node -T iqn.2026-07.storage:web-debian -p 10.1.1.1 --login
```

分区按固件来（BIOS→MBR+活动分区，UEFI→GPT+ESP），把系统装到这块盘。

**3）让 initramfs 支持从 iSCSI 启动**：

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

**4）系统里的收尾**：`fstab` 和内核 `root=` 用 **UUID**（`blkid` 查）；网络走 DHCP、别写死网卡名和静态 IP；清掉机器专属信息：

```bash
truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
```

**5）回后台「iSCSI 挂载」页点卸载。**

**6）第一次启动用 Admin Mode（回写模式）**：iPXE 菜单选 Admin Mode（输 admin 密码）→ 选 `debian` 启动，把要装的东西配好，再关机。之后客户机用普通模式启动就是从这块母盘克隆的叠加盘。

#### 母盘自检

```bash
qemu-img info /home/prts/server/images/win11.raw   # raw 的 virtual size 就是客户机看到的盘大小
fdisk -l /home/prts/server/images/win11.raw        # 确认分区表、活动分区 / ESP
```

能不能启动，只能用一台客户机走一遍 PXE/iSCSI 启动来验证。

#### 几个容易踩的坑

- **母盘只是模板**：客户机默认从它 reflink 出各自的叠加盘，谁都不写母盘；但“回写模式”和后台“iSCSI 挂载”是**直接写母盘**的，别拿唯一一份去试，留备份。
- **容量一次定好**：母盘多大，客户机看到的盘就多大（如 64G）；盘内别塞满，留空间给客户机自己用。
- **一种固件一张盘**：BIOS 母盘只能给 BIOS 客户机，UEFI 母盘只能给 UEFI 客户机。
- **装机时机器上别接本地盘**：有本地盘时引导文件可能落到本地盘，做出来就是一块起不来的盘。
- **网卡要匹配**：iSCSI 启动绑定安装时那块网卡，换网卡就得重装；机型杂就把常用网卡驱动都注入进去。
- **PE 要带客户机的网卡驱动**：官方 `boot.wim` 在 KVM/Proxmox 的 virtio 网卡上没有驱动，装系统要用 FirPE / LefPE for KVM 这类（ISO 挂虚拟机光驱即可）。
- **PE 不能占 SAN 盘**：走菜单进 PE 会让 PE 自己占一块 SAN 盘，和要装的盘冲突（`0x032320` 盘号冲突 / `0x1d8520` 读盘 I/O 错），所以 PE 从光驱起（iPXE 里 `sanhook` 后直接 `exit`）。
- **启动网卡上别装网络过滤驱动**（VPN / 防火墙 / 带网络过滤的杀软）。
- **页面文件不能放 iSCSI 盘**：Win10（1703 起）第一次启动会 `PAGE_FAULT_IN_NONPAGED_AREA` 蓝屏，装完在 PE 里就把 `PagingFiles` 清空（有本地盘就放到本地盘）。
- **iSCSI 启动不能休眠/睡眠**，装完就 `powercfg /h off`。

### 4. dnsmasq 部署（DHCP + TFTP + iPXE 推送）

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
# 上游 DNS：systemd-resolved 环境下 /etc/resolv.conf 只有 127.0.0.53，必须显式读真实上游
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

> `resolv-file` 这行别省：systemd-resolved 环境下 dnsmasq 拿 127.0.0.53 当上游就是自己问自己；也可以改用 `no-resolv` + `server=223.5.5.5`。

**（4）流程**：网卡 PXE 从 DHCP 拿到 `undionly.kpxe` / `ipxe.efi`（TFTP）→ iPXE 再发一次 DHCP（user-class `iPXE`）拿到 `http://10.1.1.1:5000/boot.ipxe` 并执行。所以 DHCP 里**唯一**的 chain 地址就是 `http(s)://<服务器IP>:5000/boot.ipxe`（开了 `HTTPS_ENABLED` 就改 https，TFTP 那段不受影响）。

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

#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse, subprocess, os, sys, datetime, hashlib, threading, glob, time, re, secrets, html, ssl, json, base64
import users_auth, cloud_store, netctrl, agent_hub, webdav, wsbridge, client_release  # 本地模块：多账号认证（users_auth）+ 个人网盘（cloud_store）+ 联网控制（netctrl）+ 客户机 agent 通道（agent_hub）+ 网盘 WebDAV（webdav）+ VNC WebSocket 桥（wsbridge）+ 客户机发布库（client_release）

# ========== 数据根目录 BASE_DIR ==========
# 优先级：环境变量 ISCSI_BROKER_BASE_DIR > /etc/iscsi-broker/iscsi-broker.env > 下面的默认值。
# install.sh 会把实际路径写进那个 env 文件、由 systemd 的 EnvironmentFile 注入，
# 所以从 git 拉下来的源码不用手改（升级/pull 也不会冲突）。
# 注意：这里放的是**数据**（母盘 images/、网盘 cloud/、配置）；程序目录是另一个地方
# （install.sh 默认 /opt/iscsi-broker），两者不能是同一个目录——否则升级/卸载会误伤数据。
_DEFAULT_BASE_DIR = "/opt/iscsi-broker-data"   # 大盘可换：--base-dir /srv/iscsi 等
_ENV_FILE = "/etc/iscsi-broker/iscsi-broker.env"

def _resolve_base_dir():
    env = os.environ.get("ISCSI_BROKER_BASE_DIR", "").strip()
    if env:
        return env
    try:
        with open(_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ISCSI_BROKER_BASE_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    except OSError:
        pass
    return _DEFAULT_BASE_DIR

BASE_DIR = _resolve_base_dir()
# =========================================

PORT = 5000
IMAGES_DIR = os.path.join(BASE_DIR, "images")  # 存放多个母盘 xxx.raw 的目录（数据结构保持不变）
DEFAULT_IMAGE = "win11"  # 菜单默认高亮/超时后自动选择的镜像名（不含 .raw 后缀）
OVERLAY_DIR = BASE_DIR   # 叠加盘存放目录（reflink 模式会在这里生成 xxx.raw）
NBD_MAX = 16
TID_BASE = 10
TID_SPACE = 1_000_000   # tid 槽位数：原 800 在 30 台时碰撞概率约 42%，碰撞会互删 target 掉盘；tgt 的 tid 是 uint32，1e6 空间 30 台碰撞概率约 0.04%

# =================== 工作模式 ===================
# 保持 ~/server/images 下 .raw 母盘不变，其余全部可改。
# 启动时自动选择最佳路线（日志会打印当前模式）：
#
#   路线 A（推荐，reflink 直出）—— 当 ~/server 所在文件系统支持 reflink（XFS/btrfs）时自动启用：
#       每台客户机用 `cp --reflink=always` 秒级生成一份 raw 叠加盘（文件系统级写时复制，
#       母盘永远只读不会被写脏），然后 tgt 直接把这份 raw 文件导出成 iSCSI 盘。
#       彻底去掉 qemu-nbd 进程和 qcow2 元数据：路径从
#          客户端 -> tgt -> /dev/nbdX -> qemu-nbd(用户态) -> qcow2 -> 母盘
#       缩短为
#          客户端 -> tgt -> raw 叠加盘 -> 母盘(共享块)
#       客户机 IOPS 最高、服务器 CPU 最低。
#
#   路线 B（回退，qcow2 叠加盘）—— 文件系统不支持 reflink（如 ext4）时自动启用：
#       沿用原来的 qemu-nbd + qcow2 结构，但带上前一版全部优化参数。
#
# 强制指定模式：auto / reflink / qcow2
FORCE_MODE = "auto"
# reflink 模式下是否启用 thin(TRIM)：默认关闭。TRIM 打到与母盘共享的 extent 会触发
# unshare(CoW 实体化)，不回收空间反而写放大；叠加盘空闲 5 分钟即删，回收无意义。
THIN_ON_REFLINK = False
# ================================================

# =================== 管理员模式配置 ===================
# 启动菜单里新增“管理员模式”入口，进入时需要输入下面的密码。
# 注意：密码会以明文拼进 HTTP URL（局域网 PXE 场景），因此
# 只允许字母/数字/下划线/短横线，避免破坏链接，也请勿设成弱密码。
ADMIN_PASSWORD = "admin123"   # TODO: 部署前请改成你自己的密码
BLANK_SIZE_MAX = 1 << 40      # 创建空白硬盘的大小上限（默认 1TiB，单位字节）
# =====================================================

# =================== Web 管理后台配置 ===================
WEB_ENABLED = True            # 是否启用简易 Web 管理后台（改密码/建空白盘/看客户机）
WEB_PORT = 8080               # 后台端口，浏览器访问 http://<服务器IP>:8080/
WEB_TITLE = "iSCSI Broker 管理后台"
ADMIN_PASS_FILE = os.path.join(OVERLAY_DIR, "admin.conf")  # 管理员密码持久化文件（iPXE 与 Web 共用，可改）
WEB_VNC_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "vnc"))
# 内置 noVNC（VNC 控制页）静态资源目录，与主程序同级的 web/vnc/；缺失时只有 VNC 页面不可用
# ========================================================

# =================== 可选 HTTPS ===================
# 配置证书路径并置 HTTPS_ENABLED=True 后，两个 HTTP 服务（iPXE 供给 / Web 后台）都启用 TLS。
# 注意：PXE 场景下 DHCP 下发的首个 chain URL 也要相应改成 https://，本脚本只负责后续链接。
HTTPS_ENABLED = False
HTTPS_CERT = ""   # 例如 /etc/letsencrypt/live/example.com/fullchain.pem
HTTPS_KEY = ""    # 例如 /etc/letsencrypt/live/example.com/privkey.pem
# ===================================================

# =================== 联网控制（按 MAC 控制客户机上网） ===================
# 服务器担任客户机网关（FORWARD 转发 + MASQUERADE）。本功能在 FORWARD 最前面挂
# 专用链 NETCTRL，按客户机 MAC 放行/拒绝“内网→外网”流量；客户机开机自动建规则、
# 关机规则空转、巡检自动清理，Web 后台可设默认行为与逐机开关。
NETCTRL_ENABLED = True            # 是否启用联网控制
NETCTRL_LAN_IF = ""               # 内网卡（接交换机）；留空自动探测（带默认路由的网卡=外网卡）
NETCTRL_WAN_IF = ""               # 外网卡（接外网）；留空自动探测
NETCTRL_FULL_TAKEOVER = True      # True=启动时清空并重建 FORWARD/POSTROUTING（旧的转发/NAT 手工规则被替换）
                                  # False=只管理自己的 NETCTRL 链，不碰其他规则（与现有规则共存）
NETCTRL_REJECT = True             # True=REJECT（客户机立即报“无法连接”）；False=DROP（静默丢弃，卡到超时才失败）
NETCTRL_RECONCILE_INTERVAL = 30   # 规则巡检间隔（秒）：自动补齐新开机客户机规则、清理离线机器
# ========================================================================

# ------- 以下配置只影响“路线 B（qcow2 回退）”；路线 A 自动使用最合适的设置 -------
QCOW2_CLUSTER_SIZE = "64K"        # qcow2 簇大小（默认 64K），与默认一致，无副作用
QCOW2_LAZY_REFCOUNTS = True       # 延迟引用计数：减少元数据落盘，保留（有利无害）
QCOW2_PREALLOC_METADATA = False   # 预留开关，默认关闭

# ---- 以下参数经实测回退到与原版一致的安全默认值 ----
# 教训：HDD/ZFS 环境下，任何"绕过页缓存"的改动都会让读性能大幅下降。
NBD_CACHE = "writeback"           # 写回缓存：写请求先确认到内存，有利无害，保留
NBD_AIO = "native"                # 仅当 qemu 支持才用；不支持会自动降级跳过
NBD_DISCARD = "ignore"            # ignore=关闭 TRIM 下传（原版默认）。叠加盘明显膨胀时可改 "unmap"
NBD_DETECT_ZEROES = "off"         # off=关闭零写检测（原版默认）。on 会给每次写加内存比较，写入类跑分略降
NBD_NUM_QUEUES = 1                # NBD 多队列数，HDD 环境 1 即可

# 保持 tgt 默认缓冲 I/O（留空 = 不加 --bsoflags）。
# 曾改为 direct(O_DIRECT)，但 O_DIRECT 会绕过页缓存和预读，客户机跑分直接减半，已回退。
TGT_BSOFLAGS = ""

APPLY_SYSCTL_TUNING = True        # 启动时做保守的 TCP/iSCSI 网络缓冲调优（提吞吐、降重传）
SYSCTL_KV = {
    "net.core.rmem_max": "16777216",
    "net.core.wmem_max": "16777216",
    "net.ipv4.tcp_rmem": "4096 87380 16777216",
    "net.ipv4.tcp_wmem": "4096 65536 16777216",
    # TCP keepalive 调短：客户机断电/关机后，死连接（半开）更快被内核回收，
    # 让 tgt 更早感知断开（对启用了 SO_KEEPALIVE 的 socket 生效；不生效也无害）
    "net.ipv4.tcp_keepalive_time": "60",
    "net.ipv4.tcp_keepalive_intvl": "10",
    "net.ipv4.tcp_keepalive_probes": "6",
}
# ===================================================================================

mac_locks = {}
mac_state = {}  # mac -> {"nbd_dev":..., "overlay":...} 记录每个 mac 当前占用的资源，供重连清理和空闲巡检使用
writeback_active = {}  # img_name -> mac：正以“回写模式”直接使用该母盘的客户机。同一母盘同一时刻只允许一台回写，防止互相覆盖写坏母盘
global_lock = threading.Lock()

# =================== 后台手动 iSCSI 挂载（母盘直出，写直达母盘） ===================
# 与 iPXE 无盘/回写流程互相独立但互斥：Web 后台把一张“未在使用的镜像”直接导出为
# iSCSI target（等同回写语义，客户机写入直达母盘 .raw），页面显示 IQN 供任意
# iSCSI 发起端（Windows 发起程序 / iscsiadm）手动连接。同一镜像同一时刻只能有
# 一种使用方式：回写、被客户机做叠加盘、后台手动挂载，三者互斥。
WEB_EXPORT_TID_BASE = 2_000_000   # 手动挂载 tid 起点：与 PXE 客户机 tid 区间
                                  # [TID_BASE, TID_BASE + TID_SPACE)（即 10 ~ 1_000_010）
                                  # 完全错开——PXE 流程按 stable_tid 删 target 永远不会误删手动挂载盘
WEB_EXPORT_TID_SLOTS = 4096       # 手动挂载并发槽位数（服务重启后全部失效，足够日常使用）
web_export_lock = threading.Lock()
web_exported = {}  # img_name -> {"tid","iqn","path","size","created"}（内存态，重启清空）

IDLE_CHECK_INTERVAL = 30    # 每隔多少秒巡检一次
IDLE_TIMEOUT_SECONDS = 300  # target 连续空闲(无 I_T nexus)超过这个时长就自动清理，5分钟
# 回写锁的宽限期：回写母盘的机器一旦没有 I_T nexus（iSCSI 会话断了：正常关机/重启/
# 拔电），就没有任何人在写母盘了，必须尽快把“回写占用”放开，否则母盘会被一台已经
# 关机的机器锁住，别的机器既不能回写也不能后台挂载。45 秒 ≫ 发起端重连时间，
# 又远小于 IDLE_TIMEOUT_SECONDS（target 本身仍按 5 分钟回收，避免把临时掉线、
# 马上要重连的机器的盘删掉）。
WRITEBACK_LOCK_GRACE = 45
MIN_FREE_GB = 5   # OVERLAY_DIR 剩余空间低于该值(GB)时拒绝新 provision

# ---- 无流量检测：识别"客户机不主动断开 iSCSI"的关机/断电（纯服务端，Windows/Linux 通用）----
# iSCSI 协议自带保活：Linux open-iscsi 默认每 5 秒发一次 NOP-Out，Windows 也会周期性发包。
# 因此"长时间没收到客户端任何包"≈客户端失联。用 ss -tni 的 lastrcv（内核级，距最后收包秒数）
# 观测，再用 arping 做 L2 层二次确认（活着必答、防火墙拦不住 ARP），防止误删"空闲但活着"的机器。
IDLE_NO_TRAFFIC_SECONDS = 300   # 距客户端最后一个包超过该秒数 → 进入可疑
IDLE_NO_TRAFFIC_CONFIRM = 2     # 连续 N 次巡检仍可疑才处理（防抖动）
IDLE_NO_TRAFFIC_HARD = 1800     # arping 不可用时，无包超过该秒数才允许删除（保守兜底）
IDLE_ARP_TRIES = 2              # arping 探测次数
IDLE_ARP_TIMEOUT = 3            # arping 单次超时（秒）


# ---------- 兼容性检测 ----------
def qemu_nbd_flags_supported():
    """检测 qemu-nbd 支持哪些性能参数，并确认 native AIO 所需的 libaio 存在。"""
    supported = set()
    try:
        out = subprocess.run(["qemu-nbd", "--help"], capture_output=True, text=True, timeout=5).stdout
        if "--cache" in out:
            supported.add("cache")
        if "--aio" in out:
            supported.add("aio")
        if "--discard" in out:
            supported.add("discard")
        if "--detect-zeroes" in out:
            supported.add("detect_zeroes")
        if "--num-queues" in out:
            supported.add("num_queues")
    except Exception:
        pass
    if "aio" in supported:
        try:
            out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=5).stdout
            if "libaio.so" not in out:
                supported.discard("aio")
        except Exception:
            supported.discard("aio")
    return supported


def qemu_img_create_opts_supported():
    """检测 qemu-img create 支持哪些 -o 选项。"""
    supported = set()
    try:
        r = subprocess.run(["qemu-img", "create", "-f", "qcow2", "-o", "help"],
                           capture_output=True, text=True, timeout=5)
        text = r.stdout + r.stderr
        if "lazy_refcounts" in text:
            supported.add("lazy_refcounts")
        if "preallocation" in text:
            supported.add("preallocation")
        if "cluster_size" in text:
            supported.add("cluster_size")
    except Exception:
        pass
    return supported


def detect_reflink_support():
    """在 IMAGES_DIR -> OVERLAY_DIR 之间做一次真实的 reflink 测试。
    只在能成功复制的文件系统（XFS/btrfs）上才返回 True，避免跨文件系统误判。"""
    src, dst = None, None
    try:
        imgs = list_images()
        if imgs:
            src = os.path.join(IMAGES_DIR, f"{imgs[0]}.raw")
        else:
            src = os.path.join(OVERLAY_DIR, ".reflink_src_test")
            with open(src, "wb") as f:
                f.write(b"\0" * 4096)
        dst = os.path.join(OVERLAY_DIR, ".reflink_dst_test")
        if os.path.exists(dst):
            try: os.remove(dst)
            except Exception: pass
        r = subprocess.run(["cp", "--reflink=always", src, dst],
                           capture_output=True, text=True, timeout=60)
        ok = (r.returncode == 0) and os.path.exists(dst)
    except Exception:
        ok = False
    finally:
        for p in (src, dst):
            if p and os.path.basename(p).startswith(".reflink"):
                try:
                    if os.path.exists(p): os.remove(p)
                except Exception:
                    pass
    return ok


QNBD_SUPP = qemu_nbd_flags_supported()
QIMG_SUPP = qemu_img_create_opts_supported()


def get_fs_type():
    try:
        r = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", OVERLAY_DIR],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_qemu_img_create_cmd(base_img, overlay):
    """按检测结果拼 qemu-img create 命令（路线 B）。"""
    parts = []
    if "cluster_size" in QIMG_SUPP:
        parts.append(f"cluster_size={QCOW2_CLUSTER_SIZE}")
    if QCOW2_LAZY_REFCOUNTS and "lazy_refcounts" in QIMG_SUPP:
        parts.append("lazy_refcounts=on")
    if QCOW2_PREALLOC_METADATA and "preallocation" in QIMG_SUPP:
        parts.append("preallocation=metadata")
    cmd = ["qemu-img", "create", "-f", "qcow2", "-b", base_img, "-F", "raw"]
    if parts:
        cmd += ["-o", ",".join(parts)]
    cmd.append(overlay)
    return cmd


_NBD_FLAG_TO_KEY = {
    "--cache": "cache",
    "--aio": "aio",
    "--discard": "discard",
    "--detect-zeroes": "detect_zeroes",
    "--num-queues": "num_queues",
}


def connect_nbd(nbd_dev, overlay, timeout=8):
    """连接 qemu-nbd，带参数降级重试：
    完整优化参数 -> 去掉 native AIO -> 最简参数 -> 纯基础连接。
    避免因某个参数在当前 qemu 构建里不支持（如未编译 libaio）导致整台机器起不来。
    降级成功后会把不支持的参数记下来，后续机器直接跳过，不再重试。"""
    base = []
    if "cache" in QNBD_SUPP and NBD_CACHE:
        base.append(("--cache", NBD_CACHE))
    if "aio" in QNBD_SUPP and NBD_AIO:
        base.append(("--aio", NBD_AIO))
    if "discard" in QNBD_SUPP and NBD_DISCARD and NBD_DISCARD != "ignore":
        base.append(("--discard", NBD_DISCARD))
    if "detect_zeroes" in QNBD_SUPP and NBD_DETECT_ZEROES and NBD_DETECT_ZEROES != "off":
        base.append(("--detect-zeroes", NBD_DETECT_ZEROES))
    if "num_queues" in QNBD_SUPP and NBD_NUM_QUEUES > 1:
        base.append(("--num-queues", str(NBD_NUM_QUEUES)))
    no_aio = [(k, v) for k, v in base if k != "--aio"]
    minimal = [(k, v) for k, v in no_aio if k in ("--cache", "--discard")]
    variants = [base, no_aio, minimal, []]
    seen, tried = set(), []
    for variant in variants:
        key = tuple(variant)
        if key in seen:
            continue
        seen.add(key)
        args = ["qemu-nbd", "-c", nbd_dev]
        for k, v in variant:
            args += [k, v]
        args += ["-f", "qcow2", overlay]
        try:
            run_cmd(args, timeout=timeout)
            if variant and tuple(variant) != tuple(base):
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                dropped = set(base) - set(variant)
                names = []
                for k, _ in dropped:
                    names.append(k)
                    supp_key = _NBD_FLAG_TO_KEY.get(k)
                    if supp_key:
                        QNBD_SUPP.discard(supp_key)  # 记住，本会话内不再尝试该参数
                print(f"[{now}] [WARN] qemu-nbd 降级参数重试成功，跳过了：{' '.join(names)}")
            return True
        except Exception as e:
            tried.append(str(e))
    raise RuntimeError("qemu-nbd connect failed: " + " | ".join(tried[-2:]))


def tgt_new_lun(tid, backing, bsoflags=None):
    """创建 LUN；bsoflags 为 None 时不传（用 tgt 默认缓冲 I/O）。"""
    args = ["tgtadm", "--lld", "iscsi", "--mode", "logicalunit", "--op", "new",
            "--tid", str(tid), "--lun", "1", "--backing-store", backing]
    if bsoflags:
        args += ["--bsoflags", bsoflags]
    run_cmd(args)


def tgt_bind_target(tid):
    run_cmd(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "bind",
             "--tid", str(tid), "--initiator-address", "ALL"])


def tgt_new_target(tid, iqn):
    run_cmd(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "new",
             "--tid", str(tid), "--targetname", iqn])


def tgt_enable_thin(tid):
    """让 Windows 认为该盘支持瘦供给，可下发 TRIM 回收空间（尽力而为）。"""
    try:
        subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "logicalunit", "--op", "update",
                        "--tid", str(tid), "--lun", "1", "--params", "thin_provisioning=1"],
                       stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


def tune_sysctl():
    """对 iSCSI/TCP 网络缓冲做保守调优，提升吞吐、减少丢包重传（尽力而为）。"""
    if not APPLY_SYSCTL_TUNING:
        return
    for key, val in SYSCTL_KV.items():
        try:
            subprocess.run(["sysctl", "-w", f"{key}={val}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            pass
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [INFO] Sysctl tuning applied.")

def _maybe_wrap_tls(server):
    """HTTPS_ENABLED 且证书路径存在时，将 server 的 socket 包上 TLS；否则原样返回。"""
    if not (HTTPS_ENABLED and HTTPS_CERT and HTTPS_KEY):
        return server
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(HTTPS_CERT, HTTPS_KEY)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


def tune_nbd_dev(nbd_dev):
    """NBD 设备调度器设 none，减少内核调度开销（尽力而为，失败不影响）。"""
    try:
        with open(f"/sys/block/{os.path.basename(nbd_dev)}/queue/scheduler", "w") as f:
            f.write("none")
    except Exception:
        pass


def get_mac_lock(mac):
    with global_lock:
        if mac not in mac_locks:
            mac_locks[mac] = threading.Lock()
        return mac_locks[mac]

def stable_tid(mac: str) -> int:
    # blake2s 替代 md5（FIPS 环境可能禁用 md5）；[:8] 取 32 位哈希映射到 TID_SPACE
    digest = hashlib.blake2s(mac.encode()).hexdigest()
    return TID_BASE + (int(digest[:8], 16) % TID_SPACE)

nbd_alloc_lock = threading.Lock()
nbd_in_use = set()

def find_free_nbd():
    """在锁内分配空闲 NBD 设备，避免两台客户机并发抢到同一个 /dev/nbdX。"""
    with nbd_alloc_lock:
        try:
            with open("/proc/mounts") as f:
                mounts = f.read()
        except Exception:
            mounts = ""
        for i in range(NBD_MAX):
            dev = f"/dev/nbd{i}"
            try:
                if dev in nbd_in_use:
                    continue
                if dev in mounts:
                    continue
                size_path = f"/sys/block/nbd{i}/size"
                if os.path.exists(size_path):
                    with open(size_path) as sf:
                        if sf.read().strip() != "0":
                            continue
                nbd_in_use.add(dev)
                return dev
            except Exception:
                continue
    raise RuntimeError("No free NBD device")

def release_nbd(dev):
    """释放 NBD 设备占用标记（幂等，可安全重复调用）。"""
    if not dev:
        return
    with nbd_alloc_lock:
        nbd_in_use.discard(dev)

def list_images():
    """扫描 IMAGES_DIR 下所有 .raw 文件，返回不含后缀的镜像名列表（按字母排序）"""
    if not os.path.isdir(IMAGES_DIR):
        return []
    names = [os.path.splitext(f)[0] for f in os.listdir(IMAGES_DIR) if f.endswith(".raw")]
    return sorted(names)

def parse_size_to_bytes(s):
    """把 '10G' / '500M' / '1048576' 这类大小字符串转成字节数；不合法返回 None。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPEkmgtpe]?)", s.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30,
            "T": 1 << 40, "P": 1 << 50, "E": 1 << 60}.get(unit, 1)
    return int(num * mult)

def wait_nbd_ready(nbd_dev, retries=20, interval=0.1):
    """qemu-nbd -c 返回后内核可能还没同步好设备大小，这里轮询直到 size 非 0 再继续，
    避免 tgtadm 创建 LUN 时读到 0 大小导致后续无法正常使用。"""
    devname = os.path.basename(nbd_dev)
    size_path = f"/sys/block/{devname}/size"
    for _ in range(retries):
        try:
            with open(size_path) as sf:
                if sf.read().strip() not in ("0", ""):
                    return True
        except FileNotFoundError:
            pass
        time.sleep(interval)
    return False

def run_cmd(args, timeout=5):
    """执行命令；失败时抛出带真实报错信息的异常，方便定位。"""
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        snippet = " | ".join(detail[-3:]) if detail else ""
        raise RuntimeError(f"{os.path.basename(args[0])} failed (rc={e.returncode}): {snippet[:400]}")
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} not found")

def ensure_overlay_space():
    """provision 前检查 OVERLAY_DIR 剩余空间，避免叠加盘写满后客户机 IO 报错（尽力而为，失败不阻断）。"""
    try:
        st = os.statvfs(OVERLAY_DIR)
        free_gb = st.f_bavail * st.f_frsize / (1 << 30)
    except Exception:
        return
    if free_gb < MIN_FREE_GB:
        raise RuntimeError(f"insufficient free space on {OVERLAY_DIR}: {free_gb:.1f} GiB (min {MIN_FREE_GB} GiB)")

def init_cleanup():
    try:
        out = subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "show"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            if "Target" in line and "iqn" in line:
                tid = line.split()[1].strip(":")
                subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                "--force", "--tid", tid],
                               stderr=subprocess.DEVNULL, timeout=3)
    except:
        pass
    for nbd_dev in glob.glob("/dev/nbd*"):
        subprocess.run(["qemu-nbd", "-d", nbd_dev],
                       stderr=subprocess.DEVNULL, timeout=3)
    # 两种叠加盘后缀都清理（reflink 模式 .raw / qcow2 模式 .qcow2）
    for f in glob.glob(f"{OVERLAY_DIR}/overlay_*.qcow2") + glob.glob(f"{OVERLAY_DIR}/overlay_*.raw"):
        try:
            os.remove(f)
        except:
            pass
    print(f"[{datetime.datetime.now()}] [INIT] Cleanup done.")

def parse_idle_tids():
    """解析 tgtadm 输出，返回 {tid: mac} 的字典，仅包含当前没有任何 I_T nexus（无客户端连接）的 target"""
    try:
        out = subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "show"],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return {}

    idle = {}
    # 按 "Target N: iqn..." 切分成每个 target 的文本块
    blocks = re.split(r"(?=^Target \d+: )", out.stdout, flags=re.MULTILINE)
    for block in blocks:
        m = re.match(r"Target (\d+): iqn\.[\d.\-]+\.storage:([0-9a-f]+)", block)
        if not m:
            continue
        tid, mac = m.group(1), m.group(2)
        # 截取 "I_T nexus information:" 到 "LUN information:" 之间的内容
        nexus_section = re.search(r"I_T nexus information:(.*?)LUN information:", block, re.DOTALL)
        nexus_text = nexus_section.group(1) if nexus_section else ""
        if "I_T nexus:" not in nexus_text:
            idle[tid] = mac
    return idle

def parse_active_nexus_tids():
    """返回当前**有** I_T nexus（有客户机连着）的 tid 集合（尽力而为）。

    与 parse_idle_tids 互补：本函数只看“还在连的”，用于判断某个 target 现在到底
    有没有人在用（回写占用是否该继续保留）。tgtadm 查询失败时返回空集合。
    """
    tids = set()
    try:
        out = subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return tids
    for block in re.split(r"(?=^Target \d+: )", out, flags=re.MULTILINE):
        m = re.match(r"Target (\d+): ", block)
        if not m:
            continue
        nexus_section = re.search(r"I_T nexus information:(.*?)LUN information:", block, re.DOTALL)
        if nexus_section and "I_T nexus:" in nexus_section.group(1):
            tids.add(m.group(1))
    return tids

def target_exists(tid):
    """tgtadm 里是否还存在该 tid 的 target。查询失败时返回 True（保守：不确定就别当它不存在）。"""
    try:
        out = subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True
    return re.search(r"^Target %d: " % int(tid), out, re.MULTILINE) is not None

def get_client_ips():
    """返回 {mac: ip}：tgt 当前有 I_T nexus 的客户机（尽力而为）。"""
    conn = {}
    try:
        out = subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "show"],
                             capture_output=True, text=True, timeout=5).stdout
        blocks = re.split(r"(?=^Target \d+: )", out, flags=re.MULTILINE)
        for block in blocks:
            m = re.match(r"Target \d+: iqn\.[\d.\-]+\.storage:([0-9a-f]+)", block)
            if not m:
                continue
            nexus = re.search(r"I_T nexus information:(.*?)LUN information:", block, re.DOTALL)
            if nexus and "I_T nexus:" in nexus.group(1):
                ipm = re.search(r"IP Address: ([\d\.]+)", nexus.group(1))
                conn[m.group(1)] = ipm.group(1) if ipm else "?"
    except Exception:
        pass
    return conn

def parse_3260_lastrcv():
    """解析 ss -tni 对 3260 端口所有连接的最后收包时间，返回 {client_ip: 距最后收包秒数}。
    lastrcv 是内核维护的"距该 socket 最后收到数据包的秒数"；活着的 iSCSI 客户端
    （NOP-Out 保活/ACK/IO）会持续刷新它，失联的客户端只会一直增长。"""
    res = {}
    try:
        out = subprocess.run(["ss", "-tni", "( sport = :3260 )"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return res
    cur_ip = None
    for line in out.splitlines():
        if line.startswith("ESTAB"):
            parts = line.split()
            if len(parts) >= 5:  # ESTAB Recv-Q Send-Q Local:Port Peer:Port
                cur_ip = parts[4].rsplit(":", 1)[0]
                continue
        m = re.search(r"lastrcv:(\d+)", line)
        if m and cur_ip:
            res[cur_ip] = int(m.group(1))
            cur_ip = None
    return res

def client_arp_alive(ip):
    """用 arping 探测客户机是否在线（L2 层邻居表应答，防火墙拦不住）。
    返回 True/False；工具缺失或出错返回 None（调用方按保守策略处理）。"""
    try:
        args = ["arping", "-c", str(IDLE_ARP_TRIES), "-w", str(IDLE_ARP_TIMEOUT)]
        # 多网卡环境下按路由选接口，避免 arping 从错误的网卡发探测
        try:
            rt = subprocess.run(["ip", "-o", "route", "get", ip],
                                capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"\bdev\s+(\S+)", rt)
            if m:
                args += ["-I", m.group(1)]
        except Exception:
            pass
        args.append(ip)
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return None

def force_cleanup_mac(mac, reason):
    """按 mac 强制回收全部资源（target/overlay/nbd/状态/写回占用）。
    用 --force 删除 target，即使 tgt 仍残留 nexus 也能删掉。持 mac 锁，与 /prov 串行。"""
    lock = get_mac_lock(mac)
    with lock:
        state = mac_state.get(mac)
        tid = stable_tid(mac)
        subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                        "--force", "--tid", str(tid)],
                       stderr=subprocess.DEVNULL, timeout=3)
        if state:
            if state.get("nbd_dev"):
                subprocess.run(["qemu-nbd", "-d", state["nbd_dev"]],
                               stderr=subprocess.DEVNULL, timeout=3)
                release_nbd(state["nbd_dev"])
            if state.get("overlay") and os.path.exists(state["overlay"]):
                try: os.remove(state["overlay"])
                except Exception: pass
            if state.get("writeback_img"):
                with global_lock:
                    writeback_active.pop(state["writeback_img"], None)
        mac_state.pop(mac, None)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [CLEANUP] {reason} for {mac} (tid={tid})")

def release_writeback_lock(img_name, reason):
    """只解除某母盘的“回写占用”登记（不动 target/叠加盘/nbd）。返回原占用者 mac 或 ""。

    给三类场景用：① 客户机已没有 I_T nexus（会话结束）宽限期到；② 后台手动解除；
    ③ 检测到登记与 tgtadm 实际状态不一致。
    """
    with global_lock:
        owner = writeback_active.pop(img_name, None)
    if owner:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [CLEANUP] write-back lock released for {img_name} (owner={owner}): {reason}")
    return owner or ""

def writeback_lock_stale(img_name, owner, no_nexus_since):
    """回写占用是否已过期可解除。返回 (可解除, 原因)。

    判据是“还有没有人在写这张母盘”，也就是 iSCSI 会话（I_T nexus）：
      - 没有 nexus 且持续超过 WRITEBACK_LOCK_GRACE -> 可解除；
      - 占用者不是当前任何在线 iSCSI 会话、tgtadm 里该 target 也不存在（比如服务重启后
        mac_state 清空、但登记是后来加的），且持续超过宽限期 -> 可解除（防止“幽灵占用”）；
      - 有 nexus 或还在宽限期内 -> 不能解除（临时掉线要留时间重连）。

    no_nexus_since：本 target 首次被观测到“没有 I_T nexus”的时间戳（由巡检维护）。
    """
    if not owner or not no_nexus_since:
        return False, ""
    age = time.time() - no_nexus_since
    if age < WRITEBACK_LOCK_GRACE:
        return False, ""
    if not parse_active_nexus_tids() and not target_exists(stable_tid(owner)):
        return True, f"占用者 {owner} 既无在线 iSCSI 会话、target 也不存在，持续 {int(age)}s"
    return True, f"{owner} 已无 I_T nexus 持续 {int(age)}s（会话已结束）"

def idle_cleanup_worker():
    idle_since = {}        # tid -> 首次被判定为 nexus 空闲的时间戳
    no_traffic_since = {}  # mac -> 首次被判定为"无流量可疑"的时间戳
    while True:
        time.sleep(IDLE_CHECK_INTERVAL)
        try:
            now_ts = time.time()

            # ---- 通道 0：回写占用只活在会话期间 ----
            # 母盘正被回写时，只要发起端断开（关机/重启/拔电/重连失败），就尽快放开占用，
            # 不能让一台已经关机的机器把母盘锁到 IDLE_TIMEOUT_SECONDS 甚至更久。
            active = parse_active_nexus_tids()
            with global_lock:
                wb_now = dict(writeback_active)
            for img_name, owner in wb_now.items():
                tid = str(stable_tid(owner))          # 与 parse_*_tids 的键类型一致（都是字符串）
                has_nexus = tid in active
                if has_nexus:
                    idle_since.pop(tid, None)
                    continue
                if tid not in idle_since:
                    idle_since[tid] = now_ts          # 与本通道原语义一致：target 无 nexus 的起点
                    continue
                ok, why = writeback_lock_stale(img_name, owner, idle_since.get(tid))
                if ok:
                    release_writeback_lock(img_name, why)

            # ---- 通道 1：正常 logout（nexus 消失）后的空闲清理 ----
            idle_now = parse_idle_tids()
            for tid in list(idle_since.keys()):
                if tid not in idle_now:
                    idle_since.pop(tid, None)
            for tid, mac in idle_now.items():
                if tid not in idle_since:
                    idle_since[tid] = now_ts
                    continue
                if now_ts - idle_since[tid] < IDLE_TIMEOUT_SECONDS:
                    continue
                force_cleanup_mac(mac, f"Idle target removed after {IDLE_TIMEOUT_SECONDS}s without I_T nexus")
                idle_since.pop(tid, None)
                no_traffic_since.pop(mac, None)

            # ---- 通道 2：无流量检测（客户机不主动断开时的关机/断电识别）----
            conn = get_client_ips()
            ages = parse_3260_lastrcv()   # {ip: 距最后收包秒数}
            for mac in list(mac_state.keys()):
                ip = conn.get(mac)
                if not ip or ip not in ages:
                    # 查不到该 mac 的连接信息（无 nexus 或 ss 无数据）：交给通道 1 / 保守跳过
                    no_traffic_since.pop(mac, None)
                    continue
                age = ages[ip]
                if age < IDLE_NO_TRAFFIC_SECONDS:
                    no_traffic_since.pop(mac, None)  # 一直在发包（保活/IO），活得好好的
                    continue
                if mac not in no_traffic_since:
                    no_traffic_since[mac] = now_ts
                    continue
                if now_ts - no_traffic_since[mac] < IDLE_NO_TRAFFIC_CONFIRM * IDLE_CHECK_INTERVAL:
                    continue  # 连续多次巡检确认，防抖动
                alive = client_arp_alive(ip)
                if alive is True:
                    no_traffic_since.pop(mac, None)  # 活着，只是没发包（如 Windows 完全空闲），不删
                    continue
                if alive is False:
                    force_cleanup_mac(mac, f"No traffic from {ip} for {age}s and ARP dead")
                    no_traffic_since.pop(mac, None)
                    continue
                # arping 不可用：退回硬阈值保守兜底
                if age > IDLE_NO_TRAFFIC_HARD:
                    force_cleanup_mac(mac, f"No traffic from {ip} for {age}s (hard threshold, no arping)")
                    no_traffic_since.pop(mac, None)
        except Exception as e:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] [ERROR] idle_cleanup_worker: {e}")

def netctrl_reconcile_worker():
    """联网控制巡检：周期对齐转发规则（新开机/离线客户机自动增删规则）。"""
    while True:
        time.sleep(NETCTRL_RECONCILE_INTERVAL)
        try:
            netctrl.reconcile_once(list(get_client_ips()))
        except Exception as e:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] [ERROR] netctrl_reconcile_worker: {e}")

# =================== Web 管理后台：会话与页面辅助 ===================
SESSION_TTL = 12 * 3600       # 后台登录会话有效期（秒）

web_sessions = {}   # token -> {"exp","csrf","user","role"} 后台会话（内存态，重启即失效；role: admin/user）
web_lock = threading.Lock()

# ---------- 管理员模式：一次性令牌（避免密码明文反复出现在 URL/日志里） ----------
ADMIN_TOKEN_TTL = 300        # 令牌有效期（秒）
ADMIN_ATTEMPT_MAX = 10       # 每 IP 在窗口期内最多密码尝试次数
ADMIN_ATTEMPT_WINDOW = 300   # 限速窗口（秒）

admin_tokens = {}    # mac -> {"tok":..., "exp":...}
admin_attempts = {}  # ip -> [尝试时间戳]
admin_lock = threading.Lock()

def admin_token_issue(mac):
    tok = secrets.token_urlsafe(12)
    with admin_lock:
        admin_tokens[mac] = {"tok": tok, "exp": time.time() + ADMIN_TOKEN_TTL}
    return tok

def admin_token_ok(mac, tok):
    if not mac or not tok:
        return False
    with admin_lock:
        rec = admin_tokens.get(mac)
        if not rec:
            return False
        if time.time() > rec["exp"]:
            admin_tokens.pop(mac, None)
            return False
        return secrets.compare_digest(rec["tok"], tok)

def admin_ratelimited(ip):
    now = time.time()
    with admin_lock:
        ts = [t for t in admin_attempts.get(ip, []) if now - t <= ADMIN_ATTEMPT_WINDOW]
        if len(ts) >= ADMIN_ATTEMPT_MAX:
            admin_attempts[ip] = ts
            return True
        ts.append(now)
        admin_attempts[ip] = ts
        return False

# ---------- 网盘登录限速（客户机托盘登录用，只统计失败次数） ----------
DAV_LOGIN_ATTEMPT_MAX = 10        # 每 IP 在窗口期内最多失败次数
DAV_LOGIN_ATTEMPT_WINDOW = 300    # 窗口（秒）
dav_login_fails = {}              # ip -> [失败时间戳]
dav_login_lock = threading.Lock()

def dav_login_ratelimited(ip):
    """该来源 IP 是否因为失败次数过多被限速。"""
    now = time.time()
    with dav_login_lock:
        ts = [t for t in dav_login_fails.get(ip, []) if now - t <= DAV_LOGIN_ATTEMPT_WINDOW]
        dav_login_fails[ip] = ts
        return len(ts) >= DAV_LOGIN_ATTEMPT_MAX

def dav_login_failed(ip):
    with dav_login_lock:
        dav_login_fails.setdefault(ip, []).append(time.time())

def dav_login_reset(ip):
    with dav_login_lock:
        dav_login_fails.pop(ip, None)

PAGE_CSS = """body{font-family:"Microsoft YaHei",Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px;color:#222}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:20px;margin-top:0}
h2{font-size:16px;margin:0 0 10px}
.card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #e0e0e0;padding:6px 10px;text-align:left}
th{background:#f0f4f8}
input[type=text],input[type=password]{padding:6px 8px;width:260px;border:1px solid #bbb;border-radius:4px;margin:4px 0}
input[type=submit]{padding:7px 18px;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer}
a{color:#2563eb;text-decoration:none;margin-right:14px}
.err{color:#c00}.ok{color:#0a0}
.small{color:#777;font-size:12px}
.inline-form{display:inline;margin:0}
.inline-form input[type=submit]{padding:2px 8px;font-size:12px}"""

NAV_ADMIN = ('<p><a href="/">客户机名单</a><a href="/web/clients">客户机控制</a>'
             '<a href="/web/create">创建空白盘</a>'
             '<a href="/web/export">iSCSI 挂载</a><a href="/web/password">修改密码</a>'
             '<a href="/web/users">用户与配额</a>'
             '<a href="/web/common">通用文件</a><a href="/web/settings">默认配额</a>'
             '<a href="/web/netctrl">联网控制</a><a href="/web/logout">退出登录</a></p>')
NAV_USER = ('<p><a href="/web/drive">我的网盘</a><a href="/web/logout">退出登录</a></p>')

def _page_html(title, body, nav=None):
    """整页 HTML；nav 为导航 HTML 字符串（None = 无导航）。含 IE11 兼容 meta。"""
    return ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
            '<meta http-equiv="X-UA-Compatible" content="IE=edge"><title>'
            + html.escape(title) + '</title><style>' + PAGE_CSS + '</style></head><body><div class="wrap"><h1>'
            + html.escape(title) + '</h1>' + (nav if nav else '') + body + '</div></body></html>')

def _human_size(n):
    """字节数 -> 人类可读字符串（如 1.2 GB）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n < 0:
        n = 0
    if n < 1024:
        return "%d B" % n
    val = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        val /= 1024.0
        if val < 1024 or unit == "PB":
            return "%.1f %s" % (val, unit)
    return "%d B" % n


def _breadcrumb_html(rel):
    """网盘面包屑（根目录/…/当前），末尾附“上一级”链接；rel 为相对路径。"""
    parts = [p for p in (rel or "").split("/") if p]
    items = ['<a href="/web/drive">根目录</a>']
    acc = ""
    for i, p in enumerate(parts):
        acc = p if not acc else acc + "/" + p
        if i == len(parts) - 1:
            items.append(html.escape(p))
        else:
            items.append('<a href="/web/drive?p=' + urllib.parse.quote(acc, safe="")
                         + '">' + html.escape(p) + '</a>')
    up = ""
    if parts:
        up = ('　<a href="/web/drive?p=' + urllib.parse.quote("/".join(parts[:-1]), safe="")
              + '">上一级</a>')
    return " / ".join(items) + up


# ---------- 管理员密码存储：加盐 SHA256 哈希（admin.conf 不再存明文） ----------
# 存储格式：单行  sha256$<salt_hex>$<digest_hex>
ADMIN_PW_STORED = None   # 当前生效的存储行（sha256$salt$digest）

def hash_password(pwd, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"

def verify_password(pwd, stored):
    try:
        algo, salt, digest = stored.strip().split("$", 2)
        if algo != "sha256":
            return False
        expect = hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
        return secrets.compare_digest(expect, digest)
    except Exception:
        return False

def _write_admin_password_file(stored):
    with open(ADMIN_PASS_FILE, "w", encoding="utf-8") as f:
        f.write(stored + "\n")

def _chmod_admin_file():
    try:
        os.chmod(ADMIN_PASS_FILE, 0o600)
    except Exception:
        pass

def load_admin_password():
    """启动时从 admin.conf 读取管理员密码哈希；文件不存在或为空则写入默认密码的哈希。
    旧版明文文件自动迁移为加盐 SHA256 哈希。"""
    global ADMIN_PASSWORD, ADMIN_PW_STORED
    if os.path.exists(ADMIN_PASS_FILE):
        try:
            with open(ADMIN_PASS_FILE, "r", encoding="utf-8") as f:
                line = f.read().strip()
        except Exception:
            line = ""
        if line:
            if line.startswith("sha256$"):
                ADMIN_PW_STORED = line
                print(f"[{datetime.datetime.now()}] [INFO] Admin password (hashed) loaded from {ADMIN_PASS_FILE}")
            else:
                # 旧版明文文件：自动迁移为哈希后重写
                ADMIN_PW_STORED = hash_password(line)
                _write_admin_password_file(ADMIN_PW_STORED)
                print(f"[{datetime.datetime.now()}] [INFO] Migrated plaintext admin password to SHA256 hash in {ADMIN_PASS_FILE}")
            _chmod_admin_file()
            return
    # 文件不存在或为空：写入默认密码的哈希
    ADMIN_PW_STORED = hash_password(ADMIN_PASSWORD)
    _write_admin_password_file(ADMIN_PW_STORED)
    _chmod_admin_file()
    print(f"[{datetime.datetime.now()}] [INFO] Admin password file created (hashed): {ADMIN_PASS_FILE}")

def set_admin_password(new_pwd):
    """修改管理员密码并持久化（加盐 SHA256 哈希）到 admin.conf（iPXE 管理员模式与 Web 后台共用）。"""
    global ADMIN_PASSWORD, ADMIN_PW_STORED
    ADMIN_PW_STORED = hash_password(new_pwd)
    _write_admin_password_file(ADMIN_PW_STORED)
    _chmod_admin_file()
    ADMIN_PASSWORD = new_pwd

def _overlay_img(mac, overlay):
    """从叠加盘文件名反推镜像名：overlay_<mac>_<img>.raw/.qcow2 -> <img>"""
    base = os.path.basename(overlay)
    prefix = f"overlay_{mac}_"
    if base.startswith(prefix):
        base = base[len(prefix):]
    for suf in (".raw", ".qcow2"):
        if base.endswith(suf):
            base = base[:-len(suf)]
    return base

def collect_client_info():
    """汇总客户机信息供 Web 后台展示：mac_state 内存记录 + tgtadm 在线连接（尽力而为）。"""
    conn = get_client_ips()  # mac -> ip（tgtadm 当前有 I_T nexus 的在线客户机）
    rows = []
    for mac, st in sorted(mac_state.items()):
        img, mode = "", ""
        if st.get("writeback_img"):
            img, mode = st["writeback_img"], "回写"
        elif st.get("overlay"):
            img, mode = _overlay_img(mac, st["overlay"]), "叠加"
        detail = " ".join(p for p in (st.get("nbd_dev"), st.get("overlay")) if p)
        rows.append({"mac": mac, "img": img, "mode": mode,
                     "online": mac in conn, "ip": conn.get(mac, ""),
                     "detail": detail})
    return rows

def _tgt_force_delete(tid):
    """尽力删除某个 target（不抛异常；带 --force，即使仍有连接也能删）。"""
    subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                    "--force", "--tid", str(tid)], stderr=subprocess.DEVNULL, timeout=3)

def _file_size_or_none(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None

def _image_busy_reason_unlocked(img_name):
    """返回母盘镜像当前被占用的原因；空闲返回 None。调用方必须已持有 global_lock。"""
    wb = writeback_active.get(img_name)
    if wb:
        return f"镜像正被客户机 {wb} 以回写模式使用"
    overlay_users = [m for m, s in mac_state.items()
                     if s.get("overlay") and _overlay_img(m, s["overlay"]) == img_name]
    if overlay_users:
        return f"镜像正被客户机 {overlay_users[0]} 以叠加模式使用" + (
            f"（共 {len(overlay_users)} 台）" if len(overlay_users) > 1 else "")
    if img_name in web_exported:
        return "镜像已被后台手动挂载（见下方列表）"
    return None

def image_busy_reason(img_name):
    """供页面展示用的带锁版本：返回占用原因或 None。"""
    with global_lock:
        return _image_busy_reason_unlocked(img_name)

def _alloc_web_export_tid():
    with web_export_lock:
        used = {rec["tid"] for rec in web_exported.values()}
        for i in range(WEB_EXPORT_TID_SLOTS):
            tid = WEB_EXPORT_TID_BASE + i
            if tid not in used:
                return tid
    raise RuntimeError("manual iSCSI export slots exhausted")

def export_image_mount(img_name):
    """后台手动挂载：把一张空闲母盘 .raw 直接导出为 iSCSI target（写直达母盘）。
    返回 (True, iqn) 或 (False, 原因)。挂载成功前先登记占用，PXE/回写流程
    并发请求同一镜像会被拒绝；tgt 建失败则回滚登记并清理。"""
    base_img = os.path.join(IMAGES_DIR, f"{img_name}.raw")
    if not img_name or os.path.basename(img_name) != img_name or not os.path.isfile(base_img):
        return False, f"镜像不存在：{img_name or '（空）'}"
    iqn = f"iqn.2026-07.storage:web-{img_name}"
    try:
        tid = _alloc_web_export_tid()
    except RuntimeError as e:
        return False, str(e)
    with global_lock:
        reason = _image_busy_reason_unlocked(img_name)
        if reason:
            return False, reason
        web_exported[img_name] = {"tid": tid, "iqn": iqn, "path": base_img,
                                  "size": _file_size_or_none(base_img),
                                  "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        tgt_new_target(tid, iqn)
        tgt_new_lun(tid, base_img, None)   # 文件后端默认缓冲 I/O，直接读写母盘文件
        tgt_bind_target(tid)
    except Exception as e:
        with global_lock:
            web_exported.pop(img_name, None)
        _tgt_force_delete(tid)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [ERROR] Manual iSCSI export failed for {img_name}: {e}")
        return False, f"挂载失败：{e}"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [SUCCESS] Manual iSCSI export: {iqn} -> {base_img} (tid={tid})")
    return True, iqn

def export_image_unmount(img_name):
    """卸载后台手动挂载的盘：删除 target 并释放该镜像占用。返回 (True, 消息)/(False, 原因)。"""
    with global_lock:
        rec = web_exported.pop(img_name, None)
    if not rec:
        return False, f"镜像未处于后台挂载状态：{img_name or '（空）'}"
    _tgt_force_delete(rec["tid"])
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [SUCCESS] Manual iSCSI export removed: {rec['iqn']} (tid={rec['tid']})")
    return True, f"已卸载：{rec['iqn']}"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): return

    def _scheme(self):
        return "https" if HTTPS_ENABLED else "http"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/boot.ipxe":
            host = self.headers['Host']
            images = list_images()
            # 出错后回到菜单时不带 --timeout：带超时的话默认镜像每 15 秒会被自动重试一次，
            # 一直卡在“报错 → 回菜单 → 超时又报错”的循环里（不带超时则等用户按键）。
            no_timeout = params.get("notimeout", [""])[0] == "1"

            if not images:
                # 没有任何母盘时，给出明确提示而不是生成空菜单
                body = (
                    "#!ipxe\n"
                    "echo No images found in IMAGES_DIR on server.\n"
                    "shell\n"
                )
            else:
                default_img = DEFAULT_IMAGE if DEFAULT_IMAGE in images else images[0]
                lines = ["#!ipxe", "menu Please select an OS image to boot"]
                for img in images:
                    lines.append(f"item {img} {img}")
                lines.append("item admin_mode Admin Mode (password required)")
                choose_opts = f"--default {default_img} "
                if not no_timeout:
                    choose_opts += "--timeout 15000 "
                lines.append(f"choose {choose_opts}target || goto cancel")
                lines.append("goto ${target}")
                for img in images:
                    lines.append(f":{img}")
                    lines.append(f"chain {self._scheme()}://{host}/prov?mac=${{mac}}&img={img} || goto cancel")
                lines.append(":admin_mode")
                lines.append(f"chain {self._scheme()}://{host}/admin?mac=${{mac}} || goto cancel")
                lines.append(":cancel")
                lines.append("echo Boot cancelled or failed.")
                lines.append("shell")
                body = "\n".join(lines) + "\n"

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if path == "/prov":
            host = self.headers['Host']
            mac = params.get("mac", ["unknown"])[0].replace(":", "").strip().lower()
            if mac == "unknown":
                self._fail_to_menu(host, "Client MAC missing in request.")
                return

            # 联网控制：客户机开机（PXE 请求供给）即确保/覆写其转发规则
            try:
                netctrl.on_client_boot(mac)
            except Exception:
                pass

            # 镜像名：来自 URL 参数 ?img=xxx，仅取文件名部分防止路径穿越（如 ../../etc）
            img_name = os.path.basename(params.get("img", [DEFAULT_IMAGE])[0])
            base_img = os.path.join(IMAGES_DIR, f"{img_name}.raw")

            if not os.path.isfile(base_img):
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [ERROR] Image not found for {mac}: {base_img}")
                self._fail_to_menu(host, f"Image '{img_name}' not found on server.")
                return

            server_ip = self.headers.get('Host', '').split(':')[0]
            lock = get_mac_lock(mac)
            with lock:
                tid = stable_tid(mac)
                iqn = f"iqn.2026-07.storage:{mac}"
                overlay = os.path.join(OVERLAY_DIR,
                                       f"overlay_{mac}_{img_name}.raw" if USE_REFLINK
                                       else f"overlay_{mac}_{img_name}.qcow2")
                nbd_dev = None
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [INFO] Client {mac} requesting target (image={img_name}, mode={'reflink' if USE_REFLINK else 'qcow2'})...")

                # 回写占用检查：母盘正被管理员回写时禁止新建叠加盘（cp --reflink 非原子，
                # 并发写入会产生新旧混杂的快照）
                with global_lock:
                    wb_owner = writeback_active.get(img_name)
                if wb_owner:
                    print(f"[{now}] [ERROR] {mac}: image {img_name} is in write-back use by {wb_owner}, refuse")
                    self._fail_to_menu(host, f"Image {img_name} is in write-back use by {wb_owner}. Please try again later.")
                    return

                # 后台手动挂载互斥：母盘正被后台导出（外部 iSCSI 发起端在写盘）时，
                # 禁止再对它建叠加盘，避免快照与并发写入混杂出脏数据
                with global_lock:
                    exported_iqn = web_exported.get(img_name, {}).get("iqn")
                if exported_iqn:
                    print(f"[{now}] [ERROR] {mac}: image {img_name} is manually exported ({exported_iqn}), refuse")
                    self._fail_to_menu(host, f"Image {img_name} is manually exported via web ({exported_iqn}). Unmount it in web admin first.")
                    return

                try:
                    ensure_overlay_space()
                except Exception as e:
                    print(f"[{now}] [ERROR] {mac}: {e}")
                    self._fail_to_menu(host, str(e))
                    return

                try:
                    subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                    "--force", "--tid", str(tid)],
                                   stderr=subprocess.DEVNULL, timeout=3)
                    # 只断开这个 mac 自己上一次用过的 nbd 设备，绝不动其他在线客户机正在用的设备
                    prev = mac_state.get(mac)
                    if prev:
                        if prev.get("nbd_dev"):
                            subprocess.run(["qemu-nbd", "-d", prev["nbd_dev"]],
                                           stderr=subprocess.DEVNULL, timeout=2)
                            release_nbd(prev["nbd_dev"])
                        if prev.get("writeback_img"):
                            with global_lock:
                                writeback_active.pop(prev["writeback_img"], None)
                    if os.path.exists(overlay):
                        os.remove(overlay)

                    if USE_REFLINK:
                        # ============ 路线 A：reflink 直出 raw，无 qemu ============
                        try:
                            run_cmd(["cp", "--reflink=always", base_img, overlay], timeout=60)
                        except Exception:
                            # 万一 reflink 失败（跨文件系统等），就地退回路线 B
                            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            print(f"[{now}] [WARN] reflink failed for {mac}, falling back to qcow2")
                            overlay = os.path.join(OVERLAY_DIR, f"overlay_{mac}_{img_name}.qcow2")
                            if os.path.exists(overlay):
                                os.remove(overlay)
                            run_cmd(build_qemu_img_create_cmd(base_img, overlay), timeout=30)
                            nbd_dev = find_free_nbd()
                            connect_nbd(nbd_dev, overlay)
                            if not wait_nbd_ready(nbd_dev):
                                raise RuntimeError(f"{nbd_dev} not ready (size still 0) after waiting")
                            tune_nbd_dev(nbd_dev)
                            mac_state[mac] = {"nbd_dev": nbd_dev, "overlay": overlay}
                            tgt_new_target(tid, iqn)
                            try:
                                tgt_new_lun(tid, nbd_dev, TGT_BSOFLAGS)
                            except Exception:
                                tgt_new_lun(tid, nbd_dev, None)
                            tgt_bind_target(tid)
                            self._send_sanboot(server_ip, iqn)
                            print(f"[{now}] [SUCCESS] Target {iqn} ready on {nbd_dev} (qcow2 fallback)")
                            return

                        mac_state[mac] = {"nbd_dev": None, "overlay": overlay}
                        tgt_new_target(tid, iqn)
                        # 文件后端用缓冲 I/O（不传 --bsoflags），让页缓存命中共享母盘块
                        tgt_new_lun(tid, overlay, None)
                        if THIN_ON_REFLINK:
                            tgt_enable_thin(tid)
                        tgt_bind_target(tid)
                        self._send_sanboot(server_ip, iqn)
                        print(f"[{now}] [SUCCESS] Target {iqn} ready via reflink overlay {overlay}")
                    else:
                        # ============ 路线 B：qcow2 叠加盘 + qemu-nbd ============
                        run_cmd(build_qemu_img_create_cmd(base_img, overlay), timeout=30)
                        nbd_dev = find_free_nbd()
                        connect_nbd(nbd_dev, overlay)
                        if not wait_nbd_ready(nbd_dev):
                            raise RuntimeError(f"{nbd_dev} not ready (size still 0) after waiting")
                        tune_nbd_dev(nbd_dev)
                        mac_state[mac] = {"nbd_dev": nbd_dev, "overlay": overlay}
                        tgt_new_target(tid, iqn)
                        try:
                            tgt_new_lun(tid, nbd_dev, TGT_BSOFLAGS)
                        except Exception:
                            tgt_new_lun(tid, nbd_dev, None)
                        tgt_bind_target(tid)
                        self._send_sanboot(server_ip, iqn)
                        print(f"[{now}] [SUCCESS] Target {iqn} ready on {nbd_dev}")

                except Exception as e:
                    print(f"[{now}] [ERROR] Failed for {mac}: {str(e)}")
                    if nbd_dev:
                        subprocess.run(["qemu-nbd", "-d", nbd_dev],
                                       stderr=subprocess.DEVNULL, timeout=3)
                        release_nbd(nbd_dev)
                    mac_state.pop(mac, None)
                    if os.path.exists(overlay):
                        try: os.remove(overlay)
                        except: pass
                    subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                    "--force", "--tid", str(tid)],
                                   stderr=subprocess.DEVNULL, timeout=3)
                    self._fail_to_menu(host, f"Boot failed: {e}")
            return

        # ================== 管理员模式 ==================
        if path == "/admin":
            # 登录页：iPXE 的 login 命令会交互式提示用户名/密码（密码不回显），
            # 仅允许账号 admin 登录，用户名与密码一起提交给服务端校验
            host = self.headers['Host']
            self._serve_script(
                "#!ipxe\n"
                "echo === Admin Mode ===\n"
                "echo Please login with username: admin\n"
                "login\n"
                f"chain {self._scheme()}://{host}/admin/verify?mac=${{mac}}&user=${{username}}&pwd=${{password}} || goto cancel\n"
                ":cancel\n"
                "echo Login cancelled.\n"
                "shell\n"
            )
            return

        if path == "/admin/verify":
            # 密码验证通过后签发一次性短时令牌（绑定 MAC），后续 wb/wb_prov 全部用令牌，
            # 密码不再进入后续 URL/日志。iPXE 登录仅允许账号 admin。
            host = self.headers['Host']
            mac = params.get("mac", [""])[0].replace(":", "").strip().lower()
            user = params.get("user", [""])[0]
            pwd = params.get("pwd", [""])[0]
            if user != "admin":
                self._fail_to_menu(host, "Admin account required!")
                return
            if admin_ratelimited(self.client_address[0]):
                time.sleep(1)
                self._fail_to_menu(host, "Too many attempts, try again later!")
                return
            if users_auth.check_login(user, pwd) != "admin":
                time.sleep(1)
                self._fail_to_menu(host, "Wrong password!")
                return
            tok = admin_token_issue(mac)
            self._admin_wb_menu(host, mac, tok)
            return

        if path == "/admin/wb":
            # 兼容入口：用一次性令牌进入选盘菜单
            host = self.headers['Host']
            mac = params.get("mac", [""])[0].replace(":", "").strip().lower()
            tok = params.get("tok", [""])[0]
            if not admin_token_ok(mac, tok):
                self._fail_to_menu(host, "Invalid or expired admin token!")
                return
            self._admin_wb_menu(host, mac, tok)
            return

        if path == "/admin/wb_prov":
            # 回写模式：直接把母盘 .raw 文件导出成 iSCSI LUN（不建叠加盘），
            # 客户机的一切写入都会直接落到母盘上
            mac = params.get("mac", [""])[0].replace(":", "").strip().lower()
            tok = params.get("tok", [""])[0]
            img_name = os.path.basename(params.get("img", [""])[0])
            # 联网控制：回写模式开机同样触发规则对齐
            try:
                netctrl.on_client_boot(mac)
            except Exception:
                pass
            host = self.headers['Host']
            if not mac or not admin_token_ok(mac, tok):
                now_dbg = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now_dbg}] [ERROR] wb_prov denied: mac={mac!r} tok_len={len(tok)} img={img_name!r}")
                self._fail_to_menu(host, "Invalid or expired admin token!")
                return
            base_img = os.path.join(IMAGES_DIR, f"{img_name}.raw")
            if not img_name or not os.path.isfile(base_img):
                self._fail_to_menu(host, f"Image not found: {img_name}")
                return
            lock = get_mac_lock(mac)
            with lock:
                with global_lock:
                    owner = writeback_active.get(img_name)
                if owner not in (None, mac):
                    # 占用者若已经没有 iSCSI 会话（关机/拔电/掉线没重连），把它当过期占用夺过来，
                    # 否则一台关机的机器能把这个母盘锁很久。判据（都必须成立）：
                    #   ① 该 target 当前没有 I_T nexus；
                    #   ② 占用者不是“刚还在心跳”的机器（回写机开机时本来就没心跳，不能用“有心跳记录”当判据）；
                    #   ③ L2 上 ARP 也不应答（arping 不可用时这项跳过，交给巡检的宽限期兜底）。
                    stale = False
                    try:
                        owner_ip = agent_hub.ip_of(owner) or ""
                        rec_owner = agent_hub.record_of(owner)
                        hb_old = (not rec_owner) or \
                            (time.time() - (rec_owner.get("last_seen") or 0)) > agent_hub.ONLINE_WINDOW
                        arp_dead = bool(owner_ip) and client_arp_alive(owner_ip) is False
                        if owner not in get_client_ips() and hb_old and arp_dead:
                            stale = True
                    except Exception:
                        stale = False
                    if stale:
                        print(f"[{datetime.datetime.now()}] [CLEANUP] write-back lock taken over from "
                              f"{owner} (no active iSCSI session) by {mac} for {img_name}")
                        subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                        "--force", "--tid", str(stable_tid(owner))],
                                       stderr=subprocess.DEVNULL, timeout=3)
                        with global_lock:
                            writeback_active.pop(img_name, None)
                            writeback_active[img_name] = mac
                        mac_state.pop(owner, None)
                    else:
                        self._fail_to_menu(host, f"Image {img_name} is in write-back use by {owner}"
                                                 ". Please try again later.")
                        return
                # 后台手动挂载互斥：该母盘正被后台导出（外部发起端写盘）时禁止回写
                with global_lock:
                    exported_iqn = web_exported.get(img_name, {}).get("iqn")
                if exported_iqn:
                    self._fail_to_menu(host, f"Image {img_name} is manually exported via web ({exported_iqn}). Unmount it in web admin first.")
                    return
                # 提示性告警：其他客户机仍持有该母盘的叠加盘时回写，旧叠加盘将保持快照状态
                with global_lock:
                    overlay_users = [m for m, s in mac_state.items()
                                     if m != mac and s.get("overlay")
                                     and _overlay_img(m, s["overlay"]) == img_name]
                if overlay_users:
                    print(f"[{datetime.datetime.now()}] [WARN] {mac} write-back of {img_name} while overlays active: {overlay_users}")
                tid = stable_tid(mac)
                iqn = f"iqn.2026-07.storage:{mac}"
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [INFO] Client {mac} requesting WRITE-BACK boot (image={img_name})...")
                try:
                    ensure_overlay_space()
                except Exception as e:
                    print(f"[{now}] [ERROR] {mac}: {e}")
                    self._fail_to_menu(host, f"Server disk space low: {e}")
                    return
                try:
                    # 清掉该 mac 上一次占用的所有资源
                    subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                    "--force", "--tid", str(tid)],
                                   stderr=subprocess.DEVNULL, timeout=3)
                    prev = mac_state.get(mac)
                    if prev:
                        if prev.get("nbd_dev"):
                            subprocess.run(["qemu-nbd", "-d", prev["nbd_dev"]],
                                           stderr=subprocess.DEVNULL, timeout=2)
                            release_nbd(prev["nbd_dev"])
                        if prev.get("overlay") and os.path.exists(prev["overlay"]):
                            try: os.remove(prev["overlay"])
                            except Exception: pass
                        if prev.get("writeback_img"):
                            with global_lock:
                                writeback_active.pop(prev["writeback_img"], None)
                    tgt_new_target(tid, iqn)
                    tgt_new_lun(tid, base_img, None)  # 文件后端默认缓冲 I/O，直接读写母盘文件
                    tgt_bind_target(tid)
                    mac_state[mac] = {"nbd_dev": None, "overlay": None, "writeback_img": img_name}
                    with global_lock:
                        writeback_active[img_name] = mac
                    server_ip = self.headers.get('Host', '').split(':')[0]
                    self._send_sanboot(server_ip, iqn)
                    print(f"[{now}] [SUCCESS] WRITE-BACK target {iqn} -> {base_img}")
                except Exception as e:
                    print(f"[{now}] [ERROR] WRITE-BACK failed for {mac}: {str(e)}")
                    subprocess.run(["tgtadm", "--lld", "iscsi", "--mode", "target", "--op", "delete",
                                    "--force", "--tid", str(tid)],
                                   stderr=subprocess.DEVNULL, timeout=3)
                    mac_state.pop(mac, None)
                    with global_lock:
                        writeback_active.pop(img_name, None)
                    self._fail_to_menu(host, f"Write-back boot failed: {e}")
            return

        self.send_error(404)

    def _admin_wb_menu(self, host, mac, tok):
        """管理员模式唯一功能：回写启动母盘的选盘菜单（修改直接写入母盘）。"""
        images = list_images()
        if not images:
            self._serve_script("#!ipxe\necho No images available.\nshell\n")
            return
        lines = ["#!ipxe",
                 "menu Boot base image in WRITE-BACK mode (modifies the master image)",
                 "item back Back to boot menu"]
        for img in images:
            lines.append(f"item {img} {img}")
        lines.append("choose target || goto cancel")
        lines.append("goto ${target}")
        lines.append(":back")
        lines.append(f"chain {self._scheme()}://{host}/boot.ipxe || goto cancel")
        # 注意：tok 由服务端在 /admin/verify 签发后直接写进 URL，不能依赖 iPXE 变量
        # ${tok} 在跨脚本 chain 后仍然存在（实测部分 iPXE 版本会丢失，导致被拒）。
        for img in images:
            lines.append(f":{img}")
            lines.append(f"chain {self._scheme()}://{host}/admin/wb_prov?mac=${{mac}}&tok={tok}&img={img} || goto cancel")
        lines.append(":cancel")
        lines.append("shell")
        self._serve_script("\n".join(lines) + "\n")

    def _send_sanboot(self, server_ip, iqn):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"#!ipxe\nsanboot iscsi:{server_ip}:::1:{iqn}\n".encode())

    def _serve_script(self, body):
        """返回一段 iPXE 脚本（text/plain）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def _admin_ok(self, pwd):
        """常量时间比较管理员密码（对存储的加盐 SHA256 哈希），防时序侧信道。"""
        stored = ADMIN_PW_STORED
        if not stored:
            return False
        return verify_password(pwd, stored)

    def _fail_to_menu(self, host, msg):
        """供给失败（密码错误 / 镜像被占用 / 空间不足等）时返回提示脚本，稍候自动回到启动菜单。

        必须用 HTTP 200 + iPXE 脚本，不能用 send_error()：非 2xx 会让 iPXE 的 `chain`
        直接失败，触发引导脚本里的 `|| goto cancel` 掉进 iPXE shell——客户机只看到一句
        报错，既看不到原因也回不到菜单（管理员回写模式与普通叠加模式必须表现一致）。

        回菜单时带 notimeout=1：否则默认镜像会在菜单超时后被自动重试，一直循环报错。
        不用 prompt --key：新版 iPXE 要求 --key 必须带参数（等特定键码），
        不带直接报 "Option 'key' requires an argument" 并中断脚本。"""
        safe = "".join(c for c in msg if c not in "\r\n")[:200]   # 防消息里的换行拆出额外 iPXE 命令
        self._serve_script(
            "#!ipxe\n"
            f"echo {safe}\n"
            "sleep 2\n"
            f"chain {self._scheme()}://{host}/boot.ipxe?notimeout=1 || goto cancel\n"
            ":cancel\n"
            "shell\n"
        )

def _form_boundary(content_type):
    """从 multipart/form-data 的 Content-Type 里取 boundary（无引号）；没有返回 None。"""
    for part in (content_type or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k.lower() == "boundary" and v:
            return v.strip().strip('"').encode("utf-8", "replace")
    return None


def _parse_multipart(body, boundary):
    """把 multipart/form-data 的完整请求体解析成 {字段名: [str 或 bytes]}。

    只用于后台上传小的客户端 exe（上限见 client_release.MAX_UPLOAD），所以整包读进内存，
    不引入 cgi 模块（3.13 已移除）。文件字段的值是 bytes，普通字段是 str。
    """
    delim = b"--" + boundary
    fields = {}
    if not body.startswith(delim):
        return fields
    for part in body[len(delim):].split(delim):
        if part[:2] == b"--":                     # 结束标记
            break
        part = part[2:] if part[:2] == b"\r\n" else part.lstrip(b"\r\n")
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, sep, val = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        name, filename = None, None
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for item in line.split(";"):
                    k, _, v = item.strip().partition("=")
                    if k.lower() == "name":
                        name = v.strip().strip('"')
                    elif k.lower() == "filename":
                        filename = v.strip().strip('"')
        if not name:
            continue
        fields.setdefault(name, []).append(val if filename is not None else
                                            val.decode("utf-8", "replace"))
    return fields


class WebAdminHandler(BaseHTTPRequestHandler):
    """简易 Web 管理后台：修改密码 / 创建空白盘 / 查看客户机名单 / 多账号与个人网盘。"""
    def log_message(self, fmt, *args):
        return  # 静默访问日志

    # ---------- 基础 ----------
    def _send_html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "admin_session":
                return v
        return None

    def _session(self):
        tok = self._cookie()
        if not tok:
            return None
        with web_lock:
            s = web_sessions.get(tok)
            if not s:
                return None
            if time.time() > s["exp"]:
                web_sessions.pop(tok, None)
                return None
            return s

    def _require_session(self):
        s = self._session()
        if not s:
            self._redirect("/")
        return s

    def _csrf_hidden(self):
        s = self._session()
        return f'<input type="hidden" name="csrf" value="{s["csrf"]}">' if s else ""

    # ---------- 页面 ----------
    def _login_page(self, err=None):
        cls = 'ok' if (err or "").startswith("注册成功") else 'err'
        body = ('<div class="card"><form method="post" action="/web/login">'
                '<p>用户名：<br><input type="text" name="user" value="admin" maxlength="32"></p>'
                '<p>密码：<br><input type="password" name="pwd" maxlength="32"></p>')
        if err:
            body += '<p class="' + cls + '">' + html.escape(err) + '</p>'
        body += ('<p><input type="submit" value="登录"></p></form>'
                 '<p class="small"><a href="/web/register">注册账号</a></p></div>')
        self._send_html(_page_html("登录 - " + WEB_TITLE, body, None))

    def _dashboard(self, msg=None):
        rows = collect_client_info()
        trs = "".join(
            '<tr><td>{mac}</td><td>{img}</td><td>{mode}</td><td>{st}</td><td>{ip}</td>'
            '<td class="small">{detail}</td></tr>'.format(
                mac=html.escape(r["mac"]), img=html.escape(r["img"]), mode=html.escape(r["mode"]),
                st='<span class="ok">在线</span>' if r["online"] else '空闲',
                ip=html.escape(r["ip"]), detail=html.escape(r["detail"]))
            for r in rows) or '<tr><td colspan="6">暂无客户机记录</td></tr>'
        # 回写占用：标明占用者现在还有没有 iSCSI 会话（没有=占用已过期，巡检会自动解除），
        # 并给管理员一个手动解除的兜底按钮（客户机拔电/状态卡住时不用等巡检）。
        conn = get_client_ips()
        wb_lines = []
        for img_name, owner in sorted(writeback_active.items()):
            live = owner in conn
            state = ('<span class="ok">会话正常</span>' if live
                     else '<span class="err">已断开</span><span class="small">（占用已过期，巡检会自动解除）</span>')
            form = ('<form method="post" action="/web/clients/cmd" class="inline-form"'
                    ' onsubmit="return confirm(\'解除 ' + html.escape(img_name)
                    + ' 的回写占用？只有确认占用者已关机/断线时才点。\')">'
                    + self._csrf_hidden()
                    + '<input type="hidden" name="action" value="release_writeback">'
                    + '<input type="hidden" name="img" value="' + html.escape(img_name) + '">'
                    + '<input type="submit" value="手动解除占用"></form>')
            wb_lines.append('<p>' + html.escape(img_name) + ' ← ' + html.escape(owner) + '　'
                            + state + '　' + form + '</p>')
        wb_items = "".join(wb_lines) or "<p>无</p>"
        exp_items = "".join(f"{html.escape(k)} → {html.escape(v.get('iqn', ''))}<br>"
                            for k, v in sorted(web_exported.items())) or "无"
        msg_html = '<p class="ok">' + html.escape(msg) + '</p>' if msg else ''
        body = (
            msg_html
            + '<div class="card"><h2>客户机名单</h2>'
            '<table><tr><th>MAC</th><th>镜像</th><th>模式</th><th>状态</th><th>IP</th><th>资源</th></tr>'
            + trs + '</table>'
            '<p class="small">“在线”表示当前有 iSCSI 连接（I_T nexus）；服务重启后内存记录会清空。</p></div>'
            '<div class="card"><h2>回写模式占用</h2>' + wb_items
            + '<p class="small">回写模式会直接修改母盘，同一母盘同一时刻只允许一台机器回写。'
            '占用只在<b>占用者有 iSCSI 会话</b>期间有效：客户机一关机/断线，'
            '约 ' + str(WRITEBACK_LOCK_GRACE) + ' 秒后由巡检自动解除（占用者仍可在这段时间内重连）。'
            '客户机拔电、跨 VLAN 探测不到、或状态卡住时，用上面的「手动解除占用」。</p></div>'
            + '<div class="card"><h2>后台手动挂载（iSCSI）</h2><p>' + exp_items + '</p>'
            '<p class="small">管理入口：<a href="/web/export">iSCSI 挂载</a>。'
            '挂载中的镜像不可再被 PXE 叠加启动或回写。</p></div>'
        )
        self._send_html(_page_html(WEB_TITLE, body, NAV_ADMIN))

    def _create_page(self, msg=None):
        body = ('<div class="card"><h2>创建空白硬盘文件</h2>'
                '<form method="post" action="/web/create">' + self._csrf_hidden() +
                '<p>文件名（不含 .raw 后缀，仅限字母/数字/下划线/短横线）：</p>'
                '<p><input type="text" name="name" required></p>'
                '<p>大小（如 20G、500M、2T，上限 ' + str(BLANK_SIZE_MAX // (1 << 30)) + 'G）：</p>'
                '<p><input type="text" name="size" placeholder="20G" required></p>')
        if msg:
            body += ('<p class="' + ('ok' if msg.startswith("创建成功") else 'err') + '">'
                     + html.escape(msg) + '</p>')
        body += ('<p><input type="submit" value="创建"></p></form></div>'
                 '<p class="small">创建后会自动出现在 iPXE 启动菜单里。</p>')
        self._send_html(_page_html("创建空白盘", body, NAV_ADMIN))

    def _password_page(self, msg=None):
        body = ('<div class="card"><h2>修改管理员密码</h2>'
                '<form method="post" action="/web/password">' + self._csrf_hidden() +
                '<p>旧密码：<br><input type="password" name="old" required></p>'
                '<p>新密码（6-32 位，仅限字母/数字/下划线/短横线）：<br>'
                '<input type="password" name="new" required></p>')
        if msg:
            body += ('<p class="' + ('ok' if msg.startswith("密码已修改") else 'err') + '">'
                     + html.escape(msg) + '</p>')
        body += ('<p><input type="submit" value="保存"></p></form></div>'
                 '<p class="small">iPXE 启动菜单的管理员模式与本后台共用同一密码。</p>')
        self._send_html(_page_html("修改密码", body, NAV_ADMIN))

    # ---------- 管理员：iSCSI 手动挂载（把空闲母盘直出为 iSCSI 盘，写直达母盘） ----------
    def _export_page(self, msg=None):
        rows = []
        for name in list_images():
            path = os.path.join(IMAGES_DIR, name + ".raw")
            size = _human_size(_file_size_or_none(path))  # 读取失败返回 "?"
            reason = image_busy_reason(name)
            if reason is None:
                st = '<span class="ok">空闲</span>'
                act = ('<form method="post" action="/web/export" class="inline-form" '
                       'onsubmit="return confirm(\'确定挂载？该盘写直达母盘镜像，'
                       '请确认镜像当前未被其他机器使用。\')">'
                       + self._csrf_hidden()
                       + '<input type="hidden" name="img" value="' + html.escape(name) + '">'
                       + '<input type="submit" value="挂载"></form>')
            else:
                st = html.escape(reason)
                act = '—'
            rows.append('<tr><td>' + html.escape(name) + '</td><td>' + size
                        + '</td><td>' + st + '</td><td>' + act + '</td></tr>')
        table = ('<table><tr><th>镜像</th><th>大小</th><th>状态</th><th>操作</th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="4">暂无镜像（images/ 目录为空）</td></tr>')
                 + '</table>')
        msg_html = ''
        if msg:
            cls = 'ok' if (msg.startswith("挂载成功") or msg.startswith("已卸载")) else 'err'
            msg_html = '<p class="' + cls + '">' + html.escape(msg) + '</p>'
        body = ('<div class="card"><h2>母盘镜像（空闲才可挂载）</h2>' + msg_html + table
                + '<p class="small">“挂载”= 把该母盘 .raw 直接导出为 iSCSI target（写直达母盘），'
                + '挂载成功后 IQN 见下表。挂载中的镜像不可再被 PXE 叠加启动或回写。</p></div>')
        exp_rows = []
        for name in sorted(web_exported):
            rec = web_exported[name]
            exp_rows.append(
                '<tr><td>' + html.escape(name) + '</td><td><b>' + html.escape(rec.get("iqn", ""))
                + '</b></td><td>' + html.escape(rec.get("created") or '')
                + '</td><td><form method="post" action="/web/export/unmount" class="inline-form" '
                + 'onsubmit="return confirm(\'确定卸载该 iSCSI 盘？正在连接的机器会立即断开，'
                + '未落盘的数据可能丢失。\')">'
                + self._csrf_hidden()
                + '<input type="hidden" name="img" value="' + html.escape(name) + '">'
                + '<input type="submit" value="卸载"></form></td></tr>')
        exp_table = ('<table><tr><th>镜像</th><th>IQN（发起端连这个）</th><th>挂载时间</th><th>操作</th></tr>'
                     + ("".join(exp_rows) if exp_rows else '<tr><td colspan="4">暂无手动挂载的盘</td></tr>')
                     + '</table>')
        body += ('<div class="card"><h2>当前手动挂载的 iSCSI 盘</h2>' + exp_table
                 + '<p class="small">客户机 iSCSI 发起端连接：服务器地址 <b>服务器IP:3260</b>，'
                 + '目标 IQN 取上表。服务重启后挂载列表会清空，需重新挂载。</p></div>')
        body += ('<div class="card"><h2>连接示例</h2>'
                 + '<p class="small">Linux（open-iscsi）：</p><pre>'
                 + 'iscsiadm -m discovery -t sendtargets -p 服务器IP\n'
                 + 'iscsiadm -m node -T iqn.2026-07.storage:web-镜像名 -p 服务器IP --login\n'
                 + 'iscsiadm -m node -T iqn.2026-07.storage:web-镜像名 -p 服务器IP --logout</pre>'
                 + '<p class="small">Windows：打开“iSCSI 发起程序”→“目标”→“连接”，'
                 + '填服务器 IP 后在上表选择要连的 IQN（写直达母盘，请谨慎操作）。</p></div>')
        self._send_html(_page_html("iSCSI 挂载", body, NAV_ADMIN))

    # ---------- VNC 控制（后台内嵌 noVNC：浏览器 --ws--> 服务器 --> 客户机 5900） ----------
    # 静态资源是 vendored 的 noVNC（web/vnc/core 与 web/vnc/vendor，MPL-2.0）；
    # 只有管理员会话能访问，普通用户的网盘页面不涉及这些 JS（继续 IE11 兼容）。
    VNC_MIME = {".js": "text/javascript", ".mjs": "text/javascript",
                ".html": "text/html; charset=utf-8", ".css": "text/css",
                ".json": "application/json", ".png": "image/png",
                ".svg": "image/svg+xml", ".gif": "image/gif", ".ico": "image/x-icon",
                ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
                ".map": "application/json"}

    def _vnc_require_admin(self):
        """VNC 页面与桥都只给管理员。返回会话或 None（已写出响应）。"""
        s = self._session()
        if not s:
            self._redirect("/")
            return None
        if s.get("role") != "admin":
            self.send_error(403, "forbidden")
            return None
        return s

    def _vnc_info(self, params):
        mac = "".join(c for c in (params.get("mac", [""])[0]).lower()
                      if c in "0123456789abcdef")
        rec = agent_hub.record_of(mac)
        if not rec:
            self._send_json({"ok": False, "mac": mac, "err": "该客户机还没跑 agent（没有心跳记录）"})
            return
        self._send_json({"ok": True, "mac": mac, "online": rec.get("online", False),
                         "hostname": rec.get("hostname", ""), "ip": rec.get("ip", ""),
                         "vnc_running": rec.get("vnc_running", False),
                         "vnc_port": rec.get("vnc_port") or 5900,
                         "drive": rec.get("drive", "")})

    def _serve_vnc_file(self, path):
        """从 web/vnc/ 下发 noVNC 静态文件（防目录穿越，按扩展名给 MIME）。"""
        rel = urllib.parse.unquote(path[len("/vnc/"):])
        ext = os.path.splitext(rel)[1].lower()
        if ext not in self.VNC_MIME:
            self.send_error(404)
            return
        full = os.path.normpath(os.path.join(WEB_VNC_DIR, rel.replace("/", os.sep)))
        if not full.startswith(WEB_VNC_DIR + os.sep) or not os.path.isfile(full):
            self.send_error(404)
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", self.VNC_MIME[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=600")
        self.end_headers()
        self.wfile.write(data)

    def _vnc_dispatch(self, path, params):
        """处理 /vnc、/vnc/info、/vnc/ws 与 /vnc/<静态文件>。"""
        if path == "/vnc/ws":
            if not self._vnc_require_admin():
                return
            mac = "".join(c for c in (params.get("mac", [""])[0]).lower()
                          if c in "0123456789abcdef")
            rec = agent_hub.record_of(mac)
            if not rec:
                self.send_error(502, "unknown client")
                return
            host = rec.get("ip") or ""
            port = rec.get("vnc_port") or 5900
            if not host:
                self.send_error(502, "client has no known IP")
                return
            try:
                wsbridge.handle(self, host, port)
            except wsbridge.BridgeError as e:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [ERROR] VNC bridge to {mac} ({host}:{port}) failed: {e.msg}")
                self.send_error(e.code, e.msg)
            except Exception as e:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [ERROR] VNC bridge error: {e}")
                self.send_error(500)
            return
        if not self._vnc_require_admin():
            return
        if path == "/vnc/info":
            self._vnc_info(params)
            return
        if path == "/vnc":
            self._serve_vnc_file("/vnc/viewer.html")
            return
        self._serve_vnc_file(path)

    # ---------- WebDAV：个人网盘挂载（客户机映射盘符用） ----------
    def _dav_auth(self):
        """WebDAV 的 HTTP Basic 认证。返回 (网盘用户名, 是否只读)；失败返回 (None, False)。

        三种身份：
          1) MAC + agent 接入令牌 → 未登录的客户机：挂的是**只读“通用文件”公共盘**
          2) 账号 + 密码 → 浏览器 / iSCSI 发起端手工连：完整个人网盘（可写，根目录里
             还能看到只读的“通用文件”子目录）
          3) 账号 + 登录会话令牌 → 客户机托盘「登录网盘」后换来的（客户机上不存密码）
        """
        hdr = self.headers.get("Authorization", "")
        if not hdr.lower().startswith("basic "):
            return None, False
        try:
            raw = base64.b64decode(hdr.split(None, 1)[1].strip()).decode("utf-8", "replace")
        except Exception:
            return None, False
        user, sep, pwd = raw.partition(":")
        if not sep:
            return None, False
        user = user.strip()
        if not user:
            return None, False
        if agent_hub.check_token(pwd):
            mac = "".join(c for c in user.lower() if c in "0123456789abcdef")
            return (mac, True) if len(mac) == 12 else (None, False)
        sess_user = agent_hub.dav_session_user(pwd)
        if sess_user and sess_user == user:
            return user, False
        return (user, False) if users_auth.check_login(user, pwd) else (None, False)

    def _dav_dispatch(self):
        """把 /dav/ 下的请求交给 webdav 模块。"""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == webdav.MOUNT_PREFIX.rstrip("/"):   # /dav -> /dav/（发起端少写斜杠时）
            self.send_response(301)
            self.send_header("Location", webdav.MOUNT_PREFIX)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not parsed.path.startswith(webdav.MOUNT_PREFIX):
            self.send_error(404)
            return
        user, readonly = self._dav_auth()
        if not user:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="iSCSI Broker cloud"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not readonly:
            # 首次使用自动建网盘根目录（与网盘页面一致），否则客户机挂载会拿到 404
            try:
                os.makedirs(cloud_store.user_home(user), exist_ok=True)
            except Exception:
                pass
        rel = urllib.parse.unquote(parsed.path[len(webdav.MOUNT_PREFIX):]).strip("/")
        webdav.handle(self, user, rel, readonly)

    def do_OPTIONS(self):
        """任何 OPTIONS 都按 WebDAV 做能力应答。

        Windows 自带的 WebDAV 重定向器（WebClient / DavClnt）在连
        http://<服务器>:8080/dav/ 之前要先做能力探测，而且**只认响应里带
        `DAV:` 应答头的结果**；探测的请求目标随版本/URL 写法不同，实测见过
        `*`、`/`、`/dav/` 几种。老代码只对 /dav/ 应答、其余回 404，重定向器
        就把服务器当成“不是 WebDAV”，`net use` 直接报“系统错误 67 找不到
        网络名”（服务器日志里连一条 PROPFIND 都不会出现）。
        所以这里不挑路径，任何 OPTIONS 一律回 DAV 头——能力应答不带凭据，
        也必须在认证之前给出。浏览器/其它工具拿到这组头也无副作用。
        """
        webdav._do_options(self)

    def do_PROPFIND(self):
        self._dav_dispatch()

    def do_PUT(self):
        self._dav_dispatch()

    def do_MKCOL(self):
        self._dav_dispatch()

    def do_DELETE(self):
        self._dav_dispatch()

    def do_MOVE(self):
        self._dav_dispatch()

    def do_LOCK(self):
        self._dav_dispatch()

    def do_UNLOCK(self):
        self._dav_dispatch()

    def do_HEAD(self):
        if urllib.parse.urlparse(self.path).path.startswith(webdav.MOUNT_PREFIX):
            self._dav_dispatch()
            return
        self.send_error(404)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/vnc" or path.startswith("/vnc/"):
            self._vnc_dispatch(path, urllib.parse.parse_qs(parsed.query))
            return
        if path == "/dav" or path.startswith(webdav.MOUNT_PREFIX):
            self._dav_dispatch()
            return
        if path in ("/", "/web", "/web/"):
            s = self._session()
            if not s:
                q = urllib.parse.parse_qs(parsed.query)
                self._login_page(q.get("msg", [""])[0] or None)
                return
            if s.get("role") == "user":
                self._redirect("/web/drive")   # 普通用户无客户机名单权限
                return
            self._dashboard(urllib.parse.parse_qs(parsed.query).get("msg", [""])[0] or None)
            return
        if path == "/web/register":   # 公开注册页（无需会话）
            self._register_page()
            return
        # 客户机托盘「检查更新」：不走浏览器会话，靠 agent 接入令牌认证（见 _agent_version_info）
        if path in ("/agent/version", "/agent/exe"):
            if path == "/agent/version":
                self._agent_version_info()
            else:
                self._agent_exe()
            return
        if not self._require_session():
            return
        if path == "/web/logout":
            tok = self._cookie()
            with web_lock:
                if tok:
                    web_sessions.pop(tok, None)
            self._redirect("/")
            return
        params = urllib.parse.parse_qs(parsed.query)
        if path in ("/web/drive", "/web/download"):
            s = self._session()
            if not self._role_gate(s, "user"):
                return
            if path == "/web/drive":
                self._drive_page(s["user"], params.get("p", [""])[0],
                                 params.get("msg", [""])[0] or None)
            else:
                self._do_download(s)
            return
        if path in ("/web/users", "/web/settings", "/web/common", "/web/create",
                    "/web/password", "/web/common/download", "/web/netctrl", "/web/export",
                    "/web/clients"):
            s = self._session()
            if not self._role_gate(s, "admin"):
                return
            if path == "/web/create":
                self._create_page(params.get("msg", [""])[0] or None)
            elif path == "/web/password":
                self._password_page(params.get("msg", [""])[0] or None)
            elif path == "/web/users":
                self._users_page(params.get("msg", [""])[0] or None)
            elif path == "/web/clients":
                self._clients_page(params.get("msg", [""])[0] or None)
            elif path == "/web/settings":
                self._settings_page(params.get("msg", [""])[0] or None)
            elif path == "/web/netctrl":
                self._netctrl_page(params.get("msg", [""])[0] or None)
            elif path == "/web/export":
                self._export_page(params.get("msg", [""])[0] or None)
            elif path == "/web/common":
                self._common_page(params.get("msg", [""])[0] or None)
            else:
                self._do_common_download()
            return
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # 客户机 agent 通道：不走会话 Cookie，靠接入令牌认证（见 agent_hub）
        if path == "/agent/poll":
            self._agent_poll()
            return
        if path == "/agent/result":
            self._agent_result()
            return
        # 客户机托盘的状态上报（挂了没挂 Z 盘、登录了谁），不影响指令队列
        if path == "/agent/status":
            self._agent_status()
            return
        # 客户机托盘「登录网盘」：账号密码换成短期会话令牌（客户机上不存密码）
        if path == "/agent/login":
            self._agent_login()
            return
        if path == "/agent/logout":
            self._agent_logout()
            return
        if path == "/web/login":
            self._do_login(self._form())
            return
        if path == "/web/register":
            self._do_register(self._form())
            return
        s = self._require_session()
        if not s:
            return
        if path == "/web/upload":
            if not self._role_gate(s, "user"):
                return
            self._do_upload(s)   # 流式上传（不缓冲整包）：CSRF 令牌走查询串
            return
        if path == "/web/common/upload":
            if not self._role_gate(s, "admin"):
                return
            self._do_common_upload(s)
            return
        if path == "/web/clients/upload":     # 客户端 exe 上传（multipart，自带 csrf 字段）
            if not self._role_gate(s, "admin"):
                return
            self._do_client_upload(s)
            return
        form = self._form()
        if form.get("csrf", [""])[0] != s["csrf"]:   # 简单 CSRF 校验
            self._redirect("/")
            return
        if path == "/web/mkdir":
            if not self._role_gate(s, "user"):
                return
            self._do_mkdir(form, s)
            return
        if path == "/web/delete":
            if not self._role_gate(s, "user"):
                return
            self._do_delete(form, s)
            return
        if path in ("/web/users/quota", "/web/settings", "/web/common/delete",
                    "/web/create", "/web/password",
                    "/web/netctrl/default", "/web/netctrl/mac",
                    "/web/export", "/web/export/unmount", "/web/clients/cmd"):
            if not self._role_gate(s, "admin"):
                return
            if path == "/web/users/quota":
                self._do_users_quota(form)
            elif path == "/web/clients/cmd":
                self._do_clients_cmd(form)
            elif path == "/web/settings":
                self._do_settings(form)
            elif path == "/web/netctrl/default":
                self._do_netctrl_default(form)
            elif path == "/web/netctrl/mac":
                self._do_netctrl_mac(form)
            elif path == "/web/common/delete":
                self._do_common_delete(form)
            elif path == "/web/create":
                self._do_create(form)
            elif path == "/web/export":
                self._do_export_mount(form)
            elif path == "/web/export/unmount":
                self._do_export_unmount(form)
            else:
                self._do_password(form)
            return
        self.send_error(404)

    def _do_login(self, form):
        user = form.get("user", [""])[0].strip()
        pwd = form.get("pwd", [""])[0]
        role = users_auth.check_login(user, pwd)
        if not role:
            time.sleep(1)  # 限速防暴力猜密码
            self._login_page("用户名或密码错误")
            return
        tok = secrets.token_hex(16)
        with web_lock:
            now = time.time()
            for t in [t for t, s in web_sessions.items() if now > s["exp"]]:
                web_sessions.pop(t, None)
            web_sessions[tok] = {"exp": now + SESSION_TTL, "csrf": secrets.token_hex(8),
                                 "user": user, "role": role}
        self.send_response(302)
        self.send_header("Location", "/" if role == "admin" else "/web/drive")
        self.send_header("Set-Cookie", f"admin_session={tok}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _do_create(self, form):
        name = form.get("name", [""])[0].strip()
        size = form.get("size", [""])[0].strip()
        msg = self._create_blank(name, size)
        self._redirect("/web/create?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_export_mount(self, form):
        img = form.get("img", [""])[0].strip()
        ok, res = export_image_mount(img)
        msg = f"挂载成功，IQN：{res}" if ok else res
        self._redirect("/web/export?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_export_unmount(self, form):
        img = form.get("img", [""])[0].strip()
        _ok, res = export_image_unmount(img)
        self._redirect("/web/export?msg=" + urllib.parse.quote(res, safe=""))

    def _create_blank(self, name, size):
        """校验并创建空白盘，返回给用户展示的结果消息。"""
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name) or name.startswith("."):
            return f"文件名不合法：{name or '（空）'}"
        target = os.path.join(IMAGES_DIR, name + ".raw")
        if os.path.exists(target):
            return f"同名镜像已存在：{name}.raw"
        try:
            bytes_n = parse_size_to_bytes(size)
        except (ValueError, OverflowError):
            bytes_n = None
        if bytes_n is None or bytes_n <= 0:
            return f"大小格式不正确：{size or '（空）'}"
        if bytes_n > BLANK_SIZE_MAX:
            return f"大小超过上限 {BLANK_SIZE_MAX // (1 << 30)}G"
        try:
            run_cmd(["truncate", "-s", size, target], timeout=60)
        except Exception as e:
            return f"创建失败：{e}"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [SUCCESS] Blank disk created: {target} ({size}) via web")
        return f"创建成功：{name}.raw（{size}），已加入启动菜单"

    def _do_password(self, form):
        old = form.get("old", [""])[0]
        new = form.get("new", [""])[0]
        if not verify_password(old, ADMIN_PW_STORED or ""):
            msg = "旧密码错误"
        elif not re.fullmatch(r"[A-Za-z0-9_\-]{6,32}", new):
            msg = "新密码需为 6-32 位字母/数字/下划线/短横线"
        elif secrets.compare_digest(new, old):
            msg = "新密码与旧密码相同"
        else:
            try:
                set_admin_password(new)
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] [SUCCESS] Admin password changed via web")
                msg = "密码已修改（iPXE 管理员模式与本后台共用）"
            except Exception as e:
                msg = f"保存失败：{e}"
        self._redirect("/web/password?msg=" + urllib.parse.quote(msg, safe=""))

    # ---------- 注册 / 角色守卫 ----------
    def _register_page(self, err=None):
        body = ('<div class="card"><h2>注册账号</h2>'
                '<form method="post" action="/web/register">'
                '<p>用户名（3-32 位字母/数字/下划线/短横线）：<br>'
                '<input type="text" name="user" maxlength="32"></p>'
                '<p>密码（6-32 位字母/数字/下划线/短横线）：<br>'
                '<input type="password" name="pwd" maxlength="32"></p>')
        if err:
            body += '<p class="err">' + html.escape(err) + '</p>'
        body += ('<p><input type="submit" value="注册"></p></form></div>'
                 '<p><a href="/">返回登录</a></p>')
        self._send_html(_page_html("注册账号", body, None))

    def _do_register(self, form):
        user = form.get("user", [""])[0].strip()
        pwd = form.get("pwd", [""])[0]
        ok, msg = users_auth.register_user(user, pwd)
        if ok:
            self._redirect("/?msg=" + urllib.parse.quote("注册成功，请登录", safe=""))
        else:
            self._register_page(msg)

    def _role_gate(self, s, required):
        """角色守卫：required='admin'/'user'。不符时重定向（普通用户→/web/drive，管理员→/）。"""
        if s.get("role") == required:
            return True
        self._redirect("/" if s.get("role") == "admin" else "/web/drive")
        return False

    # ---------- 用户个人网盘 ----------
    def _drive_page(self, user, rel, msg=None):
        try:
            os.makedirs(cloud_store.user_home(user), exist_ok=True)  # 新账号首次进入自动建根目录
        except Exception:
            pass
        listing = cloud_store.list_dir(user, rel)
        if not listing["ok"]:
            body = ('<div class="card"><p class="err">' + html.escape(listing["msg"]) + '</p>'
                    '<p><a href="/web/drive">返回根目录</a></p></div>')
            self._send_html(_page_html("我的网盘", body, NAV_USER))
            return
        used = cloud_store.quota_used(user)
        quota = users_auth.quota_of(user)
        head = ('<div class="card"><p>当前账号：<b>' + html.escape(user) + '</b>　配额使用：'
                + _human_size(used) + ' / ' + (_human_size(quota) if quota > 0 else "无配额")
                + '</p><p class="small">' + _breadcrumb_html(rel) + '</p></div>')
        in_common = rel == cloud_store.VIRTUAL_COMMON or rel.startswith(
            cloud_store.VIRTUAL_COMMON + "/")
        rows = []
        for f in listing["folders"]:
            cell = ('<a href="/web/drive?p=' + urllib.parse.quote(f["rel"], safe="")
                    + '">' + html.escape(f["name"]) + '/</a>')
            if f["rel"] == cloud_store.VIRTUAL_COMMON:
                # 虚拟“通用文件”入口：只读区，不提供删除
                rows.append('<tr><td>' + cell + '</td><td>—</td><td></td></tr>')
            else:
                rows.append('<tr><td>' + cell + '</td><td>—</td><td>'
                            + self._delete_form(f["rel"], folder=True) + '</td></tr>')
        for fl in listing["files"]:
            rows.append('<tr><td>' + html.escape(fl["name"]) + '</td><td>'
                        + _human_size(fl["size"]) + '</td><td><a href="/web/download?p='
                        + urllib.parse.quote(fl["rel"], safe="") + '">下载</a>'
                        + self._delete_form(fl["rel"]) + '</td></tr>')
        for it in listing["common"]:
            p = cloud_store.resolve(user, it["rel"])
            if p is not None and os.path.isdir(p):
                rows.append('<tr><td><a href="/web/drive?p='
                            + urllib.parse.quote(it["rel"], safe="")
                            + '">' + html.escape(it["name"]) + '/</a></td><td>—</td><td></td></tr>')
            else:
                rows.append('<tr><td>' + html.escape(it["name"]) + '</td><td>'
                            + _human_size(it["size"]) + '</td><td><a href="/web/download?p='
                            + urllib.parse.quote(it["rel"], safe="") + '">下载</a></td></tr>')
        table_rows = "".join(rows) if rows else '<tr><td colspan="3">（空）</td></tr>'
        table = ('<table><tr><th>名称</th><th>大小</th><th>操作</th></tr>'
                 + table_rows + '</table>')
        msg_html = ''
        if msg:
            cls = 'ok' if (msg.startswith("上传成功") or msg.startswith("创建成功")
                           or msg.startswith("删除成功")) else 'err'
            msg_html = '<p class="' + cls + '">' + html.escape(msg) + '</p>'
        if in_common:
            # 通用文件区只读：无上传/新建表单
            body = (head + '<div class="card"><h2>通用文件（只读）</h2>' + msg_html + table
                    + '</div><p class="small">通用文件由管理员维护，本区域只读。</p>')
        else:
            s = self._session()
            csrf = s["csrf"] if s else ""
            upload = ('<div class="card"><h2>上传文件</h2>'
                      '<form method="post" action="/web/upload?csrf=' + csrf + '&p='
                      + urllib.parse.quote(rel, safe="") + '" enctype="multipart/form-data">'
                      '<input type="hidden" name="p" value="' + html.escape(rel) + '">'
                      '<p><input type="file" name="file"></p>'
                      '<p><input type="submit" value="上传"></p></form></div>')
            mkdir = ('<div class="card"><h2>新建文件夹</h2>'
                     '<form method="post" action="/web/mkdir">' + self._csrf_hidden()
                     + '<input type="hidden" name="p" value="' + html.escape(rel) + '">'
                     + '<p>名称：<input type="text" name="name" maxlength="64"></p>'
                     + '<p><input type="submit" value="创建"></p></form></div>')
            body = (head + '<div class="card"><h2>文件列表</h2>' + msg_html + table + '</div>'
                    + upload + mkdir)
        self._send_html(_page_html("我的网盘 - " + user, body, NAV_USER))

    def _do_upload(self, s):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("csrf", [""])[0] != s["csrf"]:
            self._redirect("/web/drive")
            return
        rel = params.get("p", [""])[0]
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        quota_remaining = users_auth.quota_of(s["user"]) - cloud_store.quota_used(s["user"])
        if quota_remaining < 0:
            quota_remaining = 0
        try:
            _ok, msg = cloud_store.save_upload(s["user"], rel, self.rfile, content_length,
                                               self.headers.get("Content-Type", ""),
                                               quota_remaining)
        except Exception as e:
            msg = "上传失败：%s" % e
        back = "/web/drive" + (("?p=" + urllib.parse.quote(rel, safe="")) if rel else "")
        self._redirect(back + (("&" if "?" in back else "?") + "msg="
                               + urllib.parse.quote(msg, safe="")))

    def _do_mkdir(self, form, s):
        name = form.get("name", [""])[0].strip()
        rel = form.get("p", [""])[0]
        _ok, msg = cloud_store.create_folder(s["user"], rel, name)
        back = "/web/drive" + (("?p=" + urllib.parse.quote(rel, safe="")) if rel else "")
        self._redirect(back + (("&" if "?" in back else "?") + "msg="
                               + urllib.parse.quote(msg, safe="")))

    def _delete_form(self, rel, folder=False):
        """网盘条目删除表单（POST /web/delete）：隐藏路径 + CSRF + 确认提示。"""
        s = self._session()
        if not s or not rel:
            return ""
        tip = "确定删除该文件夹及其全部内容？此操作不可恢复" if folder \
            else "确定删除该文件？此操作不可恢复"
        return ('<form method="post" action="/web/delete" class="inline-form" '
                'onsubmit="return confirm(\'' + tip + '\')">'
                + self._csrf_hidden()
                + '<input type="hidden" name="p" value="' + html.escape(rel) + '">'
                + '<input type="submit" value="' + ('删除文件夹' if folder else '删除')
                + '"></form>')

    def _do_delete(self, form, s):
        rel = form.get("p", [""])[0]
        target = cloud_store.resolve(s["user"], rel)
        if target is not None and os.path.isdir(target) and not os.path.islink(target):
            _ok, msg = cloud_store.delete_folder(s["user"], rel)
        else:
            _ok, msg = cloud_store.delete_file(s["user"], rel)
        # 删除后回到父目录（被删的文件夹本身已不存在，不能停留其中）；
        # rel 含 ".." 等非法成分时父目录也非法，退回根目录显示错误消息
        parent = os.path.dirname(rel.rstrip("/")) if rel else ""
        if parent and cloud_store.resolve(s["user"], parent) is None:
            parent = ""
        back = "/web/drive" + (("?p=" + urllib.parse.quote(parent, safe="")) if parent else "")
        self._redirect(back + (("&" if "?" in back else "?") + "msg="
                               + urllib.parse.quote(msg, safe="")))

    def _send_file(self, path, notfound_html):
        """以 64KB 分块发送本地文件；path 为 None/不存在时返回 notfound_html 页面。"""
        if path is None or not os.path.isfile(path):
            self._send_html(notfound_html)
            return
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        enc = urllib.parse.quote(name, safe="")   # 百分号编码文件名（IE11 可识别）
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if size is not None:
            self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="' + enc + '"; filename*=UTF-8\'\'' + enc)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception:
            pass  # 客户端提前断开等，忽略

    def _do_download(self, s):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        rel = params.get("p", [""])[0]
        path = cloud_store.resolve(s["user"], rel)
        self._send_file(path, _page_html("我的网盘",
                         '<div class="card"><p class="err">文件不存在</p>'
                         '<p><a href="/web/drive">返回网盘</a></p></div>', NAV_USER))

    # ---------- 管理员：用户与配额 ----------
    def _users_page(self, msg=None):
        users = users_auth.list_users()
        rows = []
        for r in users:
            name = r["name"]
            if r["is_admin"]:
                rows.append('<tr><td>' + html.escape(name) + '</td><td>—</td><td>—</td>'
                            '<td class="small">内置账号</td></tr>')
            else:
                used = _human_size(cloud_store.quota_used(name))
                quota = r["quota_bytes"]
                rows.append(
                    '<tr><td>' + html.escape(name) + '</td><td>' + used + '</td><td>'
                    + _human_size(quota) + '</td><td><form method="post" action="/web/users/quota">'
                    + self._csrf_hidden()
                    + '<input type="hidden" name="name" value="' + html.escape(name) + '">'
                    + '<input type="text" name="quota" value="' + str(quota) + '" size="14">'
                    + '<input type="submit" value="设置"></form></td></tr>')
        table = ('<table><tr><th>用户名</th><th>已用</th><th>配额</th><th>调整配额</th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="4">暂无用户</td></tr>')
                 + '</table>')
        msg_html = ''
        if msg:
            msg_html = '<p class="' + ('ok' if msg.startswith("配额已更新") else 'err') + '">' \
                       + html.escape(msg) + '</p>'
        body = ('<div class="card"><h2>用户与配额</h2>' + msg_html + table
                + '<p class="small">配额输入支持 1G、500M、1048576 等格式；上限 8T。</p></div>')
        self._send_html(_page_html("用户与配额", body, NAV_ADMIN))

    def _do_users_quota(self, form):
        name = form.get("name", [""])[0].strip()
        quota_s = form.get("quota", [""])[0].strip()
        try:
            bytes_n = parse_size_to_bytes(quota_s)
        except (ValueError, OverflowError):
            bytes_n = None
        if bytes_n is None or bytes_n <= 0:
            msg = "配额格式不正确：" + (quota_s or "（空）")
        else:
            _ok, msg = users_auth.set_user_quota(name, bytes_n)
        self._redirect("/web/users?msg=" + urllib.parse.quote(msg, safe=""))

    # ---------- 管理员：客户机控制（agent 通道 / 关机重启 / 网盘挂载 / VNC） ----------
    # 客户机里跑 agent（见 client/），它只主动向服务器心跳，不开放入站端口：
    #   POST /agent/poll    心跳 + 取一条待执行指令
    #   POST /agent/result  回报执行结果
    # 两者都靠 agent_hub 的接入令牌认证，不走浏览器会话。
    CMD_LABELS = {"shutdown": "关机", "reboot": "重启", "vnc_start": "启用 VNC",
                  "vnc_stop": "停用 VNC", "mount": "挂载网盘", "unmount": "卸载网盘",
                  "update": "更新客户端"}

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        """读取并解析请求体 JSON（客户机 agent 用，体积很小）。非法返回 None。"""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        length = min(max(length, 0), 64 * 1024)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _dav_url(self):
        """本机 WebDAV 根地址（按当前请求的 Host 拼，兼容 HTTPS/自定义端口）。"""
        scheme = "https" if HTTPS_ENABLED else "http"
        return "%s://%s/dav/" % (scheme, self.headers.get("Host", ""))

    # ---------- 客户机客户端「检查更新」（托盘手动触发，不走浏览器会话） ----------
    #   GET /agent/version?mac=&token=&cur=1.0   查有没有比 cur 新的发布
    #   GET /agent/exe?ver=1.1&mac=&token=       下载该版本 exe（托盘校验 sha256 后自我替换）
    # 两个都用 agent 接入令牌认证；exe 不额外加密（内网 + 令牌 + sha256 校验）。
    def _agent_exe_rel(self, ver):
        """某个版本的下载地址（相对路径，托盘按自己配的 url 拼）。"""
        return "/agent/exe?ver=" + urllib.parse.quote(ver, safe="")

    def _client_exe_row(self, ver, rec=None):
        """托盘要的信息：版本元数据（不含服务器本地路径）。"""
        return {"url": self._agent_exe_rel(ver),
                "sha256": (rec or {}).get("sha256", ""),
                "size": (rec or {}).get("size", 0),
                "notes": (rec or {}).get("notes", "")}

    def _agent_version_info(self):
        """托盘「检查更新」：返回当前发布版本与是否有更新。"""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not agent_hub.check_token((q.get("token") or [""])[0]):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        cur = (q.get("cur") or [""])[0].strip()
        avail, rec = client_release.available_for(cur)
        if not avail:
            self._send_json({"ok": True, "available": False,
                             "ver": client_release.current() or cur})
            return
        out = {"ok": True, "available": True, "ver": rec["ver"]}
        out.update(self._client_exe_row(rec["ver"], rec))
        self._send_json(out)

    def _agent_exe(self):
        """下载指定版本的客户端 exe。"""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not agent_hub.check_token((q.get("token") or [""])[0]):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        ver = (q.get("ver") or [""])[0].strip() or client_release.current()
        rec = client_release.release_of(ver)
        path = client_release.path_of(ver)
        if not rec or not path:
            self._send_json({"ok": False, "err": "no such version"}, 404)
            return
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError as e:
            self._send_json({"ok": False, "err": "read failed: %s" % e}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("X-Client-Version", rec["ver"])
        self.send_header("X-Client-Sha256", rec.get("sha256", ""))
        self.send_header("Content-Disposition",
                         'attachment; filename="iscsi-broker-agent-%s.exe"' % rec["ver"])
        self.end_headers()
        self.wfile.write(blob)

    def _agent_poll(self):
        """客户机 agent 心跳：更新在线状态并下发一条待执行指令。"""
        data = self._json_body()
        if data is None:
            self._send_json({"ok": False, "err": "bad json"}, 400)
            return
        if not agent_hub.check_token(data.get("token")):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        mac, cmd = agent_hub.poll(data.get("mac"), ip=self.client_address[0], info=info)
        if not mac:
            self._send_json({"ok": False, "err": "bad mac"}, 400)
            return
        self._send_json({"ok": True, "mac": mac, "account": agent_hub.account_of(mac),
                         "dav": self._dav_url(), "mount": agent_hub.mount_wanted(mac),
                         "cmd": cmd})

    def _agent_result(self):
        """客户机 agent 回报指令执行结果。"""
        data = self._json_body()
        if data is None or not agent_hub.check_token(data.get("token")):
            self._send_json({"ok": False, "err": "bad request"}, 400)
            return
        ok, err = agent_hub.result(data.get("mac"), data.get("id"),
                                   data.get("ok", False), data.get("msg", ""))
        self._send_json({"ok": ok, "err": err}, 200 if ok else 400)

    def _agent_status(self):
        """客户机托盘上报自己的状态（Z 盘挂没挂、登录了谁）。

        与 /agent/poll 分开：托盘上报不该拿走指令（指令由 SYSTEM 进程取），
        顺带把服务器当前的“是否允许挂盘 / 默认用户名”带回去，托盘用来对状态。
        """
        data = self._json_body()
        if data is None or not agent_hub.check_token(data.get("token")):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        mac = agent_hub.touch(data.get("mac"), ip=self.client_address[0], info=info)
        if not mac:
            self._send_json({"ok": False, "err": "bad mac"}, 400)
            return
        self._send_json({"ok": True, "mac": mac, "mount": agent_hub.mount_wanted(mac),
                         "account": agent_hub.account_of(mac), "dav": self._dav_url(),
                         "avail_ver": client_release.current()})

    def _agent_login(self):
        """客户机托盘「登录网盘」：校验账号密码，换一个短期网盘会话令牌。

        这样客户机上不保存网盘密码，WebDAV 只用令牌认证；令牌过期/注销即失效。
        """
        data = self._json_body()
        if data is None or not agent_hub.check_token(data.get("token")):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        ip = self.client_address[0]
        if dav_login_ratelimited(ip):
            self._send_json({"ok": False, "err": "密码错误次数太多，请稍后再试"}, 429)
            return
        user = (data.get("user") or "").strip()
        pwd = data.get("pwd") or ""
        role = users_auth.check_login(user, pwd)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not role:
            dav_login_failed(ip)
            time.sleep(1)                       # 限速防暴力猜密码
            print(f"[{now}] [ERROR] DAV login failed for {user!r} from {ip}")
            self._send_json({"ok": False, "err": "用户名或密码错误"}, 403)
            return
        dav_login_reset(ip)
        tok = agent_hub.dav_session_issue(user)
        agent_hub.set_dav_user(data.get("mac"), user)
        print(f"[{now}] [SUCCESS] DAV login: {user} ({role}) from {ip}")
        self._send_json({"ok": True, "user": user, "role": role, "token": tok,
                         "dav": self._dav_url(), "letter": data.get("letter") or ""})

    def _agent_logout(self):
        """客户机托盘「注销网盘」：让会话令牌立刻失效。"""
        data = self._json_body()
        if data is None or not agent_hub.check_token(data.get("token")):
            self._send_json({"ok": False, "err": "bad token"}, 403)
            return
        agent_hub.dav_session_revoke(data.get("session"))
        agent_hub.set_dav_user(data.get("mac"), "")
        self._send_json({"ok": True})

    def _cmd_form(self, mac, action, label, confirm=None):
        """生成“下发指令”的内联表单。"""
        onsubmit = ''
        if confirm:
            onsubmit = ' onsubmit="return confirm(\'' + confirm.replace("'", "") + '\')"'
        return ('<form method="post" action="/web/clients/cmd" class="inline-form"' + onsubmit + '>'
                + self._csrf_hidden()
                + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                + '<input type="hidden" name="action" value="' + html.escape(action) + '">'
                + '<input type="submit" value="' + html.escape(label) + '"></form>')

    def _client_row(self, rec, ipxe=None):
        """渲染客户机控制表的一行。rec 为 agent_hub 记录（或仅有 mac 的占位记录）。"""
        mac = rec.get("mac", "")
        online = bool(rec.get("online"))
        if online:
            st = '<span class="ok">在线</span>'
        elif ipxe:
            st = '<span class="err">未装 agent</span>'
        else:
            st = '离线'
        sep = ' '
        acts = []
        if online:
            acts.append(self._cmd_form(mac, "shutdown", "关机", "确定远程关机？客户机将立即关闭。"))
            acts.append(self._cmd_form(mac, "reboot", "重启", "确定远程重启？客户机将立即重启。"))
            if rec.get("vnc_running"):
                acts.append(self._cmd_form(mac, "vnc_stop", "停用 VNC"))
                acts.append('<a href="/vnc?mac=' + urllib.parse.quote(mac, safe="")
                            + '" target="_blank">VNC 画面</a>')
            else:
                acts.append(self._cmd_form(mac, "vnc_start", "启用 VNC"))
            if rec.get("want_mount"):
                acts.append(self._cmd_form(mac, "unmount", "停用网盘",
                                           "停用后该客户机的 Z 盘会被卸载，确定？"))
            else:
                acts.append(self._cmd_form(mac, "mount", "启用网盘"))
            acts.append(self._upgrade_form(mac, rec))
        else:
            acts.append('—')
        # 默认用户名设置表单（只用于预填客户机托盘的登录框，不保存密码；离线也能先设好）
        acc_form = ('<form method="post" action="/web/clients/cmd" class="inline-form">'
                    + self._csrf_hidden()
                    + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                    + '<input type="hidden" name="action" value="set_account">'
                    + '<input type="text" name="account" size="8" value="'
                    + html.escape(rec.get("account") or "") + '">'
                    + '<input type="submit" value="保存"></form>')
        res = rec.get("results") or []
        if res:
            r = res[0]
            cls = 'ok' if r.get("ok") else 'err'
            when = datetime.datetime.fromtimestamp(r.get("ts") or 0).strftime("%m-%d %H:%M:%S")
            res_html = ('<span class="' + cls + '">' + html.escape(self.CMD_LABELS.get(
                r.get("type") or "", r.get("type") or "指令")) + (' 成功' if r.get("ok") else ' 失败')
                + '</span> <span class="small">' + when + '</span>')
            if r.get("msg"):
                res_html += '<br><span class="small">' + html.escape(r["msg"]) + '</span>'
        else:
            res_html = '<span class="small">—</span>'
        vnc = ('<span class="ok">运行中 :%s</span>' % (rec.get("vnc_port") or 5900)) \
            if rec.get("vnc_running") else '<span class="small">未运行</span>'
        if rec.get("drive"):
            drive_txt = html.escape(rec["drive"])
            if rec.get("dav_user"):
                drive_txt += '<br><span class="ok">已登录 ' + html.escape(rec["dav_user"]) + '</span>'
            else:
                drive_txt += '<br><span class="small">未登录（只读通用文件）</span>'
        elif rec.get("want_mount"):
            drive_txt = '<span class="small">待挂载</span>'
        else:
            drive_txt = '<span class="small">已停用</span>'
        ipxe_txt = ""
        if ipxe:
            ipxe_txt = '<br><span class="small">' + html.escape(
                ("iPXE: %s %s" % (ipxe.get("img") or "-", ipxe.get("mode") or "")).strip()) + '</span>'
        # 客户端版本列：服务器当前发布版 vs 这台机器心跳里报的版本
        av = client_release.current()
        cv = (rec.get("agent_ver") or "").strip()
        if not cv:
            ver_txt = '<span class="small">未知</span>'
        elif av and cv != av:
            ver_txt = ('<span class="err">' + html.escape(cv) + '</span><br><span class="small">可升 '
                       + html.escape(av) + '</span>')
        else:
            ver_txt = '<span class="ok">' + html.escape(cv) + '</span>'
        return ('<tr><td>' + html.escape(mac) + ipxe_txt + '</td><td>'
                + html.escape(rec.get("hostname") or "—") + '</td><td>'
                + html.escape(rec.get("ip") or "—") + '</td><td>' + st + '</td><td>'
                + drive_txt + '</td><td>' + vnc + '</td><td>'
                + sep.join(acts) + '</td><td>' + acc_form + '</td><td>' + ver_txt
                + '</td><td>' + res_html + '</td></tr>')

    def _upgrade_form(self, mac, rec=None):
        """单个客户机的「升级客户端」按钮（同版本时是强制重装）。"""
        av = client_release.current()
        cv = ((rec or {}).get("agent_ver") or "").strip()
        if not av:
            return '<span class="small" title="服务器还没有发布客户端">升级客户端</span>'
        if cv and cv == av:
            label, force, confirm = "重装 v" + av, "1", "该机已是 v%s，强制重装客户端？" % av
        else:
            label, force = "升级到 v" + av, "0"
            confirm = "把该客户机升级到 v%s？客户机会下载新版本并重启自己。" % av
        return ('<form method="post" action="/web/clients/cmd" class="inline-form"'
                + ' onsubmit="return confirm(\'' + confirm.replace("'", "") + '\')">'
                + self._csrf_hidden()
                + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                + '<input type="hidden" name="action" value="upgrade_one">'
                + '<input type="hidden" name="force" value="' + force + '">'
                + '<input type="submit" value="' + html.escape(label) + '"></form>')

    def _clients_page(self, msg=None):
        records = agent_hub.snapshot()
        seen = {r["mac"] for r in records}
        rows = [self._client_row(r) for r in records]
        ipxe_only = 0
        for c in collect_client_info():          # iPXE 侧见过的机器（用于对照谁还没装 agent）
            if c["mac"] in seen:
                continue
            ipxe_only += 1
            rows.append(self._client_row({"mac": c["mac"], "online": False, "ip": c.get("ip") or "",
                                          "hostname": "", "vnc_running": False, "vnc_port": 0,
                                          "drive": "", "account": "", "results": [],
                                          "agent_ver": ""}, ipxe=c))
        table = ('<table><tr><th>MAC</th><th>主机名</th><th>IP</th><th>agent</th><th>Z 盘</th>'
                 '<th>VNC</th><th>操作</th><th>默认用户名</th><th>客户端版本</th>'
                 '<th>最近结果</th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="10">暂无客户机记录（客户机 agent 还没心跳过）</td></tr>')
                 + '</table>')
        msg_html = ''
        if msg:
            # 提示颜色：明确以“失败：”开头的（编译/上传/删除等）一定是错误，不要显示成绿色成功
            cls = 'err' if (msg.startswith("失败") or "失败" in msg or "不在线" in msg
                            or "不合法" in msg or "请先" in msg or "已满" in msg
                            or "找不到" in msg) else 'ok'
            msg_html = '<p class="' + cls + '">' + html.escape(msg) + '</p>'
        tok = agent_hub.token()
        body = (
            '<div class="card"><h2>客户机 agent 接入令牌</h2>' + msg_html
            + '<p class="small">把这个令牌和服务器地址填进客户机母盘的 <b>agent.ini</b>。'
            + '令牌同时用于 agent 通道和网盘（WebDAV）认证，客户机上不需要存网盘密码。</p>'
            + '<pre>' + html.escape(tok) + '</pre>'
            + '<form method="post" action="/web/clients/cmd" class="inline-form" '
            + 'onsubmit="return confirm(\'重置后所有客户机都要改 agent.ini，确定？\')">'
            + self._csrf_hidden()
            + '<input type="hidden" name="action" value="rotate_token">'
            + '<input type="submit" value="重置令牌"></form></div>'
            + self._release_card()
            + '<div class="card"><h2>客户机控制</h2>' + table
            + '<p class="small">“在线”= 最近 %d 秒内收到过 agent 心跳。指令随心跳下发，'
            % agent_hub.ONLINE_WINDOW
            + '关机/重启会立即执行；VNC 需要母盘里装好 VNC 服务端并在 agent.ini 里配好路径。</p>'
            + self._upgrade_all_form()
            + '<p class="small"><b>升级客户端</b>：客户机托盘「检查更新」可以自己拉新版；'
            + '这里点「升级」是下发一条指令，客户机下次心跳时下载新 exe 并自我替换重启。'
            + '无盘客户机重启后会回到母盘里的旧版本，需要重做母盘或再升一次。</p>'
            + '<p class="small"><b>网盘（Z 盘）</b>：默认自动挂载，内容是服务器的“通用文件”（只读）；'
            + '用户在客户机右下角托盘里<b>登录自己的网盘账号</b>后，Z 盘换成他自己的网盘（可写，'
            + '里面仍能看到只读的“通用文件”）。「停用网盘」= 不给这台机器挂 Z 盘；'
            + '“默认用户名”只是预填托盘的登录框，<b>不保存密码</b>，每次开机用户自己输一次。</p>'
            + ('<p class="small">另有 %d 台机器只有 iPXE 记录、没有 agent 心跳（母盘里还没装客户端）。</p>'
               % ipxe_only if ipxe_only else '')
            + '</div>'
            '<div class="card"><h2>客户机 agent 配置示例（母盘：agent.ini）</h2>'
            + '<pre>' + html.escape(
                "[server]\n"
                "url=http://%s\n" % (self.headers.get("Host", "10.1.1.1:8080"))
                + "token=" + tok + "\n"
                "interval=3\n\n"
                "[vnc]\nexe=C:\\Program Files\\TightVNC\\tvnserver.exe\nargs=-run\nport=5900\n\n"
                "[dav]\nletter=Z\n") + '</pre>'
            + '<p class="small">完整部署步骤见仓库 README「客户机客户端」一节。</p></div>'
        )
        self._send_html(_page_html("客户机控制", body, NAV_ADMIN))

    def _upgrade_all_form(self):
        """「全部升级」按钮（下发给所有客户机）。"""
        av = client_release.current()
        if not av:
            return ''
        return ('<form method="post" action="/web/clients/cmd" class="inline-form"'
                ' onsubmit="return confirm(\'给所有客户机下发“升级到 v' + html.escape(av)
                + '”指令？\')">' + self._csrf_hidden()
                + '<input type="hidden" name="action" value="upgrade_all">'
                + '<input type="submit" value="全部升级到 v' + html.escape(av) + '"></form>')

    def _release_card(self):
        """「客户端发布」卡片：当前发布版、历史版本、上传入口。"""
        info = client_release.info()
        cur = info["current"]
        def short_sha(s):
            return (s or "")[:12] + "…" if s else "—"
        rows = []
        for r in info["releases"]:
            tag = '<span class="ok">当前</span>' if r["ver"] == cur else ''
            drop = ''
            if r["ver"] != cur:
                drop = ('<form method="post" action="/web/clients/cmd" class="inline-form"'
                        ' onsubmit="return confirm(\'删除已发布的 v' + html.escape(r["ver"])
                        + '？\')">' + self._csrf_hidden()
                        + '<input type="hidden" name="action" value="drop_release">'
                        + '<input type="hidden" name="ver" value="' + html.escape(r["ver"]) + '">'
                        + '<input type="submit" value="删除"></form>')
            rows.append('<tr><td>' + html.escape(r["ver"]) + ' ' + tag + '</td><td class="small">'
                        + html.escape(str(r.get("source") or "")) + '</td><td class="small">'
                        + str(int(r.get("size") or 0) // 1024) + ' KB</td><td class="small">'
                        + html.escape(short_sha(r.get("sha256"))) + '</td><td class="small">'
                        + html.escape(r.get("built") or "") + '</td><td>' + drop + '</td></tr>')
        table = ('<table><tr><th>版本</th><th>来源</th><th>大小</th><th>sha256</th><th>发布时间</th>'
                 '<th></th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="6">还没有发布过客户端</td></tr>')
                 + '</table>')
        cur_txt = ('当前发布：<b>v' + html.escape(cur) + '</b>' if cur else
                   '<span class="err">还没有发布任何客户端版本</span>')
        upload_form = ('<form method="post" action="/web/clients/upload" '
                       'enctype="multipart/form-data" class="inline-form">'
                       + self._csrf_hidden()
                       + 'exe：<input type="file" name="exe" accept=".exe"> '
                       + '版本：<input type="text" name="ver" size="7" placeholder="如 1.1"> '
                       + '<input type="submit" value="上传并发布"></form>')
        return ('<div class="card"><h2>客户端发布</h2>'
                + '<p class="small">发布库：<code>' + html.escape(info["dir"] or "") + '</code>，'
                + '只保留最近 %d 个版本。</p>' % client_release.KEEP_RELEASES
                + '<p>' + cur_txt + '</p>' + table
                + '<p>上传 exe：' + upload_form + '</p>'
                + '<p class="small">客户端是 .NET Framework + WinForms 的 Windows 程序，'
                + '<b>只在 Windows 上编译</b>：在装有 Windows 的机器上跑 <code>client\\build.bat</code>，'
                + '把 <code>client\\dist\\iscsi-broker-agent.exe</code> 传上来（版本号留空取源码里的 VERSION）。'
                + '客户机端怎么更新见 client/README.md；无盘客户机重启会回到母盘里的版本，'
                + '想让升级“永久生效”要重做母盘。</p>'
                + '</div>')

    def _do_clients_cmd(self, form):
        """处理客户机控制页的下发按钮（关机/重启/VNC/挂载/账号/令牌/更新客户端）。"""
        action = form.get("action", [""])[0]
        if action == "rotate_token":
            agent_hub.rotate_token()
            self._redirect("/web/clients?msg=" + urllib.parse.quote("令牌已重置，请更新各客户机的 agent.ini", safe=""))
            return
        if action in ("upgrade_one", "upgrade_all"):
            self._do_upgrade(action == "upgrade_all",
                             form.get("mac", [""])[0], form.get("force", [""])[0] == "1")
            return
        if action == "release_writeback":
            img = form.get("img", [""])[0].strip()
            owner = release_writeback_lock(img, "管理员在后台手动解除")
            msg = ("已解除 %s 的回写占用（原占用者 %s）" % (img, owner)) if owner \
                else ("%s 当前没有回写占用" % img)
            self._redirect("/?msg=" + urllib.parse.quote(msg, safe=""))
            return
        if action == "drop_release":
            try:
                client_release.remove((form.get("ver", [""])[0] or "").strip())
                ok, msg = True, "已删除该发布版本"
            except client_release.ReleaseError as e:
                ok, msg = False, str(e)
            self._redirect("/web/clients?msg=" + urllib.parse.quote(("" if ok else "失败：") + msg, safe=""))
            return
        mac = form.get("mac", [""])[0]
        if action == "set_account":
            _ok, msg = agent_hub.set_account(mac, form.get("account", [""])[0])
            self._redirect("/web/clients?msg=" + urllib.parse.quote(msg, safe=""))
            return
        args = {}
        if action == "mount":
            args["url"] = self._dav_url()
        # 注：enqueue 会把“是否挂网盘”的意图记在服务器上（默认挂载；无盘客户机重启后靠它对齐）
        _ok, msg = agent_hub.enqueue(mac, action, **args)
        if not _ok and action in ("mount", "unmount"):
            msg = "已记下“%s”，客户机上线后会自动同步（现在：%s）" % (
                "启用网盘" if action == "mount" else "停用网盘", msg)
        self._redirect("/web/clients?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_upgrade(self, all_machines, mac, force):
        """后台「升级客户端」：把发布版作为 update 指令下发给客户机（客户机下次心跳执行）。"""
        ver = client_release.current()
        rec = client_release.release_of(ver) if ver else None
        if not rec:
            self._redirect("/web/clients?msg=" + urllib.parse.quote(
                "服务器上还没有发布客户端：请在下面「客户端发布」里上传 Windows 上 "
                "client\\build.bat 编好的 iscsi-broker-agent.exe", safe=""))
            return
        args = {"ver": rec["ver"], "url": self._agent_exe_rel(rec["ver"]),
                "sha256": rec.get("sha256", ""), "size": rec.get("size", 0)}
        if force:
            args["force"] = 1
        targets = [r["mac"] for r in agent_hub.snapshot()] if all_machines else [mac]
        targets = [m for m in targets if m]
        if not targets:
            self._redirect("/web/clients?msg=" + urllib.parse.quote("没有可下发的客户机", safe=""))
            return
        ok_n, fail = 0, []
        for m in targets:
            ok, msg = agent_hub.enqueue(m, "update", **args)
            if ok:
                ok_n += 1
            else:
                fail.append("%s: %s" % (m, msg))
        if ok_n and not fail:
            msg = "已下发“更新客户端到 v%s”：%d 台（客户机下次心跳时执行）" % (rec["ver"], ok_n)
        elif ok_n:
            msg = "已下发 %d 台更新到 v%s；%d 台失败：%s" % (ok_n, rec["ver"], len(fail), "；".join(fail[:3]))
        else:
            msg = "下发失败：" + "；".join(fail[:3])
        self._redirect("/web/clients?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_client_upload(self, s):
        """后台上传客户端 exe：POST /web/clients/upload（multipart/form-data）。"""
        def back(msg):
            self._redirect("/web/clients?msg=" + urllib.parse.quote(msg, safe=""))

        ctype = self.headers.get("Content-Type", "") or ""
        length = int(self.headers.get("Content-Length", 0) or 0)
        limit = client_release.MAX_UPLOAD + 1024
        if "multipart/form-data" not in ctype.lower():
            back("上传失败：不是表单上传")
            return
        if length <= 0 or length > limit:
            back("上传失败：文件为空或超过 %d MB" % (client_release.MAX_UPLOAD // 1024 // 1024))
            return
        bound = _form_boundary(ctype)
        if not bound:
            back("上传失败：缺少 boundary")
            return
        # 直接读进内存（上限 32MB，够放 200KB 的 exe）
        fields = _parse_multipart(self.rfile.read(length), bound)
        if fields.get("csrf", [""])[0] != s.get("csrf"):
            back("上传失败：会话已过期，请重新登录后再传")
            return
        blob = fields.get("exe", [b""])[0]
        if isinstance(blob, str):
            blob = blob.encode("utf-8", "replace")
        ver = (fields.get("ver", [""])[0] or "").strip()
        try:
            if not ver:
                # 版本号留空时取装的源码里的 VERSION（install.sh 会把 client/Agent.cs 装到程序目录）
                ver = client_release.source_version(os.path.dirname(os.path.abspath(__file__)))
            if not ver:
                back("上传失败：没填版本号，也读不到 client/Agent.cs 里的 VERSION，请手填版本号（如 1.1）")
                return
            rec = client_release.import_bytes(blob, ver, notes="uploaded from web")
            back("已发布客户端 v%s（%d 字节，sha256=%s…）" % (rec["ver"], rec["size"], rec["sha256"][:12]))
        except client_release.ReleaseError as e:
            back("上传失败：" + str(e))
        except Exception as e:
            back("上传失败：" + str(e))

    # ---------- 管理员：默认配额 ----------
    def _settings_page(self, msg=None):
        cur = users_auth.get_default_quota()
        msg_html = ''
        if msg:
            msg_html = '<p class="' + ('ok' if msg.startswith("默认配额已更新") else 'err') + '">' \
                       + html.escape(msg) + '</p>'
        body = ('<div class="card"><h2>默认配额</h2>' + msg_html
                + '<p>当前默认配额：' + _human_size(cur) + '（' + str(cur) + ' 字节）</p>'
                + '<p class="small">默认配额作用于之后新注册的账号。</p>'
                + '<form method="post" action="/web/settings">' + self._csrf_hidden()
                + '<p>新默认配额（如 1G、500M、2T）：<input type="text" name="size"></p>'
                + '<p><input type="submit" value="保存"></p></form></div>')
        self._send_html(_page_html("默认配额", body, NAV_ADMIN))

    def _do_settings(self, form):
        size = form.get("size", [""])[0].strip()
        try:
            bytes_n = parse_size_to_bytes(size)
        except (ValueError, OverflowError):
            bytes_n = None
        if bytes_n is None or bytes_n <= 0:
            msg = "配额格式不正确：" + (size or "（空）")
        else:
            _ok, msg = users_auth.set_default_quota(bytes_n)
        self._redirect("/web/settings?msg=" + urllib.parse.quote(msg, safe=""))

    # ---------- 管理员：联网控制 ----------
    def _netctrl_page(self, msg=None):
        if not netctrl.ready():
            body = ('<div class="card"><p class="err">联网控制未启用。</p>'
                    '<p class="small">原因可能是 NETCTRL_ENABLED=False，或启动时网卡自动探测失败'
                    '（请查看服务器日志，或在配置里显式指定 NETCTRL_LAN_IF / NETCTRL_WAN_IF）。</p></div>')
            self._send_html(_page_html("联网控制", body, NAV_ADMIN))
            return
        cur = netctrl.get_default()
        msg_html = ''
        if msg:
            ok = not (msg.startswith("MAC") or msg.startswith("参数"))
            msg_html = '<p class="' + ('ok' if ok else 'err') + '">' + html.escape(msg) + '</p>'
        def_radio = ''.join(
            '<label><input type="radio" name="default" value="' + v + '"'
            + (' checked' if cur == v else '') + '>' + label + '</label>&nbsp;&nbsp;'
            for v, label in (("allow", "默认允许联网"), ("deny", "默认禁止联网")))
        body = ('<div class="card"><h2>默认行为（未手动设置的客户机）</h2>' + msg_html
                + '<form method="post" action="/web/netctrl/default">' + self._csrf_hidden()
                + '<p>' + def_radio + '</p>'
                + '<p><input type="submit" value="保存默认行为"></p></form></div>')
        conns = get_client_ips()   # mac -> ip（当前有 iSCSI 连接的客户机）
        rows = []
        for r in netctrl.known_clients(conns):
            mac = r["mac"]
            st = '<span class="ok">在线</span>' if r["online"] else '离线'
            pol = (('允许' if r["policy"] == "allow" else '禁止') + '（' + r["src"] + '）')
            acts = ('<form method="post" action="/web/netctrl/mac" class="inline-form">'
                    + self._csrf_hidden()
                    + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                    + '<input type="hidden" name="action" value="allow">'
                    + '<input type="submit" value="允许"></form>'
                    + '<form method="post" action="/web/netctrl/mac" class="inline-form">'
                    + self._csrf_hidden()
                    + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                    + '<input type="hidden" name="action" value="deny">'
                    + '<input type="submit" value="禁止"></form>')
            if r["src"] == "手动":
                acts += ('<form method="post" action="/web/netctrl/mac" class="inline-form">'
                         + self._csrf_hidden()
                         + '<input type="hidden" name="mac" value="' + html.escape(mac) + '">'
                         + '<input type="hidden" name="action" value="remove">'
                         + '<input type="submit" value="恢复默认"></form>')
            rows.append('<tr><td>' + html.escape(mac) + '</td><td>' + html.escape(r["ip"])
                        + '</td><td>' + st + '</td><td>' + pol + '</td><td>' + acts + '</td></tr>')
        table = ('<table><tr><th>MAC</th><th>IP</th><th>状态</th><th>生效策略</th><th>操作</th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="5">暂无客户机记录</td></tr>')
                 + '</table>')
        body += ('<div class="card"><h2>客户机联网控制</h2>' + table
                 + '<p class="small">列表 = 手动设置过的机器 + 当前在线机器。'
                 + '“允许/禁止”立即生效；“恢复默认”后按上面的默认行为执行。'
                 + '客户机开机时规则自动建立，关机自动清理。</p></div>')
        body += ('<div class="card"><h2>手动添加客户机</h2>'
                 + '<form method="post" action="/web/netctrl/mac">' + self._csrf_hidden()
                 + '<p>MAC（支持 aa:bb:cc:dd:ee:ff 或 aabbccddeeff）：'
                 + '<input type="text" name="mac" placeholder="aa:bb:cc:dd:ee:ff" maxlength="17"></p>'
                 + '<p><select name="action"><option value="allow">允许联网</option>'
                 + '<option value="deny">禁止联网</option></select>'
                 + '&nbsp;<input type="submit" value="添加/设置"></p></form>'
                 + '<p class="small">手动添加用于：从未上线的机器（如未装机的新客户机）预先设好策略。</p></div>')
        sts = netctrl.status_texts()
        body += ('<div class="card"><h2>当前转发规则（只读）</h2>'
                 + '<p class="small">本模块托管 FORWARD 与 POSTROUTING；接管/改动前自动备份到 netctrl_backup/。</p>'
                 + '<h2>FORWARD</h2><pre>' + html.escape(sts["forward"]) + '</pre>'
                 + '<h2>NETCTRL（联网控制）</h2><pre>' + html.escape(sts["netctrl"]) + '</pre>'
                 + '<h2>NAT POSTROUTING</h2><pre>' + html.escape(sts["nat"]) + '</pre></div>')
        self._send_html(_page_html("联网控制", body, NAV_ADMIN))

    def _do_netctrl_default(self, form):
        mode = form.get("default", [""])[0]
        msg = netctrl.set_default(mode)
        self._redirect("/web/netctrl?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_netctrl_mac(self, form):
        mac = form.get("mac", [""])[0].strip()
        action = form.get("action", [""])[0]
        if action == "remove":
            msg = netctrl.remove_mac(mac)
        else:
            msg = netctrl.set_mac(mac, action)
        self._redirect("/web/netctrl?msg=" + urllib.parse.quote(msg, safe=""))

    # ---------- 管理员：通用文件 ----------
    def _common_page(self, msg=None):
        files = cloud_store.common_list()
        rows = []
        for f in files:
            rows.append(
                '<tr><td>' + html.escape(f["name"]) + '</td><td>' + _human_size(f["size"])
                + '</td><td><a href="/web/common/download?f='
                + urllib.parse.quote(f["name"], safe="") + '">下载</a>'
                + '<form method="post" action="/web/common/delete" class="inline-form">'
                + self._csrf_hidden()
                + '<input type="hidden" name="name" value="' + html.escape(f["name"]) + '">'
                + '<input type="submit" value="删除"></form></td></tr>')
        table = ('<table><tr><th>名称</th><th>大小</th><th>操作</th></tr>'
                 + ("".join(rows) if rows else '<tr><td colspan="3">暂无通用文件</td></tr>')
                 + '</table>')
        msg_html = ''
        if msg:
            cls = 'ok' if (msg.startswith("删除成功") or msg.startswith("上传成功")) else 'err'
            msg_html = '<p class="' + cls + '">' + html.escape(msg) + '</p>'
        s = self._session()
        csrf = s["csrf"] if s else ""
        body = ('<div class="card"><h2>通用文件</h2>' + msg_html + table
                + '<p class="small">通用文件出现在所有账号网盘的“通用文件”目录，仅管理员可写。</p></div>'
                + '<div class="card"><h2>上传通用文件</h2>'
                + '<form method="post" action="/web/common/upload?csrf=' + csrf
                + '" enctype="multipart/form-data">'
                + '<p><input type="file" name="file"></p>'
                + '<p><input type="submit" value="上传"></p></form></div>')
        self._send_html(_page_html("通用文件", body, NAV_ADMIN))

    def _do_common_upload(self, s):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("csrf", [""])[0] != s["csrf"]:
            self._redirect("/web/common")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        try:
            _ok, msg = cloud_store.save_common_upload(self.rfile, content_length,
                                                      self.headers.get("Content-Type", ""),
                                                      8 << 40)
        except Exception as e:
            msg = "上传失败：%s" % e
        self._redirect("/web/common?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_common_delete(self, form):
        name = form.get("name", [""])[0]
        _ok, msg = cloud_store.delete_common(name)
        self._redirect("/web/common?msg=" + urllib.parse.quote(msg, safe=""))

    def _do_common_download(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        f = params.get("f", [""])[0]
        path = cloud_store.resolve("admin", cloud_store.VIRTUAL_COMMON + "/" + f)
        self._send_file(path, _page_html("通用文件",
                         '<div class="card"><p class="err">文件不存在</p>'
                         '<p><a href="/web/common">返回通用文件</a></p></div>', NAV_ADMIN))

if __name__ == "__main__":
    # 先确定工作模式（此时模块已完整加载，list_images 等函数都可用）
    global USE_REFLINK
    if FORCE_MODE == "reflink":
        USE_REFLINK = True
    elif FORCE_MODE == "qcow2":
        USE_REFLINK = False
    else:
        USE_REFLINK = detect_reflink_support()

    load_admin_password()  # 从 admin.conf 读取（不存在则写入默认）管理员密码

    # 多账号认证 + 个人网盘（云盘）初始化
    users_auth.setup(OVERLAY_DIR, lambda pwd: verify_password(pwd, ADMIN_PW_STORED))
    cloud_store.setup(os.path.join(BASE_DIR, "cloud"))
    print(f"[{datetime.datetime.now()}] [START] Cloud drive: {os.path.join(BASE_DIR, 'cloud')}")

    # 客户机 agent 控制通道（接入令牌 + 心跳状态 + 指令队列）：令牌落盘，重启不变
    agent_hub.setup(OVERLAY_DIR)
    print(f"[{datetime.datetime.now()}] [START] Client agent channel ready (token file: "
          f"{os.path.join(OVERLAY_DIR, agent_hub.TOKEN_FILE_NAME)})")

    # 客户机客户端发布库（托盘「检查更新」从这里拉新版 exe）。
    # 注意：客户端只在 Windows 上用 client\build.bat 编译，服务器不编译，只负责存放与分发。
    print(f"[{datetime.datetime.now()}] [START] Client release store: "
          f"{client_release.setup(BASE_DIR)}")

    # 命令行维护入口（不启动服务）：
    #   python3 iscsi_broker.py --publish-client <exe路径> --client-ver 1.1
    if "--publish-client" in sys.argv:
        def _arg_val(name):
            if name in sys.argv:
                i = sys.argv.index(name)
                if i + 1 < len(sys.argv):
                    return sys.argv[i + 1]
            return ""

        ver = (_arg_val("--client-ver") or "").strip()
        src = _arg_val("--publish-client")
        if not src or not ver:
            print("用法：iscsi_broker.py --publish-client <Windows 上 build.bat 编出的 exe> "
                  "--client-ver <版本号，如 1.1>")
            raise SystemExit(2)
        try:
            rec = client_release.import_file(src, ver, notes="published from cli")
            ok, msg = True, "已发布 v%s（%d 字节）" % (rec["ver"], rec["size"])
        except (client_release.ReleaseError, OSError) as e:
            ok, msg = False, str(e)
        print(("[OK] " if ok else "[ERROR] ") + msg)
        raise SystemExit(0 if ok else 1)

    # 联网控制：接管转发/NAT（按客户机 MAC 控制上网），并启动规则巡检线程
    if NETCTRL_ENABLED:
        netctrl.setup(BASE_DIR, NETCTRL_LAN_IF, NETCTRL_WAN_IF,
                      full_takeover=NETCTRL_FULL_TAKEOVER, reject=NETCTRL_REJECT)
        if netctrl.init():
            threading.Thread(target=netctrl_reconcile_worker, daemon=True).start()

    if os.geteuid() != 0:
        print("[WARN] 当前不是 root 运行！qemu-nbd 打开 /dev/nbdX 需要 root 权限。"
              "若报错出现 'Operation not permitted'，请改用 sudo 或 systemd 服务运行。")
    # nbd 模块只在“路线 B（qcow2 叠加盘）”需要；装不上时只告警不退出，
    # 否则没有 nbd 模块的机器（走 reflink 路线 A）会一直被 systemd 重启。
    try:
        subprocess.run(["modprobe", "nbd", "max_part=8", f"nbds_max={NBD_MAX}"],
                       check=True, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[WARN] 加载 nbd 内核模块失败：{e}")
        if not USE_REFLINK:
            print("[WARN] 当前是 qcow2 回退模式，没有 nbd 时客户机无法启动！"
                  "请安装 linux-modules-extra / 内核 nbd 模块后重启本服务。")
    init_cleanup()
    tune_sysctl()
    threading.Thread(target=idle_cleanup_worker, daemon=True).start()
    web_srv = None
    if WEB_ENABLED:
        web_srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebAdminHandler)
        _maybe_wrap_tls(web_srv)
        threading.Thread(target=web_srv.serve_forever, daemon=True).start()
    mode_name = "reflink (直出 raw，无 qemu)" if USE_REFLINK else "qcow2 (回退叠加盘)"
    print(f"[{datetime.datetime.now()}] [START] iSCSI Broker running on port {PORT}... mode={mode_name} (fs={get_fs_type()})")
    if web_srv:
        log_scheme = "https" if HTTPS_ENABLED else "http"
        print(f"[{datetime.datetime.now()}] [START] Web admin: {log_scheme}://0.0.0.0:{WEB_PORT}/  (password file: {ADMIN_PASS_FILE})")
    main_srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _maybe_wrap_tls(main_srv)
    main_srv.serve_forever()

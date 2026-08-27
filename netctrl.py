#!/usr/bin/env python3
"""netctrl.py —— 联网控制模块：按客户机 MAC 控制其访问外网。

拓扑：服务器双网卡 —— 外网卡(wan) 接外网，内网卡(lan) 接交换机接客户机；
客户机以服务器为网关，出外网流量必然经过服务器的 FORWARD 转发。

本模块在 FORWARD 链最前面挂一条专用链 NETCTRL，按 MAC 判定“内网→外网”流量：
  - 手动设置过的客户机按设置 允许/拒绝；
  - 未设置的客户机按默认行为（default=allow|deny）兜底。
同时托管 NAT（MASQUERADE）与回程放行（RELATED,ESTABLISHED）。

规则生命周期（自动维护，无需手工干预）：
  - 客户机开机（iPXE /prov 请求、ARP 邻居表出现、iSCSI 连接）→ 立即确保/覆写其规则；
  - 关机 → 规则空转无害，巡检自动清理“离线且无手动设置”的机器规则；
  - Web 后台改策略 → 立即覆写并持久化到 <BASE_DIR>/netctrl.conf；
  - 每次接管/重建前把 iptables-save 快照备份到 <BASE_DIR>/netctrl_backup/。

纯标准库；防火墙命令全部经 subprocess，失败记日志不阻断主流程。
"""
import os, re, glob, subprocess, threading, time, datetime

_lock = threading.Lock()
_cfg = {"default": "allow", "macs": {}}   # mac(12位小写hex) -> "allow"/"deny"，仅记录手动设置
_recent_boots = {}                          # mac -> 最近开机时间戳（缓存，防 ARP 邻居表未刷新）
_ifs = None                                 # (lan, wan) 解析后的网卡对
_cfg_lan = ""
_cfg_wan = ""
_full_takeover = True
_reject = True
_ready = False
_base_dir = None
_backup_dir = None
_last_fp = None                             # 最近应用过的规则指纹；未变化则跳过重建
_BOOT_TTL = 600                             # 开机缓存保留时长（秒）
_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sh(args, timeout=5):
    """执行命令；失败抛 RuntimeError（带真实报错信息）。"""
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        snippet = " | ".join(detail[-3:]) if detail else ""
        raise RuntimeError(f"{os.path.basename(args[0])} failed (rc={e.returncode}): {snippet[:300]}")
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} not found")


def _out(args, timeout=5):
    """执行命令并返回 stdout；失败抛 RuntimeError。"""
    try:
        r = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        snippet = " | ".join(detail[-3:]) if detail else ""
        raise RuntimeError(f"{os.path.basename(args[0])} failed (rc={e.returncode}): {snippet[:300]}")
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} not found")


def normalize_mac(mac):
    """把 aa:bb:cc:dd:ee:ff / AA-BB-CC-DD-EE-FF / aabbccddeeff 统一成小写 12 位 hex；非法返回 None。"""
    if not mac:
        return None
    m = re.sub(r"[:\-]", "", mac.strip().lower())
    return m if _MAC_RE.match(m) else None


# ---------- 初始化 / 配置读写 ----------

def setup(base_dir, lan_if="", wan_if="", full_takeover=True, reject=True):
    """配置模块。lan_if/wan_if 留空时由 init() 自动探测。"""
    global _base_dir, _backup_dir, _full_takeover, _reject, _cfg_lan, _cfg_wan
    _base_dir = base_dir
    _backup_dir = os.path.join(base_dir, "netctrl_backup")
    _full_takeover = full_takeover
    _reject = reject
    _cfg_lan = (lan_if or "").strip()
    _cfg_wan = (wan_if or "").strip()


def _parse_conf(text):
    """解析 netctrl.conf 文本 -> {"default": ..., "macs": {...}}（非法行忽略）。"""
    cfg = {"default": "allow", "macs": {}}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("default="):
            v = line.split("=", 1)[1].strip()
            if v in ("allow", "deny"):
                cfg["default"] = v
            continue
        mac, _, pol = line.partition("=")
        mac = normalize_mac(mac)
        pol = pol.strip().lower()
        if mac and pol in ("allow", "deny"):
            cfg["macs"][mac] = pol
    return cfg


def _serialize_cfg(cfg):
    lines = [f"default={cfg['default']}"]
    lines += [f"{mac}={cfg['macs'][mac]}" for mac in sorted(cfg["macs"])]
    return "\n".join(lines) + "\n"


def _load():
    global _cfg
    path = os.path.join(_base_dir, "netctrl.conf")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        _cfg = {"default": "allow", "macs": {}}
        return
    except Exception as e:
        print(f"[{_now()}] [WARN] netctrl.conf 读取失败: {e}")
        _cfg = {"default": "allow", "macs": {}}
        return
    _cfg = _parse_conf(text)


def _save():
    path = os.path.join(_base_dir, "netctrl.conf")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_serialize_cfg(_cfg))
        os.chmod(path, 0o600)
    except Exception as e:
        print(f"[{_now()}] [WARN] netctrl.conf 保存失败: {e}")


def _detect_ifs():
    """探测网卡：带默认路由的 = 外网卡；另一张有 IPv4 且 UP 的 = 内网卡。
    显式配置优先；探测失败返回 None。"""
    wan, lan = _cfg_wan, _cfg_lan
    try:
        if not wan:
            rt = _out(["ip", "route", "show", "default"])
            m = re.search(r"\bdev\s+(\S+)", rt)
            if m:
                wan = m.group(1)
        if not lan:
            out = _out(["ip", "-o", "-4", "addr", "show", "up"])
            cands = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 4 or parts[2] != "inet":
                    continue
                ifname = parts[1]
                if ifname == "lo" or "@" in ifname or ifname == wan:
                    continue
                cands.append(ifname)
            if cands:
                lan = cands[0]
                if len(cands) > 1:
                    print(f"[{_now()}] [WARN] netctrl: 检测到多张内网卡 {cands}，取 {lan}（可在配置里显式指定 NETCTRL_LAN_IF）")
    except Exception as e:
        print(f"[{_now()}] [WARN] netctrl 网卡探测异常: {e}")
        return None
    if not wan or not lan:
        return None
    return (lan, wan)


def _backup():
    """接管/重建前把当前 iptables 全量快照存到 netctrl_backup/（只保留最近 20 份）。"""
    try:
        os.makedirs(_backup_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_backup_dir, f"iptables-{ts}.rules")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_out(["iptables-save"]))
        for old in sorted(glob.glob(os.path.join(_backup_dir, "iptables-*.rules")))[:-20]:
            try:
                os.remove(old)
            except Exception:
                pass
        print(f"[{_now()}] [INFO] netctrl: iptables 快照 -> {path}")
    except Exception as e:
        print(f"[{_now()}] [WARN] netctrl 备份失败: {e}")


# ---------- 防火墙规则 ----------

def _effective_locked(mac):
    return _cfg["macs"].get(mac, _cfg["default"])


def _verdict_target(verdict):
    if verdict == "allow":
        return "ACCEPT"
    return "REJECT" if _reject else "DROP"


def _neigh_macs():
    """内网卡 ARP 邻居表中带 MAC 的机器（含 REACHABLE/STALE/DELAY/PROBE；FAILED 无 lladdr 自动跳过）。"""
    macs = set()
    try:
        out = _out(["ip", "neigh", "show", "dev", _ifs[0]])
        for line in out.splitlines():
            m = re.search(r"lladdr\s+([0-9a-f:]+)", line)
            if m:
                mac = normalize_mac(m.group(1))
                if mac:
                    macs.add(mac)
    except Exception:
        pass
    return macs


def _desired_rules():
    """返回 ([ (mac, verdict), ... ], fallback)。规则集 = 手动设置 ∪ 开机缓存 ∪ 邻居表在线机器。"""
    now = time.time()
    macs = set(_cfg["macs"])
    for m, ts in list(_recent_boots.items()):
        if now - ts > _BOOT_TTL:
            _recent_boots.pop(m, None)
        else:
            macs.add(m)
    macs |= _neigh_macs()
    rules = sorted((m, _effective_locked(m)) for m in macs)
    return rules, _cfg["default"]


def _sync_locked():
    """整体重建 NETCTRL 链（幂等）：指纹未变化则跳过。调用方必须持 _lock。"""
    global _last_fp
    rules, fallback = _desired_rules()
    fp = (fallback, tuple(rules))
    if fp == _last_fp:
        return
    lan, wan = _ifs
    try:
        _sh(["iptables", "-F", "NETCTRL"])
        for mac, verdict in rules:
            _sh(["iptables", "-A", "NETCTRL", "-m", "mac", "--mac-source", mac,
                 "-j", _verdict_target(verdict)])
        _sh(["iptables", "-A", "NETCTRL", "-j", _verdict_target(fallback)])
        # 钩子保证在 FORWARD 第 1 条（先删后插，幂等）
        try:
            _sh(["iptables", "-D", "FORWARD", "-i", lan, "-o", wan, "-j", "NETCTRL"])
        except RuntimeError:
            pass
        _sh(["iptables", "-I", "FORWARD", "1", "-i", lan, "-o", wan, "-j", "NETCTRL"])
        _last_fp = fp
        print(f"[{_now()}] [INFO] netctrl: 规则已应用（{len(rules)} 台客户机，默认={fallback}）")
    except Exception as e:
        print(f"[{_now()}] [ERROR] netctrl 规则同步失败: {e}")


def init():
    """启动接管：探测网卡 → 读配置 → 备份 → 清空并重建 FORWARD/POSTROUTING → 应用 NETCTRL。
    成功返回 True；失败（无 root、网卡探测失败等）返回 False，客户机上网不受影响。"""
    global _ready, _ifs, _last_fp
    with _lock:
        try:
            ifs = _detect_ifs()
            if not ifs:
                print(f"[{_now()}] [ERROR] netctrl: 网卡探测失败（配置 lan={_cfg_lan!r} wan={_cfg_wan!r}），联网控制未启用")
                return False
            _ifs = ifs
            _load()
            _backup()
            _sh(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            try:
                # 客户机永不分配 IPv6：关闭 IPv6 转发，防止走 IPv6 绕过联网控制
                _sh(["sysctl", "-w", "net.ipv6.conf.all.forwarding=0"])
            except RuntimeError:
                pass
            try:
                _sh(["iptables", "-N", "NETCTRL"])
            except RuntimeError:
                pass  # 链已存在
            if _full_takeover:
                # 完全接管：清空旧的手工转发/NAT 规则（按用户要求“现有的完全删除”）
                _sh(["iptables", "-F", "FORWARD"])
                _sh(["iptables", "-t", "nat", "-F", "POSTROUTING"])
                # NAT 回程放行 + 出外网伪装（MASQUERADE）
                _sh(["iptables", "-A", "FORWARD", "-i", wan, "-o", lan,
                     "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
                _sh(["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", wan, "-j", "MASQUERADE"])
            _last_fp = None
            _sync_locked()
            _ready = True
            print(f"[{_now()}] [INFO] netctrl: 联网控制已启用（lan={_ifs[0]}, wan={_ifs[1]}, 默认={'允许' if _cfg['default'] == 'allow' else '禁止'}）")
            return True
        except Exception as e:
            print(f"[{_now()}] [ERROR] netctrl 初始化失败: {e}")
            _ready = False
            return False


# ---------- 对外接口 ----------

def ready():
    return _ready


def effective(mac):
    """某客户机的生效策略：手动设置 > 默认行为。"""
    mac = normalize_mac(mac)
    if not mac:
        return _cfg["default"]
    with _lock:
        return _effective_locked(mac)


def get_default():
    with _lock:
        return _cfg["default"]


def set_default(mode):
    if mode not in ("allow", "deny"):
        return "参数错误"
    with _lock:
        _cfg["default"] = mode
        _save()
        if _ready:
            _sync_locked()
    return "默认行为已更新为" + ("允许联网" if mode == "allow" else "禁止联网")


def set_mac(mac, mode):
    mac = normalize_mac(mac)
    if not mac:
        return "MAC 格式不正确"
    if mode not in ("allow", "deny"):
        return "参数错误"
    with _lock:
        _cfg["macs"][mac] = mode
        _save()
        if _ready:
            _sync_locked()
    return f"{mac} 已设为" + ("允许联网" if mode == "allow" else "禁止联网")


def remove_mac(mac):
    mac = normalize_mac(mac)
    if not mac:
        return "MAC 格式不正确"
    with _lock:
        had = _cfg["macs"].pop(mac, None)
        if had:
            _save()
            if _ready:
                _sync_locked()
    return (f"{mac} 已恢复默认" if had else f"{mac} 原本就没有手动设置")


def on_client_boot(mac):
    """客户机开机事件（PXE/iPXE 请求供给时调用）：立即确保/覆写其转发规则。"""
    mac = normalize_mac(mac)
    if not mac or not _ready:
        return
    with _lock:
        _recent_boots[mac] = time.time()
        _sync_locked()


def reconcile_once(extra_macs=()):
    """周期巡检：合并 iSCSI 在线连接（extra_macs）与 ARP 邻居表，对齐规则；
    自动清理“离线且无手动设置”的机器规则。"""
    if not _ready:
        return
    with _lock:
        for m in extra_macs:
            mac = normalize_mac(m)
            if mac:
                _recent_boots[mac] = time.time()
        _sync_locked()


def known_clients(conns=None):
    """Web 展示用：{mac, ip, online, policy, src}。在线来源 = ARP 邻居表 + iSCSI 连接。"""
    if not _ready:
        return []
    conns = conns or {}
    neigh = {}
    try:
        out = _out(["ip", "neigh", "show", "dev", _ifs[0]])
        for line in out.splitlines():
            m = re.search(r"lladdr\s+([0-9a-f:]+)", line)
            if m:
                mac = normalize_mac(m.group(1))
                if mac:
                    ipm = re.search(r"^(\S+)", line)
                    neigh[mac] = ipm.group(1) if ipm else ""
    except Exception:
        pass
    with _lock:
        macs = set(_cfg["macs"]) | set(neigh) | set(_recent_boots) | set(conns)
        src_map = {m: ("手动" if m in _cfg["macs"] else "默认") for m in macs}
    rows = []
    for mac in sorted(macs):
        rows.append({
            "mac": mac,
            "ip": neigh.get(mac) or conns.get(mac, ""),
            "online": mac in neigh or mac in conns,
            "policy": effective(mac),
            "src": src_map[mac],
        })
    return rows


def status_texts():
    """当前规则只读文本（后台页面展示用）。"""
    if not _ready:
        return {"forward": "（联网控制未启用）", "netctrl": "（联网控制未启用）", "nat": "（联网控制未启用）"}
    res = {}
    for key, args in (
        ("forward", ["iptables", "-S", "FORWARD"]),
        ("netctrl", ["iptables", "-S", "NETCTRL"]),
        ("nat", ["iptables", "-t", "nat", "-S", "POSTROUTING"]),
    ):
        try:
            res[key] = _out(args)
        except Exception as e:
            res[key] = f"<查询失败> {e}"
    return res

# -*- coding: utf-8 -*-
"""
agent_hub.py —— 客户机 agent 控制通道（供 iscsi_broker.py 使用）。

客户机（无盘 Windows）里跑一个常驻 agent，它不开放任何入站端口，而是每隔几秒
主动向服务器 POST /agent/poll 心跳；管理员的指令（关机 / 重启 / 启停 VNC / 挂载
网盘）排进该 MAC 的队列，随下一次心跳下发，agent 执行完再 POST /agent/result 回报。
这样客户机在 NAT / 防火墙后面也能被控制，服务器不需要主动连客户机。

设计要点：
1. 纯 Python 标准库；不 import iscsi_broker（避免循环依赖），只被主程序 import。
2. 状态全部内存态（服务重启即清空，客户机下次心跳会重新出现）；只有两样东西落盘：
       <base_dir>/agent_token.conf   接入令牌（一行 token=<value>）
       <base_dir>/agents.conf        每台机器的网盘账号映射（一行 <mac>$<账号>）
3. 令牌用 secrets.compare_digest 常量时间比较；客户机用同一个令牌做 agent 通道
   与 WebDAV 网盘认证（WebDAV 用户名 = 自己的 MAC），所以客户机上不需要存网盘密码。
4. 指令带 TTL：机器关机很久后重新上线，不会执行一条早已过期的“关机”指令。
5. 所有公开函数线程安全（模块级 RLock）。
"""

import os
import secrets
import threading
import time

TOKEN_FILE_NAME = "agent_token.conf"
CLIENTS_FILE_NAME = "agents.conf"

ONLINE_WINDOW = 15      # 距最后一次心跳超过该秒数即视为离线
MAX_QUEUE = 20          # 每台机器最多排队的指令数（防止刷爆内存）
MAX_RESULTS = 20        # 每台机器保留的最近执行结果条数
COMMAND_TTL = 300       # 指令超过该秒数仍未被取走则丢弃（避免上线瞬间执行陈旧指令）
MAX_ACCOUNT_LEN = 32    # 网盘账号名长度上限（与 users_auth 用户名规则一致）
DAV_SESSION_TTL = 12 * 3600   # 托盘登录换来的网盘会话令牌有效期（秒）

_lock = threading.RLock()
_base_dir = None
_token = ""
_clients = {}    # mac -> 客户机记录（见 _ensure 的字段说明）
_dav_sessions = {}   # 网盘会话令牌 -> {"user","exp"}（托盘登录成功后签发，客户机上不存密码）
_seq = 0         # 指令自增 id


# ---------- 内部工具 ----------
def _norm_mac(mac):
    """MAC 归一化：小写、去掉分隔符并只保留十六进制字符；非法返回 ""。"""
    s = "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")
    return s if len(s) == 12 else ""


def _atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _load_token():
    """读取/生成接入令牌。文件不存在或为空则生成一个并落盘。"""
    path = os.path.join(_base_dir, TOKEN_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if line.startswith("token=") and len(line) > 6:
                    return line[6:].strip()
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        _atomic_write(path, "token=%s\n" % tok)
    except OSError:
        pass
    return tok


def _load_clients():
    """读取 MAC -> (网盘账号, 是否要求挂载网盘) 映射。

    agents.conf 每行：<mac>$<账号>$<want_mount 0|1>（兼容只有两段的旧格式）。
    """
    path = os.path.join(_base_dir, CLIENTS_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or "$" not in line:
            continue
        parts = line.split("$")
        mac = _norm_mac(parts[0])
        if not mac:
            continue
        rec = _ensure(mac)
        if len(parts) >= 2:
            rec["account"] = parts[1][:MAX_ACCOUNT_LEN]
        if len(parts) >= 3:
            rec["want_mount"] = (parts[2].strip() == "1")


def _save_clients_locked():
    """整体重写 agents.conf（调用方必须已持有 _lock）。"""
    rows = []
    for mac in sorted(_clients):
        rec = _clients[mac]
        acc = rec.get("account") or ""
        want = "1" if rec.get("want_mount") else "0"
        if acc or rec.get("want_mount"):
            rows.append("%s$%s$%s\n" % (mac, acc, want))
    try:
        _atomic_write(os.path.join(_base_dir, CLIENTS_FILE_NAME), "".join(rows))
    except OSError:
        pass


def _ensure(mac):
    """取出（必要时创建）某台机器的记录。"""
    rec = _clients.get(mac)
    if rec is None:
        now = time.time()
        rec = {"mac": mac, "account": "", "want_mount": True, "ip": "", "hostname": "",
               "agent_ver": "", "first_seen": now, "last_seen": 0.0, "vnc_running": False,
               "vnc_port": 0, "drive": "", "dav_user": "", "dav_since": 0.0,
               "queue": [], "results": [], "last_cmd": None}
        _clients[mac] = rec
    return rec


# ---------- 初始化 ----------
def setup(base_dir: str) -> None:
    """初始化模块（主程序启动时调用一次）：载入令牌与网盘账号映射。"""
    global _base_dir, _token
    with _lock:
        _base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        _token = _load_token()
        _load_clients()


# ---------- 接入令牌 ----------
def token() -> str:
    """当前接入令牌（后台页面展示/复制用）。"""
    return _token


def rotate_token() -> str:
    """重新生成令牌并落盘；旧令牌立即失效（所有客户机需要更新 agent.ini）。"""
    global _token
    with _lock:
        _token = secrets.token_urlsafe(24)
        try:
            _atomic_write(os.path.join(_base_dir, TOKEN_FILE_NAME), "token=%s\n" % _token)
        except OSError:
            pass
        return _token


def check_token(tok) -> bool:
    """常量时间比较接入令牌。"""
    if not tok or not _token:
        return False
    return secrets.compare_digest(str(tok), _token)


# ---------- 心跳与状态 ----------
def touch(mac, ip="", info=None):
    """记录一次心跳/状态上报，返回归一化后的 mac（非法返回 ""）。

    只更新 info 里**出现过的**字段：SYSTEM 控制进程与用户会话里的托盘进程会分别上报
    （一个知道 VNC，一个知道 Z 盘），避免互相把对方的字段抹成空值。
    """
    mac = _norm_mac(mac)
    if not mac:
        return ""
    info = info or {}
    with _lock:
        rec = _ensure(mac)
        rec["last_seen"] = time.time()
        if ip:
            rec["ip"] = str(ip)[:64]
        if info.get("hostname"):
            rec["hostname"] = str(info["hostname"])[:64]
        if info.get("agent_ver"):
            rec["agent_ver"] = str(info["agent_ver"])[:32]
        if "vnc_running" in info:
            rec["vnc_running"] = bool(info.get("vnc_running"))
        if "vnc_port" in info:
            try:
                rec["vnc_port"] = int(info.get("vnc_port") or 0)
            except (TypeError, ValueError):
                rec["vnc_port"] = 0
        if "drive" in info:
            rec["drive"] = str(info.get("drive") or "")[:8]
        if "dav_user" in info:
            rec["dav_user"] = str(info.get("dav_user") or "")[:MAX_ACCOUNT_LEN]
    return mac


def is_online(rec) -> bool:
    """记录是否在线（最近 ONLINE_WINDOW 秒内有心跳）。"""
    return bool(rec) and (time.time() - (rec.get("last_seen") or 0) <= ONLINE_WINDOW)


def online_macs():
    """当前在线客户机的 mac 列表。"""
    with _lock:
        return [m for m, r in _clients.items() if is_online(r)]


def ip_of(mac):
    """某台机器的 IP（最近一次心跳的来源地址）；未知返回 ""。"""
    mac = _norm_mac(mac)
    with _lock:
        rec = _clients.get(mac)
        return rec["ip"] if rec else ""


def record_of(mac):
    """拿到记录的浅拷贝（页面渲染用，避免持锁遍历）。"""
    mac = _norm_mac(mac)
    with _lock:
        rec = _clients.get(mac)
        if not rec:
            return None
        out = dict(rec)
        out["queue"] = list(rec["queue"])
        out["results"] = list(rec["results"])
        out["online"] = is_online(rec)
        return out


def snapshot():
    """全部已知机器的快照（按 MAC 排序），供后台页面渲染。"""
    with _lock:
        return [record_of(m) for m in sorted(_clients)]


# ---------- 指令队列 ----------
def _drop_expired_locked(rec):
    """丢掉超时未被取走的指令（机器关机很久后重新上线的情形）。"""
    cut = time.time() - COMMAND_TTL
    rec["queue"] = [c for c in rec["queue"] if c["created"] >= cut]


def enqueue(mac, ctype, **args):
    """给某台机器排一条指令。返回 (True, 说明) 或 (False, 原因)。

    ctype 取值：shutdown / reboot / vnc_start / vnc_stop / mount / unmount / update
    （update 的参数：ver / url / sha256 / size / force，客户机下载新 exe 自我替换）
    """
    global _seq
    mac = _norm_mac(mac)
    if not mac:
        return False, "MAC 不合法"
    if ctype not in ("shutdown", "reboot", "vnc_start", "vnc_stop", "mount", "unmount",
                     "update"):
        return False, "未知指令：%s" % ctype
    with _lock:
        rec = _ensure(mac)
        # 挂载/卸载指令同时改“服务器侧挂载意图”，这样机器离线时也记得住，
        # 客户机下次开机心跳会自动对齐（无盘客户机本地状态每次重启都会丢）
        if ctype in ("mount", "unmount"):
            rec["want_mount"] = (ctype == "mount")
            _save_clients_locked()
        if not is_online(rec):
            return False, "客户机不在线（没有收到 agent 心跳）"
        _drop_expired_locked(rec)
        if len(rec["queue"]) >= MAX_QUEUE:
            return False, "指令队列已满，请稍后再试"
        _seq += 1
        rec["queue"].append({"id": _seq, "type": ctype, "args": args,
                             "created": time.time()})
    return True, "指令已下发，等待客户机执行（每 %d 秒一次心跳）" % ONLINE_WINDOW


def poll(mac, ip="", info=None):
    """客户机心跳：更新状态并取走一条待执行指令。

    返回 (归一化后的 mac, 指令 dict 或 None)；mac 非法时返回 ("", None)。
    指令为 {"id":N, "type":"...", ...参数平铺...}（update 指令的 ver/url/sha256/size/force
    直接放在顶层，客户机不需要解 args；同时保留 "args" 一份，方便以后扩展）。
    """
    mac = touch(mac, ip=ip, info=info)
    if not mac:
        return "", None
    with _lock:
        rec = _clients[mac]
        _drop_expired_locked(rec)
        if not rec["queue"]:
            return mac, None
        cmd = rec["queue"].pop(0)
        args = dict(cmd.get("args") or {}, mac=mac)
        cmd["args"] = args
        for k, v in args.items():          # 平铺参数（id/type/args 不被覆盖）
            if k not in ("id", "type", "args", "created"):
                cmd[k] = v
        rec["last_cmd"] = {"id": cmd["id"], "type": cmd["type"], "sent": time.time()}
        return mac, cmd


def result(mac, cmd_id, ok, msg=""):
    """客户机回报指令执行结果，返回 (True, "") 或 (False, 原因)。"""
    mac = _norm_mac(mac)
    if not mac:
        return False, "MAC 不合法"
    try:
        cmd_id = int(cmd_id)
    except (TypeError, ValueError):
        return False, "指令 id 不合法"
    with _lock:
        rec = _clients.get(mac)
        if not rec:
            return False, "未知客户机"
        ctype = ""
        lc = rec.get("last_cmd")
        if lc and lc.get("id") == cmd_id:
            ctype = lc.get("type", "")
        rec["results"].insert(0, {"id": cmd_id, "type": ctype, "ok": bool(ok),
                                  "msg": str(msg)[:400], "ts": time.time()})
        del rec["results"][MAX_RESULTS:]
    return True, ""


def last_result(mac):
    """最近一条执行结果（没有则 None）。"""
    mac = _norm_mac(mac)
    with _lock:
        rec = _clients.get(mac)
        if not rec or not rec["results"]:
            return None
        return dict(rec["results"][0])


# ---------- 网盘账号映射（客户机 MAC -> 网盘账号） ----------
def account_of(mac):
    """某台客户机映射到的网盘账号；未设置返回 ""。"""
    mac = _norm_mac(mac)
    with _lock:
        rec = _clients.get(mac)
        return (rec.get("account") or "") if rec else ""


def set_account(mac, account):
    """设置/清除（account 为空）某台机器的网盘账号映射，并落盘。"""
    mac = _norm_mac(mac)
    if not mac:
        return False, "MAC 不合法"
    account = (account or "").strip()
    if account and (len(account) > MAX_ACCOUNT_LEN
                    or not all(c.isalnum() or c in "_-" for c in account)):
        return False, "账号名不合法（3-32 位字母/数字/下划线/短横线）"
    with _lock:
        rec = _ensure(mac)
        rec["account"] = account
        _save_clients_locked()
    return True, ("已设置网盘账号：%s" % account) if account else "已清除网盘账号"


def set_mount_wanted(mac, want):
    """记录“这台机器是否应该挂着网盘”。

    状态放服务器而不是客户机：无盘客户机每次重启叠加盘都会丢，只有服务器记住
    这个意图，客户机才能在每次开机心跳时自动把网盘挂回来。
    """
    mac = _norm_mac(mac)
    if not mac:
        return False, "MAC 不合法"
    with _lock:
        rec = _ensure(mac)
        rec["want_mount"] = bool(want)
        _save_clients_locked()
    return True, ""


def mount_wanted(mac):
    """这台机器是否需要挂着网盘（服务器侧意图）。"""
    mac = _norm_mac(mac)
    with _lock:
        rec = _clients.get(mac)
        return bool(rec.get("want_mount")) if rec else False


def mac_for_account(account):
    """反向查询：哪个 MAC 用了这个网盘账号（没有返回 ""）。"""
    account = (account or "").strip()
    if not account:
        return ""
    with _lock:
        for mac, rec in sorted(_clients.items()):
            if rec.get("account") == account:
                return mac
    return ""


# ---------- 网盘会话令牌（客户机托盘“登录网盘”后用，避免在客户机上存账号密码） ----------
def dav_session_issue(user):
    """用户登录成功后签发一个网盘会话令牌（WebDAV 里当密码用）。"""
    tok = secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        for t in [t for t, s in _dav_sessions.items() if s["exp"] < now]:
            _dav_sessions.pop(t, None)
        _dav_sessions[tok] = {"user": user, "exp": now + DAV_SESSION_TTL}
    return tok


def dav_session_user(tok):
    """会话令牌对应的网盘用户名；无效/过期返回 ""。"""
    if not tok:
        return ""
    with _lock:
        rec = _dav_sessions.get(tok)
        if not rec:
            return ""
        if time.time() > rec["exp"]:
            _dav_sessions.pop(tok, None)
            return ""
        return rec["user"]


def dav_session_revoke(tok):
    """注销：让令牌立即失效。"""
    if not tok:
        return
    with _lock:
        _dav_sessions.pop(tok, None)


def set_dav_user(mac, user):
    """记录/清除某台客户机当前登录的网盘账号（后台页面展示用）。"""
    mac = _norm_mac(mac)
    if not mac:
        return
    with _lock:
        rec = _ensure(mac)
        rec["dav_user"] = (user or "")[:MAX_ACCOUNT_LEN]
        rec["dav_since"] = time.time() if user else 0.0

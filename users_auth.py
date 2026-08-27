# -*- coding: utf-8 -*-
"""
users_auth.py —— 多账号存储与认证模块（供 iscsi_broker.py 与 cloud_store.py 共用）。

设计要点：
1. 纯 Python 标准库实现，禁止 import iscsi_broker（避免循环依赖）。
2. 用户存储文件 <overlay_dir>/users.conf，每行格式：
       <用户名>$sha256$<salt_hex>$<digest_hex>$<quota_bytes>
   哈希算法与主程序 admin.conf 完全一致：
       digest = sha256((salt + pwd).encode("utf-8")).hexdigest()
3. 配置文件 <overlay_dir>/cloud.conf，一行 default_quota=<字节数>。
4. 所有写文件均为原子写（先写 .tmp 再 os.replace），并尽力 chmod 0o600。
5. 模块级 threading.Lock 保护所有读写，所有公开函数线程安全。
6. 解析 users.conf 时跳过损坏行；文件不存在则创建空文件。

使用前必须先调用 setup(overlay_dir, admin_verify) 完成初始化。
"""

import hashlib
import os
import re
import secrets
import threading

# ---------- 常量与正则 ----------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
PW_RE = re.compile(r"^[A-Za-z0-9_\-]{6,32}$")
DEFAULT_QUOTA_BYTES = 1 << 30    # 默认配额 1 GiB
QUOTA_MAX_BYTES = 8 << 40        # 配额上限 8 TiB

# ---------- 模块级状态（由 _LOCK 保护） ----------
_LOCK = threading.Lock()
_OVERLAY_DIR = None      # 配置文件目录（setup 时由主程序注入）
_ADMIN_VERIFY = None     # 主程序注入的管理员密码校验 callable(pwd)->bool
_USERS_FILE = None       # os.path.join(_OVERLAY_DIR, "users.conf")
_CLOUD_CONF_FILE = None  # os.path.join(_OVERLAY_DIR, "cloud.conf")


# ---------- 内部工具 ----------
def _require_setup():
    if _OVERLAY_DIR is None:
        raise RuntimeError(
            "users_auth.setup() 尚未调用（主程序启动时应先 setup(OVERLAY_DIR, admin_verify)）")


def _atomic_write(path, content):
    """原子写：先写 path.tmp 再 os.replace，并尽力 chmod 0o600。"""
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


def _load_users_unlocked():
    """读取 users.conf -> {name: {"stored": "sha256$salt$digest", "quota": int}}；跳过损坏行。"""
    users = {}
    if not os.path.exists(_USERS_FILE):
        _atomic_write(_USERS_FILE, "")   # 文件不存在则创建空文件
        return users
    try:
        with open(_USERS_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return users
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("$")
        if len(parts) != 5:
            continue
        name, algo, salt, digest, quota_s = parts
        if algo != "sha256" or not USERNAME_RE.match(name):
            continue
        try:
            quota = int(quota_s)
        except ValueError:
            continue
        if quota <= 0 or quota > QUOTA_MAX_BYTES:
            continue
        users[name] = {"stored": "%s$%s$%s" % (algo, salt, digest), "quota": quota}
    return users


def _write_users_unlocked(users):
    """整体重写 users.conf（调用方必须已持有 _LOCK）。"""
    lines = []
    for name in sorted(users):
        rec = users[name]
        lines.append("%s$%s$%d\n" % (name, rec["stored"], rec["quota"]))
    _atomic_write(_USERS_FILE, "".join(lines))


def _read_default_quota_unlocked():
    """读取 cloud.conf 的 default_quota；缺失/损坏回退 DEFAULT_QUOTA_BYTES。"""
    if not os.path.exists(_CLOUD_CONF_FILE):
        return DEFAULT_QUOTA_BYTES
    try:
        with open(_CLOUD_CONF_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return DEFAULT_QUOTA_BYTES
    for line in lines:
        line = line.strip()
        if line.startswith("default_quota="):
            try:
                q = int(line[len("default_quota="):].strip())
            except ValueError:
                continue
            if 0 < q <= QUOTA_MAX_BYTES:
                return q
    return DEFAULT_QUOTA_BYTES


# ---------- 公开 API ----------
def setup(overlay_dir: str, admin_verify) -> None:
    """初始化模块（主程序启动时调用一次）。

    overlay_dir：配置文件目录（主程序传 OVERLAY_DIR）
    admin_verify：主程序注入的 callable(pwd)->bool，用于校验管理员密码（admin.conf 哈希）
    """
    global _OVERLAY_DIR, _ADMIN_VERIFY, _USERS_FILE, _CLOUD_CONF_FILE
    with _LOCK:
        _OVERLAY_DIR = overlay_dir
        _ADMIN_VERIFY = admin_verify
        _USERS_FILE = os.path.join(overlay_dir, "users.conf")
        _CLOUD_CONF_FILE = os.path.join(overlay_dir, "cloud.conf")
        os.makedirs(overlay_dir, exist_ok=True)
        if not os.path.exists(_USERS_FILE):
            _atomic_write(_USERS_FILE, "")
        if not os.path.exists(_CLOUD_CONF_FILE):
            _atomic_write(_CLOUD_CONF_FILE, "default_quota=%d\n" % DEFAULT_QUOTA_BYTES)


def hash_password(pwd, salt=None) -> str:
    """生成加盐 SHA256 哈希，格式与主程序 admin.conf 一致：sha256$<salt_hex>$<digest_hex>。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
    return "sha256$%s$%s" % (salt, digest)


def verify_password(pwd, stored) -> bool:
    """常量时间比较（secrets.compare_digest）；stored 格式非法返回 False。"""
    try:
        algo, salt, digest = stored.strip().split("$", 2)
        if algo != "sha256":
            return False
        expect = hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
        return secrets.compare_digest(expect, digest)
    except Exception:
        return False


def register_user(name, pwd):
    """注册普通用户：校验通过则写入 users.conf，配额取当前默认配额。

    返回 (True, "注册成功") 或 (False, 中文原因)。
    """
    _require_setup()
    name = (name or "").strip()
    if not USERNAME_RE.match(name):
        return False, "用户名需为 3-32 位字母/数字/下划线/短横线"
    if name.lower() == "admin":
        return False, "保留用户名 admin 不可注册"
    if not PW_RE.match(pwd or ""):
        return False, "密码需为 6-32 位字母/数字/下划线/短横线"
    with _LOCK:
        users = _load_users_unlocked()
        if name in users:
            return False, "用户名已存在"
        users[name] = {"stored": hash_password(pwd),
                       "quota": _read_default_quota_unlocked()}
        try:
            _write_users_unlocked(users)
        except Exception as e:
            return False, "保存失败：%s" % e
    return True, "注册成功"


def check_login(user, pwd):
    """登录校验：user=="admin" 且 admin_verify(pwd) 通过 -> "admin"；
    普通用户查 users.conf 且密码正确 -> "user"；否则 None。"""
    _require_setup()
    user = (user or "").strip()
    if user == "admin":
        if _ADMIN_VERIFY is None:
            return None
        try:
            return "admin" if _ADMIN_VERIFY(pwd or "") else None
        except Exception:
            return None
    if not USERNAME_RE.match(user):
        return None
    with _LOCK:
        rec = _load_users_unlocked().get(user)
    if rec is None:
        return None
    return "user" if verify_password(pwd or "", rec["stored"]) else None


def user_exists(name) -> bool:
    """用户名是否已占用（含保留名 admin）。"""
    _require_setup()
    name = (name or "").strip()
    if name.lower() == "admin":
        return True
    if not USERNAME_RE.match(name):
        return False
    with _LOCK:
        return name in _load_users_unlocked()


def quota_of(name) -> int:
    """用户配额字节数；admin 恒为 0；用户不存在返回 0。"""
    _require_setup()
    name = (name or "").strip()
    if name == "admin":
        return 0
    if not USERNAME_RE.match(name):
        return 0
    with _LOCK:
        rec = _load_users_unlocked().get(name)
    return rec["quota"] if rec else 0


def list_users() -> list:
    """返回 [{"name":..., "quota_bytes":..., "is_admin":...}]，
    含 admin 行（quota_bytes=0），按名字排序。"""
    _require_setup()
    with _LOCK:
        users = _load_users_unlocked()
    out = [{"name": "admin", "quota_bytes": 0, "is_admin": True}]
    out.extend({"name": n, "quota_bytes": users[n]["quota"], "is_admin": False}
               for n in users)
    out.sort(key=lambda u: u["name"])
    return out


def set_user_quota(name, quota_bytes: int):
    """修改指定普通用户的配额。返回 (True, "配额已更新") 或 (False, 中文原因)。"""
    _require_setup()
    name = (name or "").strip()
    if name.lower() == "admin":
        return False, "admin 为内置账号（无配额限制），不可修改"
    if not USERNAME_RE.match(name):
        return False, "用户名不合法"
    if (isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int)
            or not (0 < quota_bytes <= QUOTA_MAX_BYTES)):
        return False, "配额需为 1 字节至 8TiB 之间的整数"
    with _LOCK:
        users = _load_users_unlocked()
        if name not in users:
            return False, "用户不存在"
        users[name]["quota"] = quota_bytes
        try:
            _write_users_unlocked(users)
        except Exception as e:
            return False, "保存失败：%s" % e
    return True, "配额已更新"


def get_default_quota() -> int:
    """当前默认配额字节数（cloud.conf，缺失回退 DEFAULT_QUOTA_BYTES）。"""
    _require_setup()
    with _LOCK:
        return _read_default_quota_unlocked()


def set_default_quota(quota_bytes: int):
    """修改默认配额并持久化到 cloud.conf。返回 (True, "默认配额已更新") 或 (False, 中文原因)。"""
    _require_setup()
    if (isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int)
            or not (0 < quota_bytes <= QUOTA_MAX_BYTES)):
        return False, "配额需为 1 字节至 8TiB 之间的整数"
    try:
        with _LOCK:
            _atomic_write(_CLOUD_CONF_FILE, "default_quota=%d\n" % quota_bytes)
    except Exception as e:
        return False, "保存失败：%s" % e
    return True, "默认配额已更新"

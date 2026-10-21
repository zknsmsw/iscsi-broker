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
7. users.conf / cloud.conf 的解析结果按文件签名（mtime_ns+size）缓存：文件没变就不重复
   读盘和逐行解析，被本模块改写或外部修改后自动失效。缓存字典视为只读，写路径先复制。

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

# 保留用户名（一律不可注册、不可登录、不可改配额；比较时统一转小写）。
# 为什么必须保留：普通用户的私有目录就是 CLOUD_ROOT/<用户名>，而 "_common" 正是
# cloud_store 的全局“通用文件”目录。若允许注册 _common，任何人在公开注册页拿到这个
# 账号后，其网盘根目录就等于通用文件目录，可以任意覆盖/删除管理员上传的公共文件
# （所有未登录客户机的 Z 盘、所有用户的“通用文件”都受影响）。
RESERVED_NAMES = frozenset({"admin", "_common"})


def is_reserved(name) -> bool:
    """用户名是否被保留（admin / _common）。"""
    return (name or "").strip().lower() in RESERVED_NAMES

# ---------- 模块级状态（由 _LOCK 保护） ----------
_LOCK = threading.Lock()
_OVERLAY_DIR = None      # 配置文件目录（setup 时由主程序注入）
_ADMIN_VERIFY = None     # 主程序注入的管理员密码校验 callable(pwd)->bool
_USERS_FILE = None       # os.path.join(_OVERLAY_DIR, "users.conf")
_CLOUD_CONF_FILE = None  # os.path.join(_OVERLAY_DIR, "cloud.conf")

# 解析结果缓存：(文件签名, 解析结果)。签名取 st_mtime_ns + st_size：
# 网盘每翻一页要读一次配额、每次登录要查一次用户，老实现每次都重新打开并逐行解析
# users.conf（用户多了是线性开销）。这里改成文件没变就直接复用上次的解析结果；
# 文件被本模块改写或外部改动（mtime/大小变化）时自动失效重读。
_users_cache = None      # (sig, {name: {"stored","quota"}})；调用方只读，勿原地修改
_quota_cache = None      # (sig, default_quota 字节数)


def _file_sig(path):
    """文件签名 (mtime_ns, size)：文件不存在返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


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


def _parse_users_file():
    """真正读取并解析 users.conf（仅在缓存失效时调用）。"""
    users = {}
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
        if is_reserved(name):
            continue          # 保留名（admin / _common）即使残留在文件里也一律不生效
        try:
            quota = int(quota_s)
        except ValueError:
            continue
        if quota <= 0 or quota > QUOTA_MAX_BYTES:
            continue
        users[name] = {"stored": "%s$%s$%s" % (algo, salt, digest), "quota": quota}
    return users


def _load_users_unlocked():
    """读取 users.conf -> {name: {"stored": "sha256$salt$digest", "quota": int}}；跳过损坏行。

    结果按文件签名缓存，文件未变化时直接复用（调用方必须持 _LOCK）。
    返回的字典是缓存本体：调用方只许读，要改必须先复制（见 register_user/set_user_quota）。
    """
    global _users_cache
    if not os.path.exists(_USERS_FILE):
        _atomic_write(_USERS_FILE, "")   # 文件不存在则创建空文件
    sig = _file_sig(_USERS_FILE)
    if _users_cache is not None and _users_cache[0] == sig:
        return _users_cache[1]
    users = _parse_users_file()
    _users_cache = (_file_sig(_USERS_FILE), users)
    return users


def _write_users_unlocked(users):
    """整体重写 users.conf（调用方必须已持有 _LOCK）。写完同步刷新缓存。"""
    global _users_cache
    lines = []
    for name in sorted(users):
        rec = users[name]
        lines.append("%s$%s$%d\n" % (name, rec["stored"], rec["quota"]))
    _atomic_write(_USERS_FILE, "".join(lines))
    # 写入成功后再更新缓存：写失败时缓存保持原值，不会出现「内存里已改、磁盘没改」
    _users_cache = (_file_sig(_USERS_FILE), users)


def _read_default_quota_unlocked():
    """读取 cloud.conf 的 default_quota；缺失/损坏回退 DEFAULT_QUOTA_BYTES（按文件签名缓存）。"""
    global _quota_cache
    sig = _file_sig(_CLOUD_CONF_FILE)
    if _quota_cache is not None and _quota_cache[0] == sig:
        return _quota_cache[1]
    quota = DEFAULT_QUOTA_BYTES
    try:
        with open(_CLOUD_CONF_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("default_quota="):
            try:
                q = int(line[len("default_quota="):].strip())
            except ValueError:
                continue
            if 0 < q <= QUOTA_MAX_BYTES:
                quota = q
                break
    _quota_cache = (sig, quota)
    return quota


# ---------- 公开 API ----------
def setup(overlay_dir: str, admin_verify) -> None:
    """初始化模块（主程序启动时调用一次）。

    overlay_dir：配置文件目录（主程序传 OVERLAY_DIR）
    admin_verify：主程序注入的 callable(pwd)->bool，用于校验管理员密码（admin.conf 哈希）
    """
    global _OVERLAY_DIR, _ADMIN_VERIFY, _USERS_FILE, _CLOUD_CONF_FILE
    global _users_cache, _quota_cache
    with _LOCK:
        _OVERLAY_DIR = overlay_dir
        _ADMIN_VERIFY = admin_verify
        _USERS_FILE = os.path.join(overlay_dir, "users.conf")
        _CLOUD_CONF_FILE = os.path.join(overlay_dir, "cloud.conf")
        _users_cache = None          # 换了配置目录，旧缓存作废
        _quota_cache = None
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
    if is_reserved(name):
        return False, "保留用户名 %s 不可注册" % name
    if not PW_RE.match(pwd or ""):
        return False, "密码需为 6-32 位字母/数字/下划线/短横线"
    with _LOCK:
        users = _load_users_unlocked()
        if name in users:
            return False, "用户名已存在"
        users = dict(users)   # 缓存字典只读，写前复制（写失败时缓存保持原值）
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
    if is_reserved(user):
        return None      # _common 等保留名即使残留在 users.conf 里也不得登录
    with _LOCK:
        rec = _load_users_unlocked().get(user)
    if rec is None:
        return None
    return "user" if verify_password(pwd or "", rec["stored"]) else None


def user_exists(name) -> bool:
    """用户名是否已占用（含保留名 admin / _common）。"""
    _require_setup()
    name = (name or "").strip()
    if is_reserved(name):
        return True
    if not USERNAME_RE.match(name):
        return False
    with _LOCK:
        return name in _load_users_unlocked()


def quota_of(name) -> int:
    """用户配额字节数；admin 恒为 0；保留名/用户不存在返回 0。"""
    _require_setup()
    name = (name or "").strip()
    if name == "admin":
        return 0
    if not USERNAME_RE.match(name) or is_reserved(name):
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
    if is_reserved(name):
        return False, "%s 为内置保留名，不可修改" % name
    if not USERNAME_RE.match(name):
        return False, "用户名不合法"
    if (isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int)
            or not (0 < quota_bytes <= QUOTA_MAX_BYTES)):
        return False, "配额需为 1 字节至 8TiB 之间的整数"
    with _LOCK:
        users = _load_users_unlocked()
        if name not in users:
            return False, "用户不存在"
        users = dict(users)   # 缓存字典只读，写前复制
        users[name] = {"stored": users[name]["stored"], "quota": quota_bytes}
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
    global _quota_cache
    _require_setup()
    if (isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int)
            or not (0 < quota_bytes <= QUOTA_MAX_BYTES)):
        return False, "配额需为 1 字节至 8TiB 之间的整数"
    try:
        with _LOCK:
            _atomic_write(_CLOUD_CONF_FILE, "default_quota=%d\n" % quota_bytes)
            _quota_cache = (_file_sig(_CLOUD_CONF_FILE), quota_bytes)
    except Exception as e:
        return False, "保存失败：%s" % e
    return True, "默认配额已更新"

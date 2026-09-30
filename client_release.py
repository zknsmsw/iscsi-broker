# -*- coding: utf-8 -*-
"""
client_release.py —— 客户机客户端（iscsi-broker-agent.exe）的“发布库”（供 iscsi_broker.py 使用）。

为什么要有它：客户机装的是无盘母盘里的 agent，以前升级必须重做母盘。现在把新版 exe
放在服务器上版本化保存，客户机托盘点「检查更新」就拉最新版自我替换（详见 client/README.md）。

设计要点：
1. 纯标准库；发布库目录是 <BASE_DIR>/client_dist/，内容：
       index.json                          当前版本 + 每个版本的元数据（sha256/大小/时间/来源）
       iscsi-broker-agent-<ver>.exe        实际文件，只保留最近 KEEP_RELEASES 个
2. 版本号只允许 A.B 或 A.B.C（纯数字段），比较按数字逐段比，绝不能用字符串比大小。
3. 服务器自己编译是**可选**能力：主机上装了 mono 的 mcs/csc 才能真正编译（Linux → .NET
   Framework exe 的交叉编译）；没装就只能由管理员上传 build.bat 编出来的 exe。
   build() 永远返回 (ok, msg, ver)，绝不让“编译失败”把 Web 线程打挂。
4. 只被主程序调用，不反向 import iscsi_broker（避免循环依赖）。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

DIR_NAME = "client_dist"          # 发布库目录名（位于 BASE_DIR 下）
INDEX_NAME = "index.json"
EXE_PREFIX = "iscsi-broker-agent-"
EXE_SUFFIX = ".exe"
KEEP_RELEASES = 5                 # 只保留最近几个版本，旧的自动删
VER_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?$")
MAX_UPLOAD = 32 * 1024 * 1024     # 上传/编译产物大小上限（够放 200KB 的 exe，防误传大文件）

_lock = threading.RLock()
_dir = None            # 发布库目录（setup 后有效）
_index = {"current": "", "releases": []}   # releases 按版本升序（新→旧由调用方决定）
_loaded = False


class ReleaseError(Exception):
    """发布库操作失败（调用方把 message 直接展示给管理员）。"""


# ---------- 版本号 ----------
def parse_ver(ver):
    """'1.2.3' -> (1,2,3)；非法返回 None。"""
    if not isinstance(ver, str):
        return None
    m = VER_RE.match(ver.strip())
    if not m:
        return None
    return tuple(int(x) if x is not None else 0 for x in m.groups())


def ver_key(ver):
    """排序/比较用：非法版本返回 (0,0,0)，保证不会抛异常。"""
    return parse_ver(ver) or (0, 0, 0)


def newer(a, b):
    """a 是否比 b 新（按数字段比较，'1.10' > '1.9'）。"""
    return ver_key(a) > ver_key(b)


def ver_ok(ver):
    return parse_ver(ver) is not None


# ---------- 初始化 / 落盘 ----------
def setup(base_dir):
    """初始化发布库目录并读入 index.json（不存在就建）。"""
    global _dir, _index, _loaded
    with _lock:
        _dir = os.path.join(base_dir, DIR_NAME)
        os.makedirs(_dir, exist_ok=True)
        _index = _read_index_locked()
        _loaded = True
        return _dir


def dir_path():
    return _dir


def _require_setup():
    if not _loaded or not _dir:
        raise ReleaseError("client_release 未初始化（程序启动时应调用 setup）")


def _index_path():
    return os.path.join(_dir, INDEX_NAME)


def _read_index_locked():
    """读 index.json；损坏就当空库（不抛异常，最多是“没有发布”）。"""
    try:
        with open(_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"current": "", "releases": []}
    if not isinstance(data, dict):
        return {"current": "", "releases": []}
    rels = [r for r in (data.get("releases") or [])
            if isinstance(r, dict) and ver_ok(str(r.get("ver", "")))]
    cur = str(data.get("current", "") or "")
    if cur and not any(r["ver"] == cur for r in rels):
        cur = ""                     # index 与文件对不上时，宁可显示“没有发布”
    return {"current": cur, "releases": rels}


def _write_index_locked():
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_index, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _index_path())


def _exe_name(ver):
    return EXE_PREFIX + ver + EXE_SUFFIX


def _exe_path(ver):
    return os.path.join(_dir, _exe_name(ver))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(256 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _looks_like_exe(path):
    """粗校验：MZ 头 + PE 头（防止把 html/zip 之类传成客户端）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
    except OSError:
        return False
    if len(head) < 0x40 or head[0:2] != b"MZ":
        return False
    pe_off = int.from_bytes(head[0x3C:0x40], "little")
    try:
        with open(path, "rb") as f:
            f.seek(pe_off)
            return f.read(4) == b"PE\x00\x00"
    except OSError:
        return False


def _record(ver, path, source, notes=""):
    return {"ver": ver, "file": _exe_name(ver), "sha256": _sha256_file(path),
            "size": os.path.getsize(path), "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": source, "notes": notes or ""}


def _prune_locked():
    """只保留最近 KEEP_RELEASES 个版本的文件（先删文件再清 index，避免出现指向空文件的记录）。"""
    rels = sorted(_index["releases"], key=lambda r: ver_key(r["ver"]), reverse=True)
    keep, drop = rels[:KEEP_RELEASES], rels[KEEP_RELEASES:]
    for r in drop:
        try:
            os.remove(os.path.join(_dir, r.get("file") or _exe_name(r["ver"])))
        except OSError:
            pass
    _index["releases"] = keep


def _adopt_locked(ver, tmp_path, source, notes=""):
    """把已就绪的临时 exe 收录成该版本的发布文件并更新 index。"""
    if not ver_ok(ver):
        raise ReleaseError("版本号非法（只允许 A.B 或 A.B.C）：%r" % (ver,))
    if not _looks_like_exe(tmp_path):
        raise ReleaseError("这个文件不像 Windows 可执行文件（没有 MZ/PE 头），拒绝收录")
    size = os.path.getsize(tmp_path)
    if size <= 0 or size > MAX_UPLOAD:
        raise ReleaseError("文件大小不合适：%d 字节" % size)
    dst = _exe_path(ver)
    os.replace(tmp_path, dst)              # 同目录 rename，原子
    rec = _record(ver, dst, source, notes)
    _index["releases"] = [r for r in _index["releases"] if r["ver"] != ver] + [rec]
    _prune_locked()
    # current 只前进不后退（同版本覆盖写入时保持 current 不变）
    if not _index["current"] or newer(ver, _index["current"]) or _index["current"] == ver:
        _index["current"] = ver
    _write_index_locked()
    return rec


# ---------- 查询 ----------
def info():
    """给 Web 页面/接口用的发布库快照（已按版本从新到旧排序）。"""
    _require_setup()
    with _lock:
        rels = sorted(_index["releases"], key=lambda r: ver_key(r["ver"]), reverse=True)
        return {"current": _index["current"], "dir": _dir,
                "releases": [dict(r) for r in rels]}


def current():
    with _lock:
        return _index["current"]


def release_of(ver):
    """取某个版本的记录；不存在返回 None。"""
    with _lock:
        for r in _index["releases"]:
            if r["ver"] == ver:
                return dict(r)
    return None


def path_of(ver):
    """某个版本的 exe 路径；记录或文件不存在返回 None。"""
    r = release_of(ver)
    if not r:
        return None
    p = os.path.join(_dir, r.get("file") or _exe_name(ver))
    return p if os.path.isfile(p) else None


def available_for(cur):
    """当前版本 cur 有没有可用的更新？返回 (bool, 记录)。

    只要发布库里存在比 cur 新的版本就返回那个“最新且比 cur 新”的；cur 非法/为空时
    返回最新的发布（让老客户机也能被拉齐）。
    """
    with _lock:
        if not _index["current"]:
            return False, None
        cur_ok = ver_ok(cur)
        best = None
        for r in _index["releases"]:
            if not cur_ok or newer(r["ver"], cur):
                if best is None or newer(r["ver"], best["ver"]):
                    best = r
        return (best is not None), (dict(best) if best else None)


# ---------- 写入：上传 / 编译 ----------
def import_bytes(blob, ver, notes="", source="upload"):
    """管理员上传：校验后收录为 ver 版本。"""
    _require_setup()
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise ReleaseError("上传内容为空")
    if len(blob) > MAX_UPLOAD:
        raise ReleaseError("文件太大（上限 %d MB）" % (MAX_UPLOAD // 1024 // 1024))
    with _lock:
        fd, tmp = tempfile.mkstemp(prefix=".up_", suffix=EXE_SUFFIX, dir=_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            return _adopt_locked(ver, tmp, source, notes)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def import_file(src_path, ver, notes="", source="upload"):
    """从服务器本地文件收录（命令行用：--publish-client）。"""
    with open(src_path, "rb") as f:
        return import_bytes(f.read(), ver, notes, source)


def _find_compiler():
    """找一个可用的 C# 编译器（Linux 上是 mono 的 mcs/csc；Windows 上是 .NET 的 csc）。"""
    for name in ("mcs", "csc", "mono-csc"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
              r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"):
        if os.path.isfile(p):
            return p
    return ""


def compiler_available():
    return bool(_find_compiler())


def read_source_version(agent_cs):
    """从 Agent.cs 里读 `private const string VERSION = "1.1";`。"""
    try:
        with open(agent_cs, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        raise ReleaseError("读不到客户端源码：%s" % e)
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', txt)
    if not m or not ver_ok(m.group(1)):
        raise ReleaseError("客户端源码里没找到合法的 VERSION 常量")
    return m.group(1).strip()


def build(source_dir, ver=None, notes=""):
    """用服务器上装的 C# 编译器编译 client/Agent.cs，收录成新版本。

    source_dir：仓库根目录（client/Agent.cs 在其中）。
    返回 (ok, msg, ver)。编译失败/没编译器时 ok=False，msg 是给人看的说明。
    """
    _require_setup()
    agent_cs = os.path.join(source_dir, "client", "Agent.cs")
    icon = os.path.join(source_dir, "client", "agent.ico")
    if not os.path.isfile(agent_cs):
        return False, "找不到客户端源码：%s" % agent_cs, ""
    cc = _find_compiler()
    if not cc:
        return (False, "服务器上没有 C# 编译器（mono-mcs / csc），无法在这里编译；"
                       "请在 Windows 上跑 client\\build.bat 然后用后台上传产物", "")
    try:
        src_ver = read_source_version(agent_cs)
    except ReleaseError as e:
        return False, str(e), ""
    target_ver = (ver or src_ver).strip()
    if not ver_ok(target_ver):
        return False, "版本号非法：%r" % target_ver, ""
    with _lock:
        out = os.path.join(_dir, ".build_" + target_ver + EXE_SUFFIX)
    cmd = [cc, "/nologo", "/target:exe", "/platform:anycpu", "/optimize+", "/warn:4",
           "/out:" + out,
           "/reference:System.dll", "/reference:System.Core.dll",
           "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
           "/reference:System.Web.Extensions.dll"]
    if os.path.isfile(icon):
        cmd += ['/win32icon:' + icon, '/resource:' + icon + ",agent.ico"]
    cmd.append(agent_cs)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        return False, "编译失败：%s" % e, ""
    if r.returncode != 0 or not os.path.isfile(out):
        try:
            os.remove(out)
        except OSError:
            pass
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return False, "编译失败（%s）：%s" % (cc, " | ".join(tail[-6:]) or "无输出"), ""
    try:
        rec = _adopt_locked(target_ver, out, "build", notes or ("src=%s" % src_ver))
    except ReleaseError as e:
        try:
            os.remove(out)
        except OSError:
            pass
        return False, str(e), ""
    return True, "已编译并发布 v%s（%d 字节）" % (rec["ver"], rec["size"]), rec["ver"]


def remove(ver):
    """删掉某个版本（仅当它不是 current）。"""
    _require_setup()
    with _lock:
        if ver == _index["current"]:
            raise ReleaseError("这是当前发布版本，先发布/选用别的版本再删")
        r = release_of(ver)
        if not r:
            raise ReleaseError("没有这个版本：%s" % ver)
        try:
            os.remove(os.path.join(_dir, r.get("file") or _exe_name(ver)))
        except OSError:
            pass
        _index["releases"] = [x for x in _index["releases"] if x["ver"] != ver]
        _write_index_locked()
        return True


def set_current(ver):
    """把某个已发布的版本设为“当前版本”（只用于管理员显式指定）。"""
    _require_setup()
    with _lock:
        if not release_of(ver):
            raise ReleaseError("没有这个版本：%s" % ver)
        _index["current"] = ver
        _write_index_locked()
        return True

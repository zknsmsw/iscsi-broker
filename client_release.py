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
3. **不在服务器上编译客户端**：客户端是 .NET Framework + WinForms + Win32 P/Invoke 的
   Windows 程序，必须在 Windows 上（client\\build.bat）编译，产物由管理员上传到这里，
   服务器只负责版本化存放与分发（曾经的 mono 交叉编译已删除，见 README）。
4. 只被主程序调用，不反向 import iscsi_broker（避免循环依赖）。
"""

import hashlib
import json
import os
import re
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
_prog_dir = None       # 程序目录（用于读 client/Agent.cs 里的 VERSION）
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
def setup(base_dir, prog_dir=None):
    """初始化发布库目录并读入 index.json（不存在就建）。

    prog_dir 是程序目录（存放 client/Agent.cs 的地方），用于读客户机源码里的 VERSION——
    发布时必须用它校验“发布版本号 == exe 内编译的 VERSION”，否则客户机会无限循环更新。
    """
    global _dir, _index, _loaded, _prog_dir
    with _lock:
        _dir = os.path.join(base_dir, DIR_NAME)
        os.makedirs(_dir, exist_ok=True)
        _index = _read_index_locked()
        _prog_dir = prog_dir or None    # 每次 setup 都以本次参数为准（不传就表示不校验源码版本）
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


# ---------- 上传产物的深度校验 ----------
# 只查 MZ/PE 是不够的：**被截断的 exe 头部依然完好**，sha256/大小也自洽，客户机
# 下载校验全部通过、换上去之后 Windows 只会弹 “This application could not be
# started.”（.NET 运行时初始化失败），而且无盘客户机换掉的 exe 自己没法回滚。
# 所以收录前必须验证：
#   ① 每个节区的原始数据都完整落在文件内（截断必挂）；
#   ② 存在 CLR 头（IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR ≠ 0）——客户端必须是
#      build.bat（csc，.NET Framework 4.x）编出来的托管程序，客户机才跑得起来；
#   ③ CLR 头与 CLR 元数据都在文件内，且元数据签名是 BSJB。
_IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR = 14
_PE32_MAGIC = 0x10B
_PE32PLUS_MAGIC = 0x20B


def _parse_pe(data):
    """解析 PE 头，返回 {"sections":[...], "dirs":{idx:(rva,size)}, "size":n}；非法返回 None。"""
    if len(data) < 0x40 or data[0:2] != b"MZ":
        return None
    pe_off = int.from_bytes(data[0x3C:0x40], "little")
    if pe_off < 0x40 or pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None
    coff = pe_off + 4
    num_sections = int.from_bytes(data[coff + 2:coff + 4], "little")
    size_opt = int.from_bytes(data[coff + 16:coff + 18], "little")
    opt = coff + 20
    if size_opt < 96 or opt + size_opt > len(data) or num_sections > 96:
        return None
    magic = int.from_bytes(data[opt:opt + 2], "little")
    if magic not in (_PE32_MAGIC, _PE32PLUS_MAGIC):
        return None
    num_dd = int.from_bytes(data[opt + 92:opt + 94], "little")
    dd_off = opt + (96 if magic == _PE32_MAGIC else 112)
    dirs = {}
    for i in range(min(num_dd, 16)):
        base = dd_off + i * 8
        if base + 8 > opt + size_opt or base + 8 > len(data):
            break
        dirs[i] = (int.from_bytes(data[base:base + 4], "little"),
                   int.from_bytes(data[base + 4:base + 8], "little"))
    sections = []
    sec_off = opt + size_opt
    for i in range(num_sections):
        b = sec_off + i * 40
        if b + 40 > len(data):
            return None
        sections.append({
            "vsize": int.from_bytes(data[b + 8:b + 12], "little"),
            "vaddr": int.from_bytes(data[b + 12:b + 16], "little"),
            "raw_size": int.from_bytes(data[b + 16:b + 20], "little"),
            "raw_ptr": int.from_bytes(data[b + 20:b + 24], "little"),
        })
    return {"sections": sections, "dirs": dirs, "size": len(data)}


def _rva_to_off(pe, rva):
    """RVA -> 文件偏移；落在文件外返回 None。"""
    for s in pe["sections"]:
        if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["raw_size"]):
            off = s["raw_ptr"] + (rva - s["vaddr"])
            return off if off < pe["size"] else None
    return None


def validate_client_exe(path):
    """深度校验客户端 exe 是否是“能被客户机跑起来的 .NET 程序集”。

    返回 (True, "") 或 (False, 中文原因)。这是收录前的最后一道闸门：宁可拒绝一个
    可疑产物，也不能把坏 exe 推给无盘客户机（它们没法自己回滚）。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, "读不到文件：%s" % e
    if size < 4096:
        return False, "文件太小（%d 字节），不像编译产物" % size
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, "读不到文件：%s" % e
    pe = _parse_pe(data)
    if pe is None:
        return False, "不是合法的 Windows PE 文件（MZ/PE 头或可选头异常）"
    for s in pe["sections"]:
        if s["raw_size"] == 0:
            continue
        if s["raw_ptr"] + s["raw_size"] > pe["size"]:
            return False, ("文件不完整：数据段超出文件末尾（%d > %d 字节），"
                           "通常是上传/编译被截断，不是完整的 exe"
                           % (s["raw_ptr"] + s["raw_size"], pe["size"]))
    com = pe["dirs"].get(_IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR)
    if not com or com[0] == 0 or com[1] == 0:
        return False, ("不是 .NET 程序集（缺少 CLR 头）：客户端必须在 Windows 上用 "
                       "client\\build.bat 编译（csc / .NET Framework），"
                       "其它工具链的产物客户机跑不起来")
    clr_off = _rva_to_off(pe, com[0])
    if clr_off is None or clr_off + 0x48 > len(data):
        return False, "CLR 头越界（文件可能损坏或被截断）"
    clr = data[clr_off:clr_off + 0x48]
    if int.from_bytes(clr[0:4], "little") < 0x48:
        return False, "CLR 头长度非法"
    meta_rva = int.from_bytes(clr[8:12], "little")
    meta_size = int.from_bytes(clr[12:16], "little")
    if meta_rva == 0 or meta_size < 0x10:
        return False, "缺少 CLR 元数据（不是完整程序集）"
    meta_off = _rva_to_off(pe, meta_rva)
    if meta_off is None or meta_off + meta_size > len(data):
        return False, "CLR 元数据越界（文件可能被截断）"
    if data[meta_off:meta_off + 4] != b"BSJB":
        return False, "CLR 元数据签名不对（文件损坏）"
    return True, ""


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
    # 发布版本号必须等于 exe 里编译进去的 VERSION：客户机就是拿它和本机 VERSION 比大小来
    # 判断“要不要更新/有没有更新成功”的。两者不一致（例如源码 VERSION=1.1，后台按 89.64
    # 发布）会让客户机换上后仍然报 1.1，于是服务器永远认为它有新版 —— 无限循环更新，
    # 每轮都会杀掉托盘、重启后台进程。这里直接拒绝，从源头掐掉。
    src_ver = source_version()
    if src_ver and src_ver != ver:
        raise ReleaseError(
            "版本号 %s 与服务器上 client/Agent.cs 里的 VERSION (%s) 不一致：客户机按 exe 内编译进去的"
            " VERSION 判断新旧，不一致会导致无限循环更新（换上去后仍报 v%s）。"
            "三种处理方式任选：① 把 client/Agent.cs 的 VERSION 改成 %s 后重新编译上传；"
            "② 版本号填 %s（表示就是当前这份源码编出来的）；"
            "③ 如果你本地已经改过 VERSION，先在服务器上 git pull 让源码同步，再重传。"
            % (ver, src_ver, src_ver, ver, src_ver))
    if not _looks_like_exe(tmp_path):
        raise ReleaseError("这个文件不像 Windows 可执行文件（没有 MZ/PE 头），拒绝收录")
    ok, why = validate_client_exe(tmp_path)
    if not ok:
        raise ReleaseError("拒绝收录：%s" % why)
    size = os.path.getsize(tmp_path)
    if size <= 0 or size > MAX_UPLOAD:
        raise ReleaseError("文件大小不合适：%d 字节" % size)
    dst = _exe_path(ver)
    os.replace(tmp_path, dst)              # 同目录 rename，原子
    rec = _record(ver, dst, source, notes)
    _index["releases"] = [r for r in _index["releases"] if r["ver"] != ver] + [rec]
    _prune_locked()
    # 管理员这次“上传并发布”就是要在客户机上生效的版本，直接设为 current。
    # 不再要求“版本号只能前进”：库里万一残留了一个号更大的错误记录（例如误发布的 100.0），
    # 小的版本号永远当不上 current，客户机也永远升不到它 —— 那种坑比“允许显式降级”更麻烦。
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

    以发布库里管理员指定的 **current** 为“应该升到哪一版”：
      - current 不存在 -> 没有发布；
      - cur 非法/为空 -> 直接把 current 给它（让老客户机拉齐）；
      - current 比 cur 新 -> 返回 current。
    为什么不是“取库里号最大的那一版”：库里可能残留号更大但内容不对的旧记录
    （例如误发布的 100.0），那样会把客户机永远推向那个坏版本，管理员后来指定的
    current（比如 1.2）反而永远升不上去。
    """
    with _lock:
        rec = None
        for r in _index["releases"]:
            if r["ver"] == _index["current"]:
                rec = r
                break
        if rec is None:
            return False, None
        if not ver_ok(cur) or newer(rec["ver"], cur):
            return True, dict(rec)
        return False, None


# ---------- 写入：上传 ----------
def source_version(prog_dir=None):
    """读客户端源码里的 VERSION（供后台上传时“版本号留空”用）；读不到返回 ""。

    注：这里只**读一个版本号**用于校验/展示，服务器不编译客户端（见模块说明）。
    """
    base = prog_dir or _prog_dir
    if not base:
        return ""
    try:
        with open(os.path.join(base, "client", "Agent.cs"), "r",
                  encoding="utf-8", errors="replace") as f:
            m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
        v = (m.group(1) if m else "").strip()
        return v if ver_ok(v) else ""
    except OSError:
        return ""


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

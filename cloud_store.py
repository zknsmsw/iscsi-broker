# -*- coding: utf-8 -*-
"""
cloud_store.py —— 个人网盘（云盘）存储模块（供 iscsi_broker.py 使用）。

设计要点：
1. 纯 Python 标准库实现；可 import users_auth，禁止 import iscsi_broker（避免循环依赖）。
2. 目录布局（云盘根由主程序 setup 时注入，一般传 os.path.join(BASE_DIR, "cloud")）：
       CLOUD_ROOT/<username>/   用户私有目录（目录名即用户名）
       CLOUD_ROOT/_common/      通用文件目录（对所有用户以"通用文件"虚拟文件夹只读展示）
       CLOUD_ROOT/.tmp/         上传临时目录（失败时清理）
3. 路径安全（resolve）：拒绝绝对路径 / ".." 段 / 反斜杠 / 空字节；
   os.path.normpath + os.path.commonpath 校验包含关系，再对结果
   os.path.realpath 二次校验（防符号链接逃逸）。不合法一律返回 None。
4. multipart/form-data 采用自写流式解析器（64KB 分块，禁止 cgi 模块——
   Python 3.13 已移除），文件 part 的 body 直接流式写入 .tmp 下的唯一临时文件；
   part body 结尾的 CRLF 属于分隔符终止序列，不写入文件。
5. 上传配额在流式写入过程中累计校验，超限立即中止并清理临时文件。
6. 文件名解码兼容 RFC5987 filename*=UTF-8''<percent> 与浏览器原始 UTF-8 字节
   （header 为 latin-1 层字节时尝试按 UTF-8 重新解码）。

使用前必须先调用 setup(cloud_root) 完成初始化。
"""

import os
import tempfile
import urllib.parse

# ---------- 常量 ----------
COMMON_DIR_NAME = "_common"   # 通用文件目录名
TMP_DIR_NAME = ".tmp"         # 上传临时目录名
VIRTUAL_COMMON = "通用文件"   # 通用文件的虚拟文件夹名（rel 前缀）
_CHUNK = 64 * 1024            # 流式读取分块大小
_MAX_HEADER = 64 * 1024       # 单个 multipart part 头最大字节数（防滥用）


class _QuotaExceeded(Exception):
    """内部异常：上传累计字节超过配额，用于中途中止。"""


# ---------- 模块级状态 ----------
_CLOUD_ROOT = None  # 云盘根目录（setup 时由主程序注入）


def _require_setup():
    if _CLOUD_ROOT is None:
        raise RuntimeError(
            "cloud_store.setup() 尚未调用（主程序启动时应先 setup(os.path.join(BASE_DIR,'cloud'))）")


def _drain(rfile, length):
    """从 rfile 读走并丢弃 length 字节（保持 HTTP 连接可复用）。"""
    while length > 0:
        data = rfile.read(min(_CHUNK, length))
        if not data:
            break
        length -= len(data)


# ---------- 公开 API ----------
def setup(cloud_root: str) -> None:
    """初始化模块（主程序启动时调用一次）：创建云盘根、_common 与 .tmp。"""
    global _CLOUD_ROOT
    _CLOUD_ROOT = os.path.abspath(cloud_root)
    os.makedirs(_CLOUD_ROOT, exist_ok=True)
    os.makedirs(os.path.join(_CLOUD_ROOT, COMMON_DIR_NAME), exist_ok=True)
    os.makedirs(os.path.join(_CLOUD_ROOT, TMP_DIR_NAME), exist_ok=True)


def user_home(user: str) -> str:
    """用户私有目录 CLOUD_ROOT/<user>/。用户名非法（含路径成分）抛 ValueError。"""
    _require_setup()
    user = (user or "").strip()
    if (not user or "/" in user or "\\" in user
            or user in (".", "..") or "\x00" in user):
        raise ValueError("invalid username: %r" % user)
    return os.path.join(_CLOUD_ROOT, user)


def common_dir() -> str:
    """通用文件目录 CLOUD_ROOT/_common/。"""
    _require_setup()
    return os.path.join(_CLOUD_ROOT, COMMON_DIR_NAME)


def _inside(path, base) -> bool:
    """path 是否等于 base 或位于 base 之下（绝对路径比较）。"""
    try:
        return (os.path.commonpath([os.path.abspath(path), os.path.abspath(base)])
                == os.path.abspath(base))
    except (ValueError, OSError):
        return False


def resolve(user: str, rel: str) -> str | None:
    """把用户视角相对路径 rel 映射为磁盘绝对路径。

    rel 为相对路径（"" = 根，子目录用 "/" 分隔）。安全规则：
    拒绝绝对路径、含 ".." 段、含反斜杠、含空字节；
    normpath 后 commonpath 校验包含关系，再 realpath 二次校验（防符号链接逃逸）。
    特殊：rel 为 "通用文件" 或 "通用文件/..." 时映射到 _common 下（只读区域）。
    不合法返回 None。
    """
    _require_setup()
    rel = rel if rel is not None else ""
    if not isinstance(rel, str):
        return None
    if "\x00" in rel or "\\" in rel or os.path.isabs(rel):
        return None
    # 虚拟“通用文件”仅按精确名或其后跟 "/" 识别，避免把用户根下
    # 名为“通用文件X”之类的普通条目误映射到 _common 区
    if rel == VIRTUAL_COMMON or rel.startswith(VIRTUAL_COMMON + "/"):
        base = common_dir()
        sub = rel[len(VIRTUAL_COMMON):].lstrip("/")
    else:
        try:
            base = user_home(user)
        except ValueError:
            return None
        sub = rel
    parts = sub.split("/") if sub else []
    if any(p == ".." for p in parts):
        return None
    if not sub:
        return os.path.abspath(base)
    norm = os.path.normpath(os.path.join(base, sub))
    if not _inside(norm, base):
        return None
    real = os.path.realpath(norm)
    if not _inside(real, os.path.realpath(base)):
        return None
    return real


def _scan_dir(path, rel_prefix=""):
    """扫描目录 -> (folders, files)。

    folders: [{"name","rel"}]；files: [{"name","size","rel"}]。
    仅统计普通目录/文件；符号链接一律跳过（防链接逃逸）。
    rel = rel_prefix + "/" + name（rel_prefix 为空则为 name）。
    """
    folders, files = [], []
    try:
        it = os.scandir(path)
    except OSError:
        return folders, files
    with it:
        for entry in it:
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    files.append((entry.name, entry.stat(follow_symlinks=False).st_size))
            except OSError:
                continue
    folders.sort()
    files.sort(key=lambda t: t[0])

    def rel_of(name):
        return name if not rel_prefix else rel_prefix + "/" + name

    return ([{"name": n, "rel": rel_of(n)} for n in folders],
            [{"name": n, "size": s, "rel": rel_of(n)} for n, s in files])


def list_dir(user: str, rel: str) -> dict:
    """列出目录内容。

    返回 {"ok","msg","rel","folders","files","common"}；
    根目录时 folders 附加虚拟条目 "通用文件"（rel 为 "通用文件"）；
    进入 "通用文件" 后显示 _common 内容（files 为空、common 为内容）。
    目录不存在 -> ok=False。
    """
    _require_setup()
    rel = rel if rel is not None else ""
    if not isinstance(rel, str) or "\x00" in rel or "\\" in rel or os.path.isabs(rel):
        return {"ok": False, "msg": "路径不合法", "rel": rel,
                "folders": [], "files": [], "common": []}

    def empty(msg):
        return {"ok": False, "msg": msg, "rel": rel,
                "folders": [], "files": [], "common": []}

    if rel == VIRTUAL_COMMON or rel.startswith(VIRTUAL_COMMON + "/"):
        path = resolve(user, rel)
        if path is None:
            return empty("路径不合法")
        if not os.path.isdir(path):
            return empty("目录不存在")
        # 通用文件区：全部内容归入 common（目录 size=0，便于继续导航）
        common = []
        try:
            it = os.scandir(path)
        except OSError:
            it = None
        if it is not None:
            with it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            common.append((entry.name, 0))
                        elif entry.is_file(follow_symlinks=False):
                            common.append((entry.name,
                                           entry.stat(follow_symlinks=False).st_size))
                    except OSError:
                        continue
            common.sort(key=lambda t: t[0])
        common = [{"name": n, "size": s, "rel": rel + "/" + n} for n, s in common]
        return {"ok": True, "msg": "ok", "rel": rel,
                "folders": [], "files": [], "common": common}

    path = resolve(user, rel)
    if path is None:
        return empty("路径不合法")
    if not os.path.isdir(path):
        return empty("目录不存在")
    folders, files = _scan_dir(path, rel_prefix=rel)
    if rel in ("", ".", None):
        folders.insert(0, {"name": VIRTUAL_COMMON, "rel": VIRTUAL_COMMON})
    return {"ok": True, "msg": "ok", "rel": rel,
            "folders": folders, "files": files, "common": []}


def quota_used(user: str) -> int:
    """递归统计用户目录内普通文件字节总和（os.walk，跳过符号链接）。"""
    _require_setup()
    try:
        home = user_home(user)
    except ValueError:
        return 0
    total = 0
    if not os.path.isdir(home):
        return 0
    for root, _dirs, files in os.walk(home, followlinks=False):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.islink(p):
                continue
            try:
                total += os.path.getsize(p)
            except OSError:
                continue
    return total


def create_folder(user: str, rel: str, name: str) -> (bool, str):
    """在 rel 目录下新建文件夹 name。返回 (True, "创建成功") 或 (False, 中文原因)。"""
    _require_setup()
    name = name or ""
    if (not name or "/" in name or "\\" in name
            or name in (".", "..")
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in name)):
        return False, "文件夹名不合法"
    if rel and (rel == VIRTUAL_COMMON or rel.startswith(VIRTUAL_COMMON + "/")):
        return False, "通用文件为只读"
    if rel in ("", ".", None) and (name == VIRTUAL_COMMON
                                   or name.startswith(VIRTUAL_COMMON)):
        return False, "该名称与内置“通用文件”目录冲突"
    parent = resolve(user, rel)
    if parent is None:
        return False, "路径不合法"
    if not os.path.isdir(parent):
        if rel in ("", ".", None):
            try:
                os.makedirs(parent, exist_ok=True)   # 用户根目录首次使用时自动创建
            except OSError as e:
                return False, "创建目录失败：%s" % e
        else:
            return False, "目录不存在"
    target = os.path.join(parent, name)
    if os.path.lexists(target):
        return False, "同名文件或文件夹已存在"
    try:
        os.makedirs(target)
    except OSError as e:
        return False, "创建失败：%s" % e
    return True, "创建成功"


# ---------- multipart 流式解析 ----------
def _extract_boundary(content_type):
    """从 Content-Type 取 boundary 字符串（无引号）；缺省返回 None。"""
    ct = content_type if isinstance(content_type, str) else ""
    for part in ct.split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.strip().lower() == "boundary":
            v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            if not v:
                return None
            try:
                return v.encode("latin-1")
            except UnicodeEncodeError:
                return None
    return None


def _split_params(value):
    """把 `k1=v1; k2="v2"` 拆成 {lower_k: bytes}；处理引号与转义。"""
    out = {}
    for part in value.split(b";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition(b"=")
        k = k.strip().lower()
        v = v.strip()
        if len(v) >= 2 and v[:1] == b'"' and v[-1:] == b'"':
            v = v[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        if k:
            out[k] = v
    return out


def _decode_rfc5987(val):
    """解码 RFC5987 参数值：charset'lang'percent-encoded；仅支持 UTF-8。"""
    try:
        s = val.decode("ascii")
    except UnicodeDecodeError:
        return None
    if "'" not in s:
        return None
    charset, _, rest = s.partition("'")
    _lang, _, value = rest.partition("'")
    if charset.strip().lower() != "utf-8":
        return None
    try:
        return urllib.parse.unquote(value)
    except Exception:
        return None


def _parse_disposition(headers):
    """从 part 头解析 Content-Disposition，返回 (name, filename)。

    filename 优先 filename*（RFC5987），否则取 filename（bytes）。
    均无返回 (None, None)。
    """
    name = None
    filename = None
    for line in headers.split(b"\r\n"):
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        if k.strip().lower() != b"content-disposition":
            continue
        params = _split_params(v.strip())
        name = params.get(b"name")
        filename = params.get(b"filename")
        fnstar = params.get(b"filename*")
        if fnstar is not None:
            dec = _decode_rfc5987(fnstar)
            if dec is not None:
                filename = dec.encode("utf-8")
        break
    return name, filename


def _decode_filename_bytes(raw):
    """兼容浏览器两种文件名编码：

    1) 字节本身就是合法 UTF-8（现代浏览器直接发送）→ 按 UTF-8 解；
    2) 原始 UTF-8 字节被 latin-1 层误读（header 为 latin-1 字节的场景）→
       先 latin-1 解出 str，再按 latin-1 编码回字节、按 UTF-8 重解；
       仍失败则保留 latin-1 解码结果（真实 latin-1 文件名）。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    s = raw.decode("latin-1")
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _clean_filename(raw):
    """把 header 中的 filename 字节规整为可安全落盘的文件名；不合法返回 None。

    规则：解码后取 os.path.basename；拒绝空名、"."、".."、以 "." 开头、
    控制字符与残留路径分隔符。
    """
    fn = _decode_filename_bytes(raw)
    fn = os.path.basename(fn)
    if not fn or fn in (".", ".."):
        return None
    if fn.startswith("."):
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in fn):
        return None
    if "/" in fn or "\\" in fn:
        return None
    return fn


def _parse_multipart(rfile, content_length, boundary, on_headers, on_body):
    """流式解析 multipart/form-data（替代已移除的 cgi 模块）。

    rfile：可读二进制流；content_length：请求体总字节数；boundary：边界字节。
    on_headers(headers_bytes) -> bool：返回 True 的 part 为活动 part，
       其 body 分块经 on_body(chunk) 回传（其余 part 的 body 直接跳过）。
    约定恰好消费 content_length 字节；格式非法抛 ValueError。
    注意：part body 结尾的 CRLF 属于分隔符终止序列，不包含在回传数据内。
    """
    delim = b"--" + boundary
    needle = b"\r\n" + delim          # 终止序列：\r\n--boundary
    keep = len(needle) - 1            # 跨块边界需保留的尾部字节数

    buf = b""
    remaining = content_length

    try:
        # ---- 1. 定位首个 --boundary（容忍 preamble）----
        while True:
            i = buf.find(delim)
            if i >= 0:
                buf = buf[i + len(delim):]
                break
            if remaining <= 0:
                return  # 没有 part
            if len(buf) > keep:
                buf = buf[-keep:]
            data = rfile.read(min(_CHUNK, remaining))
            if not data:
                remaining = 0
                continue
            buf += data
            remaining -= len(data)

        # ---- 2. 逐 part ----
        while True:
            # boundary 之后应为 \r\n（下一 part 头）或 --（结束）
            while len(buf) < 2 and remaining > 0:
                data = rfile.read(min(_CHUNK, remaining))
                if not data:
                    remaining = 0
                    break
                buf += data
                remaining -= len(data)
            if len(buf) < 2:
                raise ValueError("multipart: 请求体过早结束")
            if buf[:2] == b"--":
                buf = buf[2:]
                break
            if buf[:2] != b"\r\n":
                raise ValueError("multipart: boundary 后缺少 CRLF")
            buf = buf[2:]

            # ---- part 头（到 \r\n\r\n）----
            while True:
                j = buf.find(b"\r\n\r\n")
                if j >= 0:
                    headers = buf[:j]
                    buf = buf[j + 4:]
                    break
                if remaining <= 0:
                    raise ValueError("multipart: part 头未结束")
                if len(buf) > _MAX_HEADER:
                    raise ValueError("multipart: part 头过大")
                data = rfile.read(min(_CHUNK, remaining))
                if not data:
                    remaining = 0
                    continue
                buf += data
                remaining -= len(data)

            active = bool(on_headers(headers))

            # ---- part body（到 \r\n--boundary 为止）----
            while True:
                k = buf.find(needle)
                if k >= 0:
                    if active:
                        on_body(buf[:k])
                    buf = buf[k + len(needle):]
                    break
                if remaining <= 0:
                    raise ValueError("multipart: 缺少终止 boundary")
                if len(buf) > keep:
                    if active:
                        on_body(buf[:-keep])
                    buf = buf[-keep:]
                data = rfile.read(min(_CHUNK, remaining))
                if not data:
                    remaining = 0
                    continue
                buf += data
                remaining -= len(data)
    except _QuotaExceeded:
        raise  # 配额超限：立即中止，不再排空剩余请求体
    except Exception:
        _drain(rfile, remaining)  # 格式错误：排空剩余请求体，保持连接可用
        raise
    _drain(rfile, remaining)  # 排空 epilogue


# ---------- 上传/下载/删除 ----------
def _receive(rfile, content_length, boundary, target_dir, limit, user_root=False):
    """流式接收一个 multipart 请求并保存第一个文件 part。

    返回 (True, "上传成功") 或 (False, 中文原因)；任何失败路径都清理临时文件。
    user_root=True 表示目标为用户私有目录根（rel=""），此时拒绝与虚拟
    "通用文件" 目录同名的文件（该文件将无法通过 resolve 下载，造成死文件）。
    """
    tmp_dir = os.path.join(_CLOUD_ROOT, TMP_DIR_NAME)
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="up_", dir=tmp_dir)
    state = {"saved_name": None, "saw_file": False, "name_err": None}
    written = [0]
    try:
        with os.fdopen(fd, "wb") as out:
            def on_headers(headers):
                _name, fname = _parse_disposition(headers)
                if fname is None:
                    return False
                if state["saw_file"]:
                    return False  # 只取第一个文件 part
                state["saw_file"] = True
                fn = _clean_filename(fname)
                if fn is None:
                    state["name_err"] = "文件名不合法"
                    return False
                state["saved_name"] = fn
                return True

            def on_body(chunk):
                if written[0] + len(chunk) > limit:
                    raise _QuotaExceeded()
                written[0] += len(chunk)
                out.write(chunk)

            _parse_multipart(rfile, content_length, boundary, on_headers, on_body)

        if state["name_err"]:
            return False, state["name_err"]
        if state["saved_name"] is None:
            return False, "未收到文件"
        if user_root and (state["saved_name"] == VIRTUAL_COMMON
                          or state["saved_name"].startswith(VIRTUAL_COMMON)):
            return False, "文件名与内置“通用文件”目录冲突"
        target = os.path.join(target_dir, state["saved_name"])
        os.replace(tmp_path, target)
    except _QuotaExceeded:
        return False, "超出配额"
    except ValueError as e:
        return False, "上传失败：%s" % e
    except OSError as e:
        return False, "保存失败：%s" % e
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return True, "上传成功"


def save_upload(user: str, rel: str, rfile, content_length: int, content_type: str,
                quota_remaining: int) -> (bool, str):
    """保存用户个人网盘上传（multipart/form-data，流式解析）。

    目标目录 = resolve(user, rel)（须为目录）；最终 os.replace 到目标，覆盖同名文件。
    rel 在 "通用文件" 内 -> (False, "通用文件为只读，仅管理员可写")；
    无文件 part -> (False, "未收到文件")；累计字节超过 quota_remaining ->
    立即中止并清理临时文件，返回 (False, "超出配额")。
    """
    _require_setup()
    if rel and (rel == VIRTUAL_COMMON or rel.startswith(VIRTUAL_COMMON + "/")):
        return False, "通用文件为只读，仅管理员可写"
    target_dir = resolve(user, rel)
    if target_dir is None:
        return False, "目标路径不合法"
    if not os.path.isdir(target_dir):
        if rel in ("", ".", None):
            try:
                os.makedirs(target_dir, exist_ok=True)  # 用户根目录首次上传自动创建
            except OSError as e:
                return False, "创建目录失败：%s" % e
        else:
            return False, "目标目录不存在"
    boundary = _extract_boundary(content_type)
    if boundary is None:
        return False, "缺少 multipart boundary"
    if (isinstance(quota_remaining, bool) or not isinstance(quota_remaining, int)
            or quota_remaining < 0):
        return False, "配额参数不合法"
    return _receive(rfile, content_length, boundary, target_dir, quota_remaining,
                    user_root=rel in ("", ".", None))


def save_common_upload(rfile, content_length: int, content_type: str,
                       max_bytes: int) -> (bool, str):
    """保存通用文件上传（仅管理员可调用）。逻辑同 save_upload，目标为 _common/，
    限额用 max_bytes（主程序传硬上限）。"""
    _require_setup()
    boundary = _extract_boundary(content_type)
    if boundary is None:
        return False, "缺少 multipart boundary"
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 0):
        return False, "容量参数不合法"
    return _receive(rfile, content_length, boundary, common_dir(), max_bytes)


def common_list() -> list[dict]:
    """通用文件列表：[{"name","size","rel"}]，_common 根下普通文件，按名排序。"""
    _require_setup()
    _folders, files = _scan_dir(common_dir())
    return files


def delete_common(name: str) -> (bool, str):
    """删除 _common 根下的普通文件。

    仅允许删除 _common 根下的普通文件；拒绝目录/越界（含路径分隔符）/符号链接。
    """
    _require_setup()
    name = name or ""
    if (not name or "/" in name or "\\" in name
            or name in (".", "..") or "\x00" in name):
        return False, "文件名不合法"
    target = os.path.join(common_dir(), name)
    if os.path.islink(target):
        return False, "不支持删除符号链接"
    if not os.path.isfile(target):
        return False, "文件不存在"
    try:
        os.unlink(target)
    except OSError as e:
        return False, "删除失败：%s" % e
    return True, "删除成功"

# -*- coding: utf-8 -*-
"""
webdav.py —— 个人网盘的 WebDAV 端点（供 iscsi_broker.py 使用）。

为什么是 WebDAV：用户要在“我的电脑”/资源管理器里访问个人网盘（映射网络驱动器，
或直接用浏览器 / 任意 WebDAV 客户端读写），又不想装内核驱动（WinFsp/Dokan）。
Windows 自带 WebDAV 重定向器（WebClient 服务），
`net use Z: http://<服务器>:8080/dav/` 就能把网盘映射成盘符，所以服务器只要把
cloud_store 的个人网盘用 WebDAV 暴露出来即可。

设计要点：
1. 纯 Python 标准库；只被主程序调用，import cloud_store 复用它的路径安全与配额逻辑。
2. 认证不在这里做：调用方（Web 服务）先认证，再把“网盘用户名”和相对路径交给
   handle()。认证方式只有一个：网盘账号 + 密码（浏览器 / 任意 WebDAV 客户端）。
3. 只暴露用户自己的私有目录（cloud_store 里的“通用文件”虚拟区不暴露，避免只读区
   被 WebDAV 写入路径绕过）。
4. 实现 Windows WebDAV 重定向器实际会用到的方法：
   OPTIONS / PROPFIND / GET / HEAD / PUT / MKCOL / DELETE / MOVE / LOCK / UNLOCK。
   其中 LOCK 只做“签发锁令牌”的兼容处理（Windows 写文件前必先 LOCK），不做强制互斥。
5. 响应用 207 Multi-Status 的 D: 命名空间 XML；目录 href 带结尾斜杠，
   Windows 才能把它当文件夹。
"""

import datetime
import os
import tempfile
import threading
import time
import urllib.parse
import uuid

import cloud_store
import users_auth

DAV_NS = "DAV:"
MOUNT_PREFIX = "/dav/"
# OPTIONS 应答与 405 的 Allow 头共用（两处写串会让重定向器以为方法不支持）
ALLOW = "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, MKCOL, MOVE, LOCK, UNLOCK"
MAX_PUT_BYTES = 1 << 40          # 单次 PUT 上限（1 TiB，兜底防止恶意 Content-Length）
_XML_HEADER = '<?xml version="1.0" encoding="utf-8"?>\n'

# 锁表： (user, rel) -> {"token":..., "exp":...}（仅用于让 Windows 拿到锁令牌，不做强制互斥）
# 键必须带 user：不同用户的同名路径是不同文件，只用 rel 做键会让 A 用户拿到 B 用户的锁令牌。
_lock = threading.Lock()
_locks = {}
LOCK_TTL = 3600


class _Abort(Exception):
    """内部：带 HTTP 状态码的提前返回。"""

    def __init__(self, code, msg=""):
        Exception.__init__(self, msg or str(code))
        self.code = code
        self.msg = msg


# ---------- 基础工具 ----------
def _quote(rel, is_dir=False):
    """把相对路径转成 href（逐段百分号编码，目录带结尾斜杠）。"""
    href = MOUNT_PREFIX + urllib.parse.quote(rel, safe="/")
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


COMMON = cloud_store.VIRTUAL_COMMON          # “通用文件”虚拟目录（登录用户在根目录能看到，只读）


def _is_common(rel):
    """路径是否落在只读的“通用文件”区。"""
    return rel == COMMON or rel.startswith(COMMON + "/")


def _resolve(user, rel):
    """请求路径 -> 磁盘路径。"""
    return cloud_store.resolve(user, rel)


def _http_date(ts):
    """RFC1123/GMT 时间（HTTP 头与 WebDAV getlastmodified 都用这个）。"""
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _iso_date(ts):
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ctype(name):
    """按扩展名给个 Content-Type（够 Windows 识别即可）。"""
    ext = os.path.splitext(name)[1].lower()
    return {".txt": "text/plain", ".log": "text/plain", ".html": "text/html",
            ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
            ".json": "application/json", ".xml": "application/xml",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
            ".pdf": "application/pdf", ".zip": "application/zip",
            ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".ppt": "application/vnd.ms-powerpoint",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".exe": "application/octet-stream",
            ".iso": "application/octet-stream"}.get(ext, "application/octet-stream")


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _resp(handler, code, body=b"", ctype=None, extra=None):
    """统一响应写出（响应体可为 bytes 或 str）。"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    handler.send_response(code)
    if ctype:
        handler.send_header("Content-Type", ctype)
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body and handler.command != "HEAD":
        handler.wfile.write(body)


def _send_dav_error(handler, code, msg=""):
    body = _XML_HEADER + '<D:error xmlns:D="DAV:"><D:responsedescription>%s</D:responsedescription></D:error>' \
        % _xml_escape(msg or ("HTTP %d" % code))
    _resp(handler, code, body, "application/xml; charset=utf-8")


# ---------- 属性（PROPFIND 用） ----------
def _prop_xml(rel, path, is_dir):
    """一个 <D:response> 内的属性集合。"""
    try:
        st = os.stat(path)
        mtime, size = st.st_mtime, (0 if is_dir else st.st_size)
    except OSError:
        mtime, size = 0, 0
    name = rel.rsplit("/", 1)[-1] if rel else ""
    props = [
        '<D:displayname>%s</D:displayname>' % _xml_escape(name),
        '<D:getlastmodified>%s</D:getlastmodified>' % _http_date(mtime),
        '<D:creationdate>%s</D:creationdate>' % _iso_date(mtime),
    ]
    if is_dir:
        props.append('<D:resourcetype><D:collection/></D:resourcetype>')
        props.append('<D:getcontentlength>0</D:getcontentlength>')
        props.append('<D:getcontenttype>httpd/unix-directory</D:getcontenttype>')
    else:
        props.append('<D:resourcetype/>')
        props.append('<D:getcontentlength>%d</D:getcontentlength>' % size)
        props.append('<D:getcontenttype>%s</D:getcontenttype>' % _ctype(name))
        props.append('<D:getetag>"%d-%d"' % (int(mtime), size) + '</D:getetag>')
    props.append('<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>'
                 '<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>')
    return ('<D:response><D:href>%s</D:href><D:propstat><D:prop>%s</D:prop>'
            '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
            % (_quote(rel, is_dir), "".join(props)))


def _list_children(user, rel):
    """列出目录下的直接子项（跳过符号链接），返回 [(rel, path, is_dir)]。

    登录用户在根目录会额外看到只读的“通用文件”目录（与网页版网盘一致）。
    """
    out = []
    path = _resolve(user, rel)
    if not path or not os.path.isdir(path):
        return out
    try:
        it = os.scandir(path)
    except OSError:
        return out
    with it:
        for entry in it:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue          # 跳过符号链接等特殊项
            except OSError:
                continue
            crel = entry.name if not rel else rel + "/" + entry.name
            out.append((crel, entry.path, is_dir))
    # 登录用户（rel 为空）的根目录里塞一个只读“通用文件”
    if rel == "":
        cpath = cloud_store.resolve(user, COMMON)
        if cpath and os.path.isdir(cpath):
            out.insert(0, (COMMON, cpath, True))
    out.sort(key=lambda t: (not t[2], t[0]))
    return out


# ---------- 各方法 ----------
def _do_options(handler):
    _resp(handler, 200, b"", None, {
        "Allow": ALLOW,
        "DAV": "1, 2",
        "MS-Author-Via": "DAV",
    })


def _do_propfind(handler, user, rel):
    depth = (handler.headers.get("Depth") or "1").strip().lower()
    path = _resolve(user, rel)
    if path is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if not os.path.exists(path):
        _send_dav_error(handler, 404, "不存在")
        return
    is_dir = os.path.isdir(path)
    items = [_prop_xml(rel, path, is_dir)]
    if is_dir and depth != "0":          # Depth: infinity 也按 1 层处理（够 Windows 用）
        for crel, cpath, cdir in _list_children(user, rel):
            items.append(_prop_xml(crel, cpath, cdir))
    body = (_XML_HEADER + '<D:multistatus xmlns:D="DAV:">' + "".join(items) + '</D:multistatus>')
    _resp(handler, 207, body, "application/xml; charset=utf-8")


def _open_range(handler, path, size):
    """按 Range 头做单段读取（Windows 大文件会用 Range）。返回 (start, end)。"""
    rng = handler.headers.get("Range") or ""
    if not rng.startswith("bytes="):
        return 0, size - 1
    spec = rng[6:].split(",")[0].strip()
    a, _, b = spec.partition("-")
    try:
        if a == "":                       # bytes=-N：最后 N 字节
            n = int(b)
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        raise _Abort(416)
    if start >= size or start > end:
        raise _Abort(416)
    return start, min(end, size - 1)


def _do_get(handler, user, rel, head=False):
    path = _resolve(user, rel)
    if path is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if os.path.isdir(path):
        if not (handler.path.split("?", 1)[0].endswith("/")):
            _resp(handler, 301, b"", None, {"Location": _quote(rel, True)})
            return
        rows = "".join('<li><a href="%s">%s</a></li>' % (_quote(cr, cd), _xml_escape(os.path.basename(cr)))
                       for cr, _p, cd in _list_children(user, rel))
        _resp(handler, 200, "<html><body><ul>%s</ul></body></html>" % rows,
              "text/html; charset=utf-8")
        return
    if not os.path.isfile(path):
        _send_dav_error(handler, 404, "文件不存在")
        return
    size = os.path.getsize(path)
    try:
        start, end = _open_range(handler, path, size)
    except _Abort as e:
        _resp(handler, e.code, b"", None, {"Content-Range": "bytes */%d" % size})
        return
    extra = {"Accept-Ranges": "bytes", "Last-Modified": _http_date(os.path.getmtime(path))}
    code = 200
    if handler.headers.get("Range"):
        code = 206
        extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    if head:
        _resp(handler, code, b"", _ctype(os.path.basename(rel)), extra)
        return
    handler.send_response(code)
    handler.send_header("Content-Type", _ctype(os.path.basename(rel)))
    handler.send_header("Content-Length", str(end - start + 1))
    for k, v in extra.items():
        handler.send_header(k, v)
    handler.end_headers()
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(256 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def _do_put(handler, user, rel):
    if _is_common(rel):
        _send_dav_error(handler, 403, "通用文件为只读，不能写入")
        return
    if not rel:
        _send_dav_error(handler, 405, "不能写入挂载根目录")
        return
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    parent = _resolve(user, parent_rel)
    target = _resolve(user, rel)
    if parent is None or target is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if not os.path.isdir(parent):
        _send_dav_error(handler, 409, "上级目录不存在")
        return
    if os.path.isdir(target):
        _send_dav_error(handler, 405, "目标是目录")
        return
    # Content-Length 必须区分“头缺失”和“值为 0”：
    # Windows 自带的 WebDAV 重定向器（WebClient）在资源管理器里新建/复制文件时，
    # 第一步固定是先发一个 Content-Length: 0 的 PUT 建 0 字节空文件，之后才 LOCK +
    # PUT 写真实数据。老代码把 <=0 一律当成“缺少 Content-Length”回 411，
    # 结果资源管理器往 Z 盘新建文件、复制文件、上传空文件全部失败。
    raw_len = handler.headers.get("Content-Length")
    if raw_len is None:
        _send_dav_error(handler, 411, "缺少 Content-Length")
        return
    try:
        length = int(str(raw_len).strip())
    except (TypeError, ValueError):
        _send_dav_error(handler, 400, "Content-Length 不合法")
        return
    if length < 0:
        _send_dav_error(handler, 400, "Content-Length 不合法")
        return
    if length > MAX_PUT_BYTES:
        _send_dav_error(handler, 413, "文件过大")
        return
    # 配额：已用 + 本次（覆盖写时先扣除旧文件大小）
    quota = users_auth.quota_of(user)
    if quota > 0:
        old = os.path.getsize(target) if os.path.isfile(target) else 0
        if cloud_store.quota_used(user) - old + length > quota:
            _send_dav_error(handler, 507, "配额不足")
            return
    existed = os.path.isfile(target)
    # 临时文件必须是唯一名：老代码用固定名 target + ".davtmp"，并发写同一路径会互相
    # 覆盖，失败残留在用户目录里还会被列表/配额统计到。tempfile.mkstemp 在目标目录内
    # 建唯一文件（与 target 同目录，保证 os.replace 是同一文件系统内的原子改名）。
    tmp = None
    remaining = length
    try:
        fd, tmp = tempfile.mkstemp(prefix=".davtmp-", suffix=".part",
                                   dir=os.path.dirname(target) or ".")
        with os.fdopen(fd, "wb") as f:
            while remaining > 0:
                chunk = handler.rfile.read(min(256 * 1024, remaining))
                if not chunk:
                    raise _Abort(400, "请求体提前结束")
                f.write(chunk)
                remaining -= len(chunk)
        os.replace(tmp, target)
        tmp = None                      # 已经改名成功，不需要清理
    except _Abort as e:
        _cleanup(tmp)
        _send_dav_error(handler, e.code, e.msg)
        return
    except OSError as e:
        _cleanup(tmp)
        _send_dav_error(handler, 500, "写入失败：%s" % e)
        return
    _resp(handler, 204 if existed else 201, b"")


def _cleanup(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _do_mkcol(handler, user, rel):
    if _is_common(rel):
        _send_dav_error(handler, 403, "通用文件为只读，不能新建文件夹")
        return
    if not rel:
        _send_dav_error(handler, 405, "挂载根目录已存在")
        return
    name = rel.rsplit("/", 1)[-1]
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    target = _resolve(user, rel)
    if target is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if os.path.exists(target):
        _send_dav_error(handler, 405, "已存在")
        return
    ok, msg = cloud_store.create_folder(user, parent_rel, name)
    if not ok:
        _send_dav_error(handler, 409, msg)
        return
    _resp(handler, 201, b"")


def _do_delete(handler, user, rel):
    if _is_common(rel):
        _send_dav_error(handler, 403, "通用文件为只读，不能删除")
        return
    if not rel:
        _send_dav_error(handler, 405, "不能删除挂载根目录")
        return
    path = _resolve(user, rel)
    if path is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if os.path.isdir(path):
        ok, msg = cloud_store.delete_folder(user, rel)
    elif os.path.isfile(path):
        ok, msg = cloud_store.delete_file(user, rel)
    else:
        _send_dav_error(handler, 404, "不存在")
        return
    if not ok:
        _send_dav_error(handler, 403, msg)
        return
    _resp(handler, 204, b"")


def _dest_rel(handler):
    """从 Destination 头解析出目标相对路径（只接受挂载点下的路径）。"""
    dest = handler.headers.get("Destination") or ""
    if not dest:
        return None
    parsed = urllib.parse.urlparse(dest)
    path = urllib.parse.unquote(parsed.path or "")
    if not path.startswith(MOUNT_PREFIX):
        return None
    rel = path[len(MOUNT_PREFIX):]
    if ".." in rel.replace("\\", "/").split("/"):
        return None
    return rel.rstrip("/")


def _do_move(handler, user, rel):
    if not rel:
        _send_dav_error(handler, 405, "不能移动挂载根目录")
        return
    dst_rel = _dest_rel(handler)
    if not dst_rel:
        _send_dav_error(handler, 400, "Destination 头不合法")
        return
    if _is_common(rel) or _is_common(dst_rel):
        _send_dav_error(handler, 403, "通用文件为只读，不能改名/移动")
        return
    src = _resolve(user, rel)
    dst = _resolve(user, dst_rel)
    if src is None or dst is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    if not os.path.exists(src):
        _send_dav_error(handler, 404, "源不存在")
        return
    overwrite = (handler.headers.get("Overwrite") or "T").upper() != "F"
    if os.path.exists(dst):
        if not overwrite:
            _send_dav_error(handler, 412, "目标已存在")
            return
        if os.path.isdir(dst) and not os.path.islink(dst):
            ok, msg = cloud_store.delete_folder(user, dst_rel)
        else:
            ok, msg = cloud_store.delete_file(user, dst_rel)
        if not ok:
            _send_dav_error(handler, 403, msg)
            return
    if not os.path.isdir(os.path.dirname(dst)):
        _send_dav_error(handler, 409, "目标上级目录不存在")
        return
    try:
        os.rename(src, dst)
    except OSError as e:
        _send_dav_error(handler, 500, "移动失败：%s" % e)
        return
    _resp(handler, 201, b"")


def _do_lock(handler, user, rel):
    path = _resolve(user, rel)
    if path is None:
        _send_dav_error(handler, 403, "路径不合法")
        return
    token = "opaquelocktoken:" + str(uuid.uuid4())
    key = (user, rel)                             # 不同用户同名路径必须隔离
    with _lock:
        now = time.time()
        if len(_locks) > 4096:                    # 顺手清掉过期锁，避免长期运行后无限增长
            for k in [k for k, v in _locks.items() if v["exp"] < now]:
                _locks.pop(k, None)
        old = _locks.get(key)
        if old and handler.headers.get("If"):
            token = old["token"]                 # 刷新已有锁
        _locks[key] = {"token": token, "exp": now + LOCK_TTL}
    body = (_XML_HEADER + '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
            '<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>'
            '<D:depth>infinity</D:depth><D:timeout>Second-%d</D:timeout>'
            '<D:locktoken><D:href>%s</D:href></D:locktoken>'
            '</D:activelock></D:lockdiscovery></D:prop>' % (LOCK_TTL, _xml_escape(token)))
    _resp(handler, 200, body, "application/xml; charset=utf-8", {"Lock-Token": "<%s>" % token})


def _do_unlock(handler, user, rel):
    with _lock:
        _locks.pop((user, rel), None)
    _resp(handler, 204, b"")


# ---------- 入口 ----------
def handle(handler, user, rel):
    """处理一次 WebDAV 请求。

    handler ：BaseHTTPRequestHandler（方法在 handler.command，正文在 handler.rfile）
    user    ：已认证的网盘用户名（调用方负责认证）
    rel     ：挂载根（/dav/）下的相对路径，已 URL 解码、以 "/" 分隔、不含前后斜杠
    """
    if rel and (".." in rel.split("/") or "\\" in rel or rel.startswith("/")):
        _send_dav_error(handler, 403, "路径不合法")
        return
    method = handler.command
    try:
        if method == "OPTIONS":
            _do_options(handler)
        elif method == "PROPFIND":
            _do_propfind(handler, user, rel)
        elif method in ("GET", "HEAD"):
            _do_get(handler, user, rel, head=(method == "HEAD"))
        elif method == "PUT":
            _do_put(handler, user, rel)
        elif method == "MKCOL":
            _do_mkcol(handler, user, rel)
        elif method == "DELETE":
            _do_delete(handler, user, rel)
        elif method == "MOVE":
            _do_move(handler, user, rel)
        elif method == "LOCK":
            _do_lock(handler, user, rel)
        elif method == "UNLOCK":
            _do_unlock(handler, user, rel)
        else:
            _resp(handler, 405, b"", None, {"Allow": ALLOW})
    except _Abort as e:
        try:
            _send_dav_error(handler, e.code, e.msg)
        except Exception:
            pass
    except Exception as e:                       # WebDAV 出错也不能把连接线程打挂
        try:
            _send_dav_error(handler, 500, "服务器内部错误：%s" % e)
        except Exception:
            pass

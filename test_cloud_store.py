# -*- coding: utf-8 -*-
"""cloud_store.py 单元测试（临时目录内运行，结束后清理）。"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_store as cs

_BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(_BASE, ".cloud_test_root")
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT)
CLOUD = os.path.join(ROOT, "cloud")
PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok  %s" % name)
    else:
        FAIL.append(name)
        print("FAIL  %s  %s" % (name, detail))


def make_body(fields, boundary=b"----TestBoundary123"):
    """构造 multipart body。fields: [(name, filename_or_None, body_bytes)]"""
    out = bytearray()
    for name, fname, body in fields:
        out += b"--" + boundary + b"\r\n"
        out += b'Content-Disposition: form-data; name="%s"' % name.encode()
        if fname is not None:
            out += b'; filename="%s"' % fname
        out += b"\r\nContent-Type: application/octet-stream\r\n\r\n"
        out += body
        out += b"\r\n"
    out += b"--" + boundary + b"--\r\n"
    return bytes(out)


def upload(user, rel, fields, quota, ct_override=None, content_length_override=None):
    body = make_body(fields)
    ct = ct_override or "multipart/form-data; boundary=----TestBoundary123"
    cl = content_length_override if content_length_override is not None else len(body)
    return cs.save_upload(user, rel, io.BytesIO(body), cl, ct, quota)


def common_upload(fields, max_bytes, ct_override=None):
    body = make_body(fields)
    ct = ct_override or "multipart/form-data; boundary=----TestBoundary123"
    return cs.save_common_upload(io.BytesIO(body), len(body), ct, max_bytes)


# ---------- setup ----------
cs.setup(CLOUD)
check("setup 创建目录", os.path.isdir(CLOUD) and os.path.isdir(cs.common_dir())
      and os.path.isdir(os.path.join(CLOUD, ".tmp")))
check("user_home", cs.user_home("alice") == os.path.join(CLOUD, "alice"))
check("common_dir", cs.common_dir() == os.path.join(CLOUD, "_common"))

# ---------- resolve 安全 ----------
base = os.path.join(CLOUD, "alice")
os.makedirs(base, exist_ok=True)
check("resolve 根", cs.resolve("alice", "") == base)
check("resolve 子目录", cs.resolve("alice", "docs") == os.path.join(base, "docs"))
check("resolve 拒绝 ..", cs.resolve("alice", "../bob") is None)
check("resolve 拒绝 a/../b", cs.resolve("alice", "a/../b") is None)
check("resolve 拒绝绝对路径", cs.resolve("alice", "/etc/passwd") is None)
check("resolve 拒绝反斜杠", cs.resolve("alice", "a\\..\\..") is None)
check("resolve 拒绝空字节", cs.resolve("alice", "a\x00b") is None)
check("resolve 拒绝空段..", cs.resolve("alice", "..") is None)
check("resolve 通用文件", cs.resolve("alice", "通用文件") == cs.common_dir())
check("resolve 通用文件/子", cs.resolve("alice", "通用文件/sub")
      == os.path.join(cs.common_dir(), "sub"))
check("resolve 通用文件 逃逸", cs.resolve("alice", "通用文件/../..") is None)
check("resolve 通用文件/..", cs.resolve("alice", "通用文件/..") is None)

# ---------- list_dir ----------
os.makedirs(os.path.join(base, "docs"), exist_ok=True)
with open(os.path.join(base, "a.txt"), "wb") as f:
    f.write(b"hello")
r = cs.list_dir("alice", "")
check("list_dir 根 ok", r["ok"] is True and r["msg"] == "ok")
check("list_dir 根虚拟目录", any(f["name"] == "通用文件" and f["rel"] == "通用文件"
                                for f in r["folders"]))
check("list_dir 根文件", any(f["name"] == "a.txt" and f["size"] == 5 and f["rel"] == "a.txt"
                            for f in r["files"]))
check("list_dir 根文件夹", any(f["name"] == "docs" and f["rel"] == "docs"
                             for f in r["folders"]))
r = cs.list_dir("alice", "docs")
check("list_dir 子目录", r["ok"] is True and r["folders"] == [] and r["files"] == [])
r = cs.list_dir("alice", "不存在")
check("list_dir 不存在 -> ok=False", r["ok"] is False and r["msg"] == "目录不存在")
r = cs.list_dir("nobody", "")
check("list_dir 无主页 -> ok=False", r["ok"] is False)

# ---------- create_folder ----------
ok, msg = cs.create_folder("alice", "", "newdir")
check("create_folder 成功", ok and msg == "创建成功"
      and os.path.isdir(os.path.join(base, "newdir")))
ok, msg = cs.create_folder("alice", "", "newdir")
check("create_folder 重名", not ok)
ok, msg = cs.create_folder("alice", "", "../evil")
check("create_folder 拒绝 ..", not ok)
ok, msg = cs.create_folder("alice", "", "a\\b")
check("create_folder 拒绝反斜杠", not ok)
ok, msg = cs.create_folder("alice", "", "a/b")
check("create_folder 拒绝斜杠", not ok)
ok, msg = cs.create_folder("alice", "", ".")
check("create_folder 拒绝 .", not ok)
ok, msg = cs.create_folder("alice", "", "..")
check("create_folder 拒绝 ..2", not ok)
ok, msg = cs.create_folder("alice", "", "bad\x01name")
check("create_folder 拒绝控制字符", not ok)
ok, msg = cs.create_folder("alice", "", "通用文件")
check("create_folder 拒绝通用文件同名", not ok and "冲突" in msg)
ok, msg = cs.create_folder("alice", "通用文件", "x")
check("create_folder 通用文件只读", not ok and msg == "通用文件为只读")
ok, msg = cs.create_folder("alice", "newdir", "sub")
check("create_folder 子目录成功", ok and os.path.isdir(os.path.join(base, "newdir", "sub")))
ok, msg = cs.create_folder("nobody", "", "x")
check("create_folder 自动建主页", ok and os.path.isdir(cs.user_home("nobody")))

# ---------- 上传基本 ----------
ok, msg = upload("alice", "", [("file", b"hello.txt", b"HELLO-CONTENT")], 10**9)
check("上传成功", ok and msg == "上传成功")
p = os.path.join(base, "hello.txt")
check("上传文件存在且内容一致", os.path.isfile(p)
      and open(p, "rb").read() == b"HELLO-CONTENT")

# 覆盖同名
ok, msg = upload("alice", "", [("file", b"hello.txt", b"NEW")], 10**9)
check("覆盖同名", ok and open(p, "rb").read() == b"NEW")

# 上传到子目录
os.makedirs(os.path.join(base, "docs"), exist_ok=True)
ok, msg = upload("alice", "docs", [("file", b"d.txt", b"DDD")], 10**9)
check("上传到子目录", ok and open(os.path.join(base, "docs", "d.txt"), "rb").read() == b"DDD")

# 二进制内容含 \r\n-- 前缀的假边界与尾部 CRLF
tricky = b"abc\r\n--x\r\n--y\r\n" + b"tail\r\n"
ok, msg = upload("alice", "", [("file", b"tricky.bin", tricky)], 10**9)
check("二进制内容完整", ok and open(os.path.join(base, "tricky.bin"), "rb").read() == tricky)

# body 以 CRLF 结尾：该 CRLF 属于 part 内容（保留）；紧邻 --boundary 的
# 分隔符 CRLF 属于终止序列（不得写入）
ok, msg = upload("alice", "", [("file", b"crlf.txt", b"DATA\r\n")], 10**9)
got = open(os.path.join(base, "crlf.txt"), "rb").read()
check("分隔符 CRLF 剔除", ok and got == b"DATA\r\n" and got != b"DATA\r\n\r\n")

# 多 part：第一个是普通字段，第二个是文件
ok, msg = upload("alice", "", [("field1", None, b"junk"),
                               ("file", b"multi.txt", b"MULTI")], 10**9)
check("多 part 取文件", ok and open(os.path.join(base, "multi.txt"), "rb").read() == b"MULTI")

# 无文件 part
ok, msg = upload("alice", "", [("field1", None, b"junk")], 10**9)
check("无文件 part", not ok and msg == "未收到文件")

# 通用文件只读
ok, msg = upload("alice", "通用文件", [("file", b"x.txt", b"X")], 10**9)
check("用户写通用文件被拒", not ok and msg == "通用文件为只读，仅管理员可写")

# 目标目录不存在
ok, msg = upload("alice", "nodir", [("file", b"x.txt", b"X")], 10**9)
check("目标目录不存在", not ok and msg == "目标目录不存在")

# 缺少 boundary
ok, msg = upload("alice", "", [("file", b"x.txt", b"X")], 10**9,
                 ct_override="multipart/form-data")
check("缺少 boundary", not ok and msg == "缺少 multipart boundary")

# ---------- 配额 ----------
ok, msg = upload("alice", "", [("file", b"big.txt", b"12345")], 4)
check("配额超限拒绝", not ok and msg == "超出配额")
check("配额超限临时文件清理",
      not any(f.startswith("up_") for f in os.listdir(os.path.join(CLOUD, ".tmp"))))
check("配额超限目标未落盘", not os.path.exists(os.path.join(base, "big.txt")))
ok, msg = upload("alice", "", [("file", b"big.txt", b"12345")], 5)
check("配额恰好等于", ok and open(os.path.join(base, "big.txt"), "rb").read() == b"12345")

# ---------- 中文文件名 ----------
# 1) filename* RFC5987
body = (b"--B7\r\nContent-Disposition: form-data; name=\"file\"; "
        b"filename*=UTF-8''%E4%B8%AD%E6%96%87%20%E6%96%87%E4%BB%B6.txt\r\n"
        b"Content-Type: text/plain\r\n\r\n" + "中文内容".encode() + b"\r\n--B7--\r\n")
ok, msg = cs.save_upload("alice", "", io.BytesIO(body), len(body),
                         "multipart/form-data; boundary=B7", 10**9)
check("filename* 解码", ok and os.path.exists(os.path.join(base, "中文 文件.txt"))
      and open(os.path.join(base, "中文 文件.txt"), "rb").read() == "中文内容".encode())
# 2) 原始 UTF-8 字节
raw_utf8 = "原始中文.txt".encode("utf-8")
body = (b"--B8\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" + raw_utf8
        + b"\"\r\n\r\nABC\r\n--B8--\r\n")
ok, msg = cs.save_upload("alice", "", io.BytesIO(body), len(body),
                         "multipart/form-data; boundary=B8", 10**9)
check("原始 UTF-8 文件名", ok and os.path.exists(os.path.join(base, "原始中文.txt"))
      and open(os.path.join(base, "原始中文.txt"), "rb").read() == b"ABC")
# 3) latin-1 真实文件名（如 café）
body = (b"--B9\r\nContent-Disposition: form-data; name=\"file\"; filename=\"caf\xe9.txt\"\r\n"
        b"\r\nLATIN\r\n--B9--\r\n")
ok, msg = cs.save_upload("alice", "", io.BytesIO(body), len(body),
                         "multipart/form-data; boundary=B9", 10**9)
check("latin-1 文件名", ok and os.path.exists(os.path.join(base, "caf\u00e9.txt"))
      and open(os.path.join(base, "caf\u00e9.txt"), "rb").read() == b"LATIN")
# 4) 路径型文件名被 basename 收编
ok, msg = upload("alice", "", [("file", b"../../evil.txt", b"X")], 10**9)
check("filename 穿越被收编", ok and os.path.exists(os.path.join(base, "evil.txt"))
      and not os.path.exists(os.path.join(ROOT, "evil.txt")))
# 5) 非法名：空/点开头/控制字符
ok, msg = upload("alice", "", [("file", b"", b"X")], 10**9)
check("空文件名拒绝", not ok)
ok, msg = upload("alice", "", [("file", b".hidden", b"X")], 10**9)
check("点开头拒绝", not ok)
ok, msg = upload("alice", "", [("file", b"bad\x01.txt", b"X")], 10**9)
check("控制字符拒绝", not ok)

# ---------- 符号链接 ----------
try:
    os.symlink(ROOT, os.path.join(base, "link_out"))
    symlink_ok = True
except OSError:
    symlink_ok = False
    print("  (跳过符号链接测试：无权限)")
if symlink_ok:
    check("resolve 符号链接逃逸", cs.resolve("alice", "link_out") is None)
    r = cs.list_dir("alice", "")
    check("list_dir 跳过符号链接",
          all(f["name"] != "link_out" for f in r["folders"] + r["files"]))
    os.unlink(os.path.join(base, "link_out"))

# ---------- 用户删除：delete_file / delete_folder ----------
ok, msg = upload("alice", "", [("file", b"del_a.txt", b"AAA")], 10**9)
check("删除测试准备上传", ok)
ok, msg = upload("alice", "docs", [("file", b"del_sub.txt", b"BBB")], 10**9)
check("删除测试准备上传子目录", ok)
ok, msg = cs.create_folder("alice", "", "del_dir")
check("删除测试准备建目录", ok)
ok, msg = upload("alice", "del_dir", [("file", b"inner.bin", b"IIIIII")], 10**9)
check("删除测试准备上传目录内", ok)

ok, msg = cs.delete_file("alice", "del_a.txt")
check("delete_file 成功", ok and msg == "删除成功"
      and not os.path.exists(os.path.join(base, "del_a.txt")))
ok, msg = cs.delete_file("alice", "del_a.txt")
check("delete_file 不存在", not ok and msg == "文件不存在")
ok, msg = cs.delete_file("alice", "docs/del_sub.txt")
check("delete_file 子目录文件", ok
      and not os.path.exists(os.path.join(base, "docs", "del_sub.txt")))
ok, msg = cs.delete_file("alice", "docs")
check("delete_file 目录被拒", not ok and msg == "文件不存在")
ok, msg = cs.delete_file("alice", "")
check("delete_file 根被拒", not ok and msg == "路径不合法")
ok, msg = cs.delete_file("alice", "通用文件")
check("delete_file 通用文件只读", not ok and msg == "通用文件为只读")
ok, msg = cs.delete_file("alice", "通用文件/x.txt")
check("delete_file 通用文件只读2", not ok and msg == "通用文件为只读")
ok, msg = cs.delete_file("alice", "../bob/x")
check("delete_file 拒绝 ..", not ok and msg == "路径不合法")
ok, msg = cs.delete_file("alice", "a/../b")
check("delete_file 拒绝 a/../b", not ok and msg == "路径不合法")
ok, msg = cs.delete_file("alice", "/etc/passwd")
check("delete_file 拒绝绝对路径", not ok and msg == "路径不合法")
ok, msg = cs.delete_file("alice", "a\\..\\x")
check("delete_file 拒绝反斜杠", not ok and msg == "路径不合法")
ok, msg = cs.delete_file("alice", "bad\x00name")
check("delete_file 拒绝空字节", not ok and msg == "路径不合法")

ok, msg = cs.delete_folder("alice", "del_dir")
check("delete_folder 递归成功", ok and msg == "删除成功"
      and not os.path.exists(os.path.join(base, "del_dir")))
ok, msg = cs.delete_folder("alice", "del_dir")
check("delete_folder 不存在", not ok and msg == "文件夹不存在")
ok, msg = upload("alice", "", [("file", b"del_b.txt", b"B")], 10**9)
ok, msg = cs.delete_folder("alice", "del_b.txt")
check("delete_folder 文件被拒", not ok and msg == "文件夹不存在")
ok, msg = cs.delete_file("alice", "del_b.txt")
check("delete_folder 文件被拒后清理", ok)
ok, msg = cs.delete_folder("alice", "")
check("delete_folder 根被拒", not ok and msg == "路径不合法")
ok, msg = cs.delete_folder("alice", ".")
check("delete_folder 点被拒", not ok and msg == "路径不合法")
ok, msg = cs.delete_folder("alice", "通用文件")
check("delete_folder 通用文件只读", not ok and msg == "通用文件为只读")
ok, msg = cs.delete_folder("alice", "../bob")
check("delete_folder 拒绝 ..", not ok and msg == "路径不合法")
ok, msg = cs.delete_folder("alice", "/tmp")
check("delete_folder 拒绝绝对路径", not ok and msg == "路径不合法")
ok, msg = cs.delete_folder("alice", "a\\b")
check("delete_folder 拒绝反斜杠", not ok and msg == "路径不合法")

# 删除不影响其他用户 / 通用文件区
ok, msg = upload("alice", "docs", [("file", b"del_sub.txt", b"BBB")], 10**9)
check("跨用户测试准备重新上传", ok)
ok, msg = cs.delete_file("bob", "docs/del_sub.txt")
check("delete_file 他人文件不可删", not ok)
check("他人文件仍在", os.path.exists(os.path.join(base, "docs", "del_sub.txt")))

# 删除后配额回落
q0 = cs.quota_used("alice")
with open(os.path.join(base, "docs", "q.bin"), "wb") as f:
    f.write(b"0123456789")
ok, msg = cs.delete_file("alice", "docs/q.bin")
check("删除后配额回落", ok and cs.quota_used("alice") == q0)

if symlink_ok:
    # 1) 目标本身是符号链接：拒绝删除
    os.symlink(ROOT, os.path.join(base, "link_out"))
    ok, msg = cs.delete_file("alice", "link_out")
    check("delete_file 拒绝符号链接", not ok and msg == "不支持删除符号链接")
    ok, msg = cs.delete_folder("alice", "link_out")
    check("delete_folder 拒绝符号链接", not ok and msg == "不支持删除符号链接")
    os.unlink(os.path.join(base, "link_out"))
    # 2) 中间成分符号链接指向目录外：realpath 二次校验拒绝，目标不被删除
    os.makedirs(os.path.join(ROOT, "outside"), exist_ok=True)
    outside_f = os.path.join(ROOT, "outside", "secret.txt")
    with open(outside_f, "wb") as f:
        f.write(b"SECRET")
    os.symlink(os.path.join(ROOT, "outside"), os.path.join(base, "sub_link"))
    ok, msg = cs.delete_file("alice", "sub_link/secret.txt")
    check("delete_file 中间链接逃逸拒绝", not ok and msg == "路径不合法")
    check("delete_file 逃逸目标未删", os.path.exists(outside_f))
    ok, msg = cs.delete_folder("alice", "sub_link")
    check("delete_folder 中间链接逃逸拒绝", not ok and msg == "不支持删除符号链接")
    check("delete_folder 逃逸目标未删", os.path.exists(os.path.join(ROOT, "outside")))
    os.unlink(os.path.join(base, "sub_link"))
    # 3) 目录内嵌套符号链接：递归删除只删链接本身，不进入其目标
    os.makedirs(os.path.join(base, "nested"), exist_ok=True)
    os.symlink(os.path.join(ROOT, "outside"), os.path.join(base, "nested", "sl"))
    ok, msg = cs.delete_folder("alice", "nested")
    check("delete_folder 嵌套链接安全删除", ok)
    check("delete_folder 链接目标未删", os.path.exists(os.path.join(ROOT, "outside")))

# ---------- quota_used ----------
before = cs.quota_used("alice")
with open(os.path.join(base, "docs", "size.bin"), "wb") as f:
    f.write(b"0123456789")
check("quota_used 统计", cs.quota_used("alice") == before + 10)
check("quota_used 无主页为 0", cs.quota_used("ghost") == 0)

# ---------- 通用文件 ----------
ok, msg = common_upload([("file", b"common_a.txt", b"COMMON-A")], 10**9)
check("通用上传成功", ok and msg == "上传成功")
check("通用文件落盘", os.path.exists(os.path.join(cs.common_dir(), "common_a.txt")))
ok, msg = common_upload([("file", b"big.bin", b"12345")], 4)
check("通用超限", not ok and msg == "超出配额")
check("通用超限临时清理",
      not any(f.startswith("up_") for f in os.listdir(os.path.join(CLOUD, ".tmp"))))
lst = cs.common_list()
check("common_list", any(e["name"] == "common_a.txt" and e["size"] == 8
                         and e["rel"] == "common_a.txt" for e in lst))
r = cs.list_dir("alice", "通用文件")
check("list_dir 通用文件区", r["ok"] is True and r["files"] == []
      and any(e["name"] == "common_a.txt" and e["rel"] == "通用文件/common_a.txt"
              for e in r["common"]))
r = cs.list_dir("alice", "通用文件/..")
check("list_dir 通用逃逸", r["ok"] is False)
ok, msg = cs.delete_common("common_a.txt")
check("delete_common 成功", ok and msg == "删除成功"
      and not os.path.exists(os.path.join(cs.common_dir(), "common_a.txt")))
ok, msg = cs.delete_common("common_a.txt")
check("delete_common 不存在", not ok)
ok, msg = cs.delete_common("../evil")
check("delete_common 越界", not ok)
os.makedirs(os.path.join(cs.common_dir(), "subdir"), exist_ok=True)
ok, msg = cs.delete_common("subdir")
check("delete_common 拒绝目录", not ok)
if symlink_ok:
    try:
        os.symlink(ROOT, os.path.join(cs.common_dir(), "sl"))
        ok, msg = cs.delete_common("sl")
        check("delete_common 拒绝符号链接", not ok)
        os.unlink(os.path.join(cs.common_dir(), "sl"))
    except OSError:
        pass

# ---------- 大文件流式（>1 个 64KB 块）----------
big = bytes(range(256)) * 600   # 153600 字节，跨 3 个 64KB 块
ok, msg = upload("alice", "", [("file", b"big.bin", big)], 10**9)
check("大文件流式", ok and open(os.path.join(base, "big.bin"), "rb").read() == big)

# 大文件配额中途超限
ok, msg = upload("alice", "", [("file", b"big2.bin", big)], 100000)
check("大文件配额中途中止", not ok and msg == "超出配额")
check("大文件配额中止清理",
      not os.path.exists(os.path.join(base, "big2.bin"))
      and not any(f.startswith("up_") for f in os.listdir(os.path.join(CLOUD, ".tmp"))))

print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(1 if FAIL else 0)

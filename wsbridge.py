# -*- coding: utf-8 -*-
"""
wsbridge.py —— 把浏览器（noVNC）的 WebSocket 桥到客户机的 VNC 端口（供 iscsi_broker.py 使用）。

为什么要桥：无盘客户机的 VNC 端口只在局域网里、而且浏览器不能直接开 TCP，
所以服务器做一次中继：浏览器 --ws--> 服务器 --tcp 5900--> 客户机。

实现要点：
1. 纯 Python 标准库，自己实现 RFC6455 的握手与帧收发（不引入 websockets 之类的包）。
2. 握手必须自己写裸 HTTP/1.1 响应：BaseHTTPRequestHandler 默认按 HTTP/1.0 输出状态行，
   而 WebSocket 规范要求 101 的响应是 HTTP/1.1。
3. 客户端发来的帧一定带掩码（RFC6455 规定），要解掩码；服务器发出的帧不能带掩码。
4. 支持分片（continuation）、ping/pong、close；文本帧与二进制帧都按字节原样转发，
   VNC 协议本身是二进制流。
5. 两个方向各一个线程：浏览器→客户机（主线程）、客户机→浏览器（子线程）。
   任一侧断开就关掉另一侧，避免线程泄漏。
"""

import base64
import hashlib
import socket
import struct
import threading

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class BridgeError(Exception):
    """握手失败（带 HTTP 状态码）。"""

    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg


def _read_exact(rfile, n):
    """从缓冲流里精确读 n 字节；EOF 返回 None。"""
    data = rfile.read(n)
    if data is None or len(data) < n:
        return None
    return data


def _recv_frame(rfile):
    """读一个 WebSocket 帧，返回 (opcode, fin, payload) 或 None（连接结束）。"""
    head = _read_exact(rfile, 2)
    if head is None:
        return None
    b1, b2 = head[0], head[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        ext = _read_exact(rfile, 2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _read_exact(rfile, 8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
    if length > (16 << 20):          # 单帧 16MB 上限（VNC 帧长不会到这个量级）
        return None
    mask = _read_exact(rfile, 4) if masked else None
    if masked and mask is None:
        return None
    payload = _read_exact(rfile, length) if length else b""
    if payload is None:
        return None
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, fin, payload


def _send_frame(wfile, opcode, payload):
    """按 RFC6455 发一个帧（服务器→客户端不加掩码）。"""
    header = bytearray()
    header.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", n))
    wfile.write(bytes(header) + payload)
    wfile.flush()


def handle(handler, host, port, connect_timeout=8.0):
    """处理一次 WebSocket 升级请求，并把它桥到 host:port。

    handler：BaseHTTPRequestHandler（已由调用方完成会话校验）
    host/port：客户机 VNC 地址
    抛 BridgeError 表示握手/连接失败（调用方负责回 HTTP 错误）。
    """
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or (handler.headers.get("Upgrade", "").lower() != "websocket"):
        raise BridgeError(400, "不是合法的 WebSocket 升级请求")

    try:
        vnc = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as e:
        raise BridgeError(502, "连不上客户机 VNC %s:%s（%s）" % (host, port, e))
    vnc.settimeout(None)

    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    resp = ["HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Accept: " + accept]
    sub = handler.headers.get("Sec-WebSocket-Protocol")
    if sub:
        resp.append("Sec-WebSocket-Protocol: " + sub.split(",")[0].strip())
    handler.wfile.write(("\r\n".join(resp) + "\r\n\r\n").encode("ascii"))
    handler.wfile.flush()
    handler.close_connection = True      # 之后这个连接归本函数管，HTTP 层别再复用

    rfile, wfile = handler.rfile, handler.wfile
    stop = threading.Event()
    send_lock = threading.Lock()      # 两个方向都可能发帧，串行化避免帧交叠

    def send(opcode, payload):
        with send_lock:
            _send_frame(wfile, opcode, payload)

    def pump_vnc_to_ws():
        """客户机 → 浏览器。"""
        try:
            while not stop.is_set():
                data = vnc.recv(65536)
                if not data:
                    break
                send(OP_BIN, data)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                send(OP_CLOSE, struct.pack(">H", 1000))
            except Exception:
                pass
            try:
                vnc.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    t = threading.Thread(target=pump_vnc_to_ws, daemon=True)
    t.start()

    frag = bytearray()
    frag_op = OP_BIN
    try:
        while not stop.is_set():
            frame = _recv_frame(rfile)
            if frame is None:
                break
            opcode, fin, payload = frame
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN):
                frag_op = opcode
                frag = bytearray(payload)
            elif opcode == OP_CONT:
                frag.extend(payload)
            else:
                continue
            if fin:
                if frag:
                    vnc.sendall(bytes(frag))
                frag = bytearray()
    except Exception:
        pass
    finally:
        stop.set()
        try:
            vnc.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            vnc.close()
        except Exception:
            pass
        t.join(timeout=2.0)

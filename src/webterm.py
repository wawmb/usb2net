"""
webterm.py - Web 终端

在浏览器中提供 xterm.js 终端，后端以 telnet 客户端连到本机桥接的
TCP 端口（= 串口），实现网页访问串口控制台。

特性：
  - 每个桥接拥有自己独立、可配置的 Web 端口（默认 9211~9214）
  - HTTP 页面与 WebSocket 共用同一个端口（单端口模型，process_request 实现）
  - 无需 ?id= 路由：页面直接用 location.host 连 /ws
  - 配置项 web_port 来自 config.json，GUI 里可改

实现：
  - WebTerminal      : 单桥接的 Web 终端服务（单端口 HTTP + WS 桥接到本机 TCP）
  - WebTerminalHub   : 管理多个 WebTerminal（按 bridge_id 注册/注销）
  - TelnetFilter     : 增量剥离 telnet 协商(IAC)序列，并对对端给出最小应答
"""

import os
import sys
import json
import asyncio
import threading
import time
import webbrowser

from websockets.asyncio.server import serve as _ws_serve
from websockets.http11 import Response
from websockets.datastructures import Headers


# 资源目录：打包后优先使用 PyInstaller 释放目录 _MEIPASS
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _STATIC_DIR = os.path.join(sys._MEIPASS, "static")
else:
    _STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ── 官方 xterm.css（内联，保证离线渲染正确）─────────────────────
_XTERM_CSS = """
.xterm { position: relative; user-select: none; -ms-user-select: none;
  -webkit-user-select: none; }
.xterm.focus, .xterm:focus { outline: none; }
.xterm .xterm-helpers { position: absolute; top: 0; z-index: 5; }
.xterm .xterm-helper-textarea { position: absolute; opacity: 0; left: -9999em;
  top: 0; width: 1px; height: 1px; z-index: -5; white-space: nowrap;
  overflow: hidden; resize: none; }
.xterm .composition-view { position: absolute; z-index: 2; color: #fff;
  white-space: nowrap; background: #000; display: none; }
.xterm .composition-view.active { display: block; }
.xterm .xterm-viewport { position: absolute; inset: 0; overflow-y: scroll;
  overflow-x: hidden; cursor: default; }
.xterm .xterm-screen { position: relative; width: 100%; height: 100%; }
.xterm .xterm-screen canvas { position: absolute; left: 0; top: 0; }
.xterm .xterm-scroll-area { visibility: hidden; }
.xterm .xterm-rows { position: absolute; left: 0; top: 0; }
.xterm .xterm-rows > div { height: 16px; line-height: 16px; white-space: nowrap; }
.xterm .xterm-char-measure-element { display: inline-block; visibility: hidden;
  position: absolute; top: 0; left: -9999em; white-space: pre; font: inherit; }
.xterm.enable-mouse-events { cursor: default; }
.xterm.xterm-cursor-hidden .terminal-cursor { display: none; }
.xterm .terminal-cursor { background-color: #fff; color: #000; }
.xterm .xterm-viewport::-webkit-scrollbar { width: 10px; }
.xterm .xterm-viewport::-webkit-scrollbar-thumb { background-color: rgba(255,255,255,0.3); }
"""


# ── Telnet IAC 协商过滤 ─────────────────────────────────────────


class TelnetFilter:
    """
    增量解析 telnet IAC 序列：剥离控制字节（不让 xterm 显示乱码），
    并对服务器的 WILL/DO 给出最小应答，使协商顺利完成。

    对应连接时发送的那 15 字节终端协商报文：
        FF FB 03 FB 01 FE 01 FD 00 FB 00
    """

    IAC = 0xFF

    def __init__(self):
        self._buf = bytearray()  # 跨包缓存未处理完的尾随数据

    def feed(self, data: bytes):
        """返回 (clean_bytes, responses)。
        clean_bytes  = 转发给浏览器的纯文本
        responses    = 需回写给对端的 telnet 应答
        """
        self._buf.extend(data)
        out = bytearray()
        resp = bytearray()
        i = 0
        n = len(self._buf)
        while i < n:
            b = self._buf[i]
            if b != self.IAC:
                out.append(b)
                i += 1
                continue
            # b == IAC
            if i + 1 >= n:
                break  # 等待更多数据补齐命令
            c = self._buf[i + 1]
            if c == self.IAC:  # IAC IAC -> 字面 0xFF 数据
                out.append(self.IAC)
                i += 2
                continue
            if c == 0xFA:  # SB 子协商，找到 IAC SE 结束
                j = i + 2
                while j + 1 < n:
                    if self._buf[j] == self.IAC and self._buf[j + 1] == 0xF0:
                        break
                    j += 1
                if j + 1 >= n:
                    break  # 未收到 SE，等待
                i = j + 2
                continue
            if c in (0xFB, 0xFC, 0xFD, 0xFE):  # WILL/WONT/DO/DONT + 选项字节
                if i + 2 >= n:
                    break
                opt = self._buf[i + 2]
                self._respond(opt, c, resp)
                i += 3
                continue
            # 其它单字节 telnet 命令
            i += 2
            continue
        del self._buf[:i]
        return bytes(out), bytes(resp)

    @staticmethod
    def _respond(opt: int, cmd: int, resp: bytearray):
        IAC = TelnetFilter.IAC
        if opt == 1:  # ECHO：交给我们不回显，让远端(串口控制台)负责 echo
            if cmd == 0xFB:  # 服务器 WILL ECHO -> 我们 DONT ECHO
                resp += bytes([IAC, 0xFE, opt])
            elif cmd == 0xFD:  # 服务器 DO ECHO   -> 我们 WONT ECHO
                resp += bytes([IAC, 0xFC, opt])
            return
        # 其余选项：标准"接受"应答
        if cmd == 0xFB:  # 服务器 WILL -> 我们 DO
            resp += bytes([IAC, 0xFD, opt])
        elif cmd == 0xFD:  # 服务器 DO   -> 我们 WILL
            resp += bytes([IAC, 0xFB, opt])
        elif cmd == 0xFC:  # 服务器 WONT -> 我们 DONT
            resp += bytes([IAC, 0xFE, opt])
        elif cmd == 0xFE:  # 服务器 DONT -> 我们 WONT
            resp += bytes([IAC, 0xFC, opt])


# ── 单桥接 Web 终端（单端口 HTTP + WS）──────────────────────────

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USB2Net · __NAME__</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #000; overflow: hidden; }
  #term { position: absolute; inset: 0; padding: 4px; box-sizing: border-box; }
  __XTERM_CSS__
</style>
</head>
<body>
<div id="term"></div>
<script src="/xterm.js"></script>
<script src="/addon-fit.js"></script>
<script>
  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'Consolas, "Courier New", monospace',
    theme: { background: '#000000', foreground: '#cccccc', cursor: '#ffffff' }
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById('term'));
  fitAddon.fit();

  // 单端口模型：页面与 WebSocket 共用同一个端口（location.host 已含端口）
  // 不再需要 ?id= 路由
  let ws = null;
  let reconnectTimer = null;

  function connect() {
    ws = new WebSocket('ws://' + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () { term.clear(); };
    ws.onmessage = function (ev) {
      if (typeof ev.data === 'string') return;        // 控制消息忽略
      term.write(new Uint8Array(ev.data));
    };
    ws.onclose = function () {
      term.write('\\r\\n\\x1b[31m[连接已断开，3秒后自动重连...]\\x1b[0m\\r\\n');
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 3000);
    };
    ws.onerror = function () {
      term.write('\\r\\n\\x1b[31m[WebSocket 连接错误]\\x1b[0m\\r\\n');
    };
  }
  connect();

  term.onData(function (data) {
    if (ws && ws.readyState === 1) ws.send(new TextEncoder().encode(data));
  });
  term.onResize(function (size) {
    if (ws && ws.readyState === 1)
      ws.send(JSON.stringify({ type: 'resize', cols: size.cols, rows: size.rows }));
  });
  window.addEventListener('resize', function () { fitAddon.fit(); });
</script>
</body>
</html>
"""


class WebTerminal:
    """
    单桥接的 Web 终端服务。

      - 在 web_port 上用 process_request 同时提供 HTTP 页面与 WebSocket 握手
        （单端口，无 ?id= 路由）
      - 任意 WebSocket 连接都桥接到本桥接的本地 TCP 端口（= 串口）
    """

    def __init__(
        self, web_port: int, name: str, target_host: str, target_port: int, log_cb=None
    ):
        self.web_port = web_port
        self.name = name
        self.target_host = target_host
        self.target_port = target_port
        self._log = log_cb or (lambda l, m: None)
        self._server = None
        self._loop = None
        self._thread = None
        self._stop_event = None
        self._running = False

    # ── 启停 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(0.3)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve_coro())
        except Exception as e:
            self._log("error", f"[Web终端:{self.name}] 服务异常: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve_coro(self):
        self._stop_event = asyncio.Event()
        # websockets 16 异步版 serve 是异步上下文管理器，必须在运行中的 loop 内
        # 用 async with 进入，才会真正 create_server 并开始监听
        async with _ws_serve(
            self._ws_handler,
            "0.0.0.0",
            self.web_port,
            process_request=self._http_process_request,
        ) as server:
            self._server = server
            self._log(
                "info", f"[Web终端:{self.name}] 已启动: http://0.0.0.0:{self.web_port}"
            )
            # 持续运行，直到 stop() 触发 _stop_event
            await self._stop_event.wait()

    def stop(self):
        self._running = False
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        except Exception:
            pass

    async def _shutdown(self):
        try:
            if self._server is not None:
                self._server.close()
        except Exception:
            pass
        if self._stop_event is not None:
            self._stop_event.set()

    # ── HTTP 页面（process_request，与 WS 共用同一端口）──

    def _http_process_request(self, connection, request):
        # WebSocket 升级请求：交还给 websockets 走握手流程（不再区分路径/ID）
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None

        path = request.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = self._page_html().encode("utf-8")
            return Response(
                200,
                "OK",
                Headers(
                    [
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                    ]
                ),
                body,
            )
        if path in ("/xterm.js", "/addon-fit.js"):
            fpath = os.path.join(_STATIC_DIR, path[1:])
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    body = f.read()
                return Response(
                    200,
                    "OK",
                    Headers(
                        [
                            ("Content-Type", "application/javascript"),
                            ("Content-Length", str(len(body))),
                        ]
                    ),
                    body,
                )
        return Response(
            404,
            "Not Found",
            Headers(
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", "9"),
                ]
            ),
            b"not found",
        )

    def _page_html(self) -> str:
        return _PAGE_TEMPLATE.replace("__XTERM_CSS__", _XTERM_CSS).replace(
            "__NAME__", self.name
        )

    # ── WebSocket 桥接处理 ──

    async def _ws_handler(self, websocket):
        try:
            reader, writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
        except Exception as e:
            self._log(
                "error",
                f"[Web终端:{self.name}] 连接桥接 ({self.target_host}:"
                f"{self.target_port}) 失败: {e}",
            )
            try:
                await websocket.close()
            except Exception:
                pass
            return

        self._log(
            "info",
            f"[Web终端:{self.name}] 客户端已连接 -> "
            f"({self.target_host}:{self.target_port})",
        )
        filt = TelnetFilter()
        # 初始协商：声明我们是终端（远端负责 echo）
        writer.write(
            bytes(
                [
                    0xFF,
                    0xFD,
                    0x03,  # DO  Suppress Go Ahead
                    0xFF,
                    0xFC,
                    0x01,  # WONT Echo (远端回显)
                    0xFF,
                    0xFD,
                    0x00,
                ]
            )
        )  # DO  Binary
        try:
            await writer.drain()
        except Exception:
            pass

        async def ws_to_tcp():
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)):
                    writer.write(bytes(message))
                    try:
                        await writer.drain()
                    except Exception:
                        break
                elif isinstance(message, str):
                    # 控制消息：终端尺寸变化 -> 发送 telnet NAWS
                    try:
                        obj = json.loads(message)
                        if obj.get("type") == "resize":
                            cols = int(obj.get("cols", 80))
                            rows = int(obj.get("rows", 24))
                            self._send_naws(writer, cols, rows)
                    except Exception:
                        pass

        async def tcp_to_ws():
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                clean, resp = filt.feed(data)
                if resp:
                    writer.write(resp)
                    try:
                        await writer.drain()
                    except Exception:
                        pass
                if clean:
                    try:
                        await websocket.send(clean)
                    except Exception:
                        break

        try:
            await asyncio.gather(ws_to_tcp(), tcp_to_ws())
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
            self._log("info", f"[Web终端:{self.name}] 客户端断开")

    @staticmethod
    def _send_naws(writer, cols: int, rows: int):
        """发送 telnet 窗口尺寸协商 (NAWS)"""
        payload = bytes(
            [
                0xFF,
                0xFA,
                0x1F,  # IAC SB NAWS
                (cols >> 8) & 0xFF,
                cols & 0xFF,  # 宽度(16位)
                (rows >> 8) & 0xFF,
                rows & 0xFF,  # 高度(16位)
                0xFF,
                0xF0,  # IAC SE
            ]
        )
        try:
            writer.write(payload)
            asyncio.ensure_future(writer.drain())
        except Exception:
            pass


# ── Web 终端管理器（按桥接注册/注销）──────────────────────────


class WebTerminalHub:
    """
    管理多个 WebTerminal 实例，每个桥接一个，端口由配置决定。

      - register(bridge_id, web_port, host, tcp_port, name): 为该桥接启动一个独立
        Web 终端服务（端口 = web_port）；web_port<=0 视为不启用 Web 终端
      - unregister(bridge_id): 停止并移除
      - url_for(bridge_id): 该桥接 Web 终端的固定访问地址（无 id）
    """

    def __init__(self, log_cb=None):
        self._log = log_cb or (lambda l, m: None)
        self._terminals = {}  # bridge_id -> WebTerminal

    def register(self, bridge_id: int, web_port: int, host: str, port: int, name: str):
        # 若已存在，先停掉旧的（端口/目标变化时需要）
        self.unregister(bridge_id)

        if not web_port or web_port <= 0:
            self._log("info", f"[Web终端] {name}: 未配置 Web 端口，已跳过")
            return

        try:
            wt = WebTerminal(web_port, name, host, port, log_cb=self._log)
            wt.start()
            self._terminals[bridge_id] = wt
        except Exception as e:
            self._log("error", f"[Web终端] {name}: 启动失败 (端口 {web_port}): {e}")

    def unregister(self, bridge_id: int):
        wt = self._terminals.pop(bridge_id, None)
        if wt:
            try:
                wt.stop()
            except Exception:
                pass

    def url_for(self, bridge_id: int):
        wt = self._terminals.get(bridge_id)
        if wt:
            return f"http://127.0.0.1:{wt.web_port}"
        return None

    def start(self):
        """兼容旧调用：终端在 register 时已启动，这里无需操作"""
        pass

    def stop(self):
        for bid in list(self._terminals.keys()):
            self.unregister(bid)


def open_browser(url: str):
    """在默认浏览器打开 Web 终端页面"""
    try:
        webbrowser.open(url)
    except Exception:
        pass

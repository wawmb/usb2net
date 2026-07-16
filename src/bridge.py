"""
bridge.py - Serial-to-TCP Bridge Engine

串口 ↔ TCP 桥接核心引擎的 Python 实现：
  - 串口管理
  - TCP 服务器（接受多客户端连接）
  - 双向数据桥接 (串口 <-> TCP)
  - 串口热插拔自动重连
  - 多客户端支持
  - 统计计数 & 回调通知
"""

import serial
import serial.tools.list_ports
import socket
import threading
import time
import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Optional, List, Dict


# ── 枚举 & 数据类 ──────────────────────────────────────────────


class BridgeState(Enum):
    STOPPED = "已停止"
    STARTING = "启动中"
    RUNNING = "运行中"
    ERROR = "错误"
    WAITING = "等待设备"


@dataclass
class BridgeConfig:
    """单条桥接配置"""

    name: str = "网络串口-1"
    serial_port: str = ""
    baudrate: int = 115200
    tcp_port: int = 5000
    enabled: bool = True
    databits: int = 8  # 数据位 5/6/7/8
    parity: str = "N"  # 校验 N/E/O/S/M
    stopbits: float = 1  # 停止位 1/1.5/2
    flow_control: str = "none"  # none/xonxoff/rtscts
    auto_reconnect: bool = True
    telnet_mode: bool = False  # 是否启用 telnet 协商（连接时发送终端协商报文）
    web_port: int = 0  # Web 终端端口（0=不启用）；HTTP 页面与 WS 共用此端口

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BridgeConfig":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


# ── 回调类型 ──────────────────────────────────────────────────

LogCallback = Callable[[str, str], None]  # (level, message)
StateCallback = Callable[[int, BridgeState], None]  # (bridge_id, state)
DataCallback = Callable[
    [int, str, bytes], None
]  # (bridge_id, direction, data)  direction: 'rx'|'tx'


# ── 串口↔TCP 桥接 ─────────────────────────────────────────────


class SerialTCPBridge:
    """
    单个串口到 TCP 的桥接实例。

    工作模型（线程驱动）：
      - _tcp_accept_loop : 接受 TCP 客户端连接
      - _serial_read_loop : 读串口数据 → 广播给所有 TCP 客户端
      - _client_read_loop : 每个 TCP 客户端一个线程，读 TCP 数据 → 写串口
      - _reconnect_loop   : 串口断开后定时尝试重连（热插拔）
    """

    # telnet 协商头（连接时发送的 15 字节终端协商报文）
    _TELNET_IAC = bytes(
        [
            0xFF,
            0xFB,
            0x03,  # IAC WILL Suppress Go Ahead
            0xFF,
            0xFB,
            0x01,  # IAC WILL Echo
            0xFF,
            0xFE,
            0x01,  # IAC DONT Echo
            0xFF,
            0xFD,
            0x00,  # IAC DO Binary
            0xFF,
            0xFB,
            0x00,  # IAC WILL Binary
        ]
    )

    # 终端消息前缀：FF FE FD + 波特率(ASCII) → 动态切换串口波特率
    _TERINFO_BAUDRATE = bytes([0xFF, 0xFE, 0xFD])

    def __init__(
        self,
        bridge_id: int,
        config: BridgeConfig,
        log_cb: Optional[LogCallback] = None,
        state_cb: Optional[StateCallback] = None,
        data_cb: Optional[DataCallback] = None,
        persist_cb: Optional[Callable[[], None]] = None,
    ):
        self.id = bridge_id
        self.config = config
        self._log_cb = log_cb
        self._state_cb = state_cb
        self._data_cb = data_cb
        self._persist_cb = persist_cb

        self.state = BridgeState.STOPPED
        self.serial_conn: Optional[serial.Serial] = None
        self.tcp_server: Optional[socket.socket] = None
        self._tcp_clients: Dict[socket.socket, tuple] = {}
        self._lock = threading.Lock()
        self._running = False
        self._threads: List[threading.Thread] = []

        # 统计
        self.bytes_serial_to_tcp = 0
        self.bytes_tcp_to_serial = 0
        self.client_count = 0
        self.start_time: Optional[float] = None

    # ── 公共接口 ──

    def start(self):
        """启动桥接"""
        if self._running:
            return
        self._running = True
        self.start_time = time.time()
        self._set_state(BridgeState.STARTING)
        self._log("info", f"[{self.config.name}] 正在启动...")

        # 启动 TCP 服务器线程
        t_tcp = threading.Thread(
            target=self._tcp_accept_loop, daemon=True, name=f"tcp-{self.id}"
        )
        t_tcp.start()
        self._threads.append(t_tcp)

        # 启动串口读取线程
        t_serial = threading.Thread(
            target=self._serial_read_loop, daemon=True, name=f"serial-{self.id}"
        )
        t_serial.start()
        self._threads.append(t_serial)

        # 启动重连监控线程
        if self.config.auto_reconnect:
            t_reconn = threading.Thread(
                target=self._reconnect_loop, daemon=True, name=f"reconn-{self.id}"
            )
            t_reconn.start()
            self._threads.append(t_reconn)

    def stop(self):
        """停止桥接"""
        self._running = False
        self._log("info", f"[{self.config.name}] 正在停止...")

        # 关闭所有 TCP 客户端
        with self._lock:
            for client in list(self._tcp_clients.keys()):
                try:
                    client.close()
                except Exception:
                    pass
            self._tcp_clients.clear()
            self.client_count = 0

        # 关闭 TCP 服务器
        if self.tcp_server:
            try:
                self.tcp_server.close()
            except Exception:
                pass
            self.tcp_server = None

        # 关闭串口
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None

        self._set_state(BridgeState.STOPPED)
        self._log("info", f"[{self.config.name}] 已停止")

    def update_config(self, config: BridgeConfig):
        """更新配置（需要重启生效）"""
        was_running = self._running
        if was_running:
            self.stop()
        self.config = config
        if was_running:
            time.sleep(0.3)
            self.start()

    def get_stats(self) -> dict:
        """获取统计信息"""
        uptime = (
            time.time() - self.start_time if self.start_time and self._running else 0
        )
        return {
            "state": self.state.value,
            "bytes_serial_to_tcp": self.bytes_serial_to_tcp,
            "bytes_tcp_to_serial": self.bytes_tcp_to_serial,
            "client_count": self.client_count,
            "uptime": uptime,
        }

    # ── 串口操作 ──

    def _open_serial(self) -> bool:
        """打开串口，成功返回 True"""
        if not self.config.serial_port:
            self._log("warn", f"[{self.config.name}] 未指定串口")
            return False

        try:
            if self.serial_conn and self.serial_conn.is_open:
                return True

            # 映射配置参数
            bytesize_map = {
                5: serial.FIVEBITS,
                6: serial.SIXBITS,
                7: serial.SEVENBITS,
                8: serial.EIGHTBITS,
            }
            parity_map = {
                "N": serial.PARITY_NONE,
                "E": serial.PARITY_EVEN,
                "O": serial.PARITY_ODD,
                "S": serial.PARITY_SPACE,
                "M": serial.PARITY_MARK,
            }
            stopbits_map = {
                1: serial.STOPBITS_ONE,
                1.5: serial.STOPBITS_ONE_POINT_FIVE,
                2: serial.STOPBITS_TWO,
            }

            kwargs = dict(
                port=self.config.serial_port,
                baudrate=self.config.baudrate,
                bytesize=bytesize_map.get(self.config.databits, serial.EIGHTBITS),
                parity=parity_map.get(self.config.parity, serial.PARITY_NONE),
                stopbits=stopbits_map.get(self.config.stopbits, serial.STOPBITS_ONE),
                timeout=0.1,  # 非阻塞读，短超时
                write_timeout=0.5,
            )
            if self.config.flow_control == "xonxoff":
                kwargs["xonxoff"] = True
            elif self.config.flow_control == "rtscts":
                kwargs["rtscts"] = True

            self.serial_conn = serial.Serial(**kwargs)
            self._log(
                "info",
                f"[{self.config.name}] 串口 {self.config.serial_port} "
                f"@ {self.config.baudrate} 已打开",
            )
            self._set_state(BridgeState.RUNNING)
            return True

        except Exception as e:
            self._log(
                "error",
                f"[{self.config.name}] 打开串口 {self.config.serial_port} 失败: {e}",
            )
            self._set_state(
                BridgeState.WAITING if self.config.auto_reconnect else BridgeState.ERROR
            )
            return False

    def _serial_read_loop(self):
        """串口读取循环：串口数据 → 广播给所有 TCP 客户端"""
        while self._running:
            # 确保串口已打开（在循环顶部取一次引用，避免读到一半被重配置替换）
            conn = self.serial_conn
            if conn is None or not conn.is_open:
                if not self._open_serial():
                    time.sleep(1)
                    continue
                conn = self.serial_conn

            try:
                data = conn.read(4096)
            except (serial.SerialException, TypeError, ValueError, OSError) as e:
                # 波特率重配置/热插拔瞬间，旧串口句柄会被置空或关闭，
                # pyserial 在 Windows 上会抛出 byref() argument ... not None 这类
                # ctypes 异常。属可恢复错误：仅当当前线程持有的仍是「同一个」
                # 连接时才释放它，下次循环会用新波特率重新打开；若连接已被
                # 重配置替换（conn is not self.serial_conn），则直接忽略本次读取。
                if conn is self.serial_conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self.serial_conn = None
                    self._set_state(
                        BridgeState.WAITING
                        if self.config.auto_reconnect
                        else BridgeState.ERROR
                    )
                # 重配置过程中偶发的瞬断，记录为 debug，避免误报为致命错误
                self._log(
                    "debug",
                    f"[{self.config.name}] 串口读取在重配置窗口内中断，已自动恢复: {e}",
                )
                time.sleep(0.3)
                continue

            if data:
                self.bytes_serial_to_tcp += len(data)
                self._notify_data("rx", data)  # rx = serial -> tcp

                # 广播给所有 TCP 客户端
                with self._lock:
                    dead_clients = []
                    for client in self._tcp_clients:
                        try:
                            client.sendall(data)
                        except Exception:
                            dead_clients.append(client)
                    for dc in dead_clients:
                        self._remove_client(dc)

    def _close_serial(self):
        """关闭串口连接"""
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None

    def _reconnect_loop(self):
        """串口热插拔监控：检测断开后定时尝试重连"""
        while self._running:
            time.sleep(2)
            if not self._running:
                break

            # 如果串口未打开，尝试检测端口是否可用
            if not self.serial_conn or not self.serial_conn.is_open:
                if self.config.serial_port:
                    available = [p.device for p in serial.tools.list_ports.comports()]
                    if self.config.serial_port in available:
                        self._log(
                            "info",
                            f"[{self.config.name}] 检测到设备 {self.config.serial_port}，尝试重连",
                        )
                        # _serial_read_loop 会自动尝试打开
                    # 如果配置的端口不在可用列表中，说明设备被拔出

    # ── TCP 服务器 ──

    def _tcp_accept_loop(self):
        """TCP 连接接受循环"""
        try:
            self.tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp_server.bind(("0.0.0.0", self.config.tcp_port))
            self.tcp_server.listen(32)  # MAX_LISTEN_CONNECTS = 32
            self.tcp_server.settimeout(0.5)
            self._log(
                "info",
                f"[{self.config.name}] TCP 服务器监听 0.0.0.0:{self.config.tcp_port}",
            )
        except Exception as e:
            self._log("error", f"[{self.config.name}] TCP 服务器启动失败: {e}")
            self._set_state(BridgeState.ERROR)
            return

        while self._running:
            try:
                client_sock, client_addr = self.tcp_server.accept()
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                with self._lock:
                    if len(self._tcp_clients) >= 32:
                        client_sock.close()
                        self._log(
                            "warn",
                            f"[{self.config.name}] 连接数已满，拒绝 {client_addr}",
                        )
                        continue
                    self._tcp_clients[client_sock] = client_addr
                    self.client_count = len(self._tcp_clients)

                self._log(
                    "info",
                    f"[{self.config.name}] 客户端连接: {client_addr[0]}:{client_addr[1]} "
                    f"(共 {self.client_count} 个客户端)",
                )

                # telnet 协商（连接时始终发送 15 字节终端协商报文）
                if self.config.telnet_mode:
                    try:
                        client_sock.sendall(self._TELNET_IAC)
                    except Exception:
                        pass

                # 启动客户端读取线程
                t = threading.Thread(
                    target=self._client_read_loop,
                    args=(client_sock,),
                    daemon=True,
                    name=f"client-{self.id}-{client_addr[0]}",
                )
                t.start()

            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                if self._running:
                    self._log("error", f"[{self.config.name}] TCP accept 异常: {e}")
                break

    def _client_read_loop(self, client_sock: socket.socket):
        """单个 TCP 客户端的读取循环：TCP 数据 → 写入串口"""
        client_addr = self._tcp_clients.get(client_sock, ("unknown", 0))

        while self._running:
            try:
                data = client_sock.recv(4096)
                if not data:
                    break

                # 终端控制消息拦截
                #  - FF FE FD + 波特率(ASCII) → 动态切换串口波特率
                #  - 全 0xFF（每 3 字节一组）→ 终端控制消息，丢弃不写串口
                if self.config.telnet_mode:
                    is_term, new_baud = self._is_terminal_msg(data)
                    if is_term:
                        if new_baud:
                            self._reconfigure_baudrate(new_baud)
                        self._log(
                            "debug", f"[{self.config.name}] 终端控制消息，已忽略写串口"
                        )
                        continue

                self.bytes_tcp_to_serial += len(data)
                self._notify_data("tx", data)  # tx = tcp -> serial

                # 写入串口
                if self.serial_conn and self.serial_conn.is_open:
                    self.serial_conn.write(data)
                    self.serial_conn.flush()
                else:
                    self._log(
                        "warn",
                        f"[{self.config.name}] 串口未就绪，丢弃 {len(data)} 字节",
                    )

            except ConnectionResetError:
                break
            except Exception as e:
                if self._running:
                    self._log(
                        "debug",
                        f"[{self.config.name}] 客户端 {client_addr} 读取异常: {e}",
                    )
                break

        self._remove_client(client_sock)
        self._log(
            "info",
            f"[{self.config.name}] 客户端断开: {client_addr[0]}:{client_addr[1]}",
        )

    def _strip_telnet(self, data: bytes, client_sock: socket.socket) -> bytes:
        """过滤 telnet 协商命令，返回纯数据"""
        IAC = 0xFF
        result = bytearray()
        i = 0
        while i < len(data):
            if data[i] == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd == 0xFB or cmd == 0xFC:  # WILL / WONT
                    if i + 2 < len(data):
                        i += 3
                        continue
                elif cmd == 0xFD or cmd == 0xFE:  # DO / DONT
                    if i + 2 < len(data):
                        i += 3
                        continue
                elif cmd == 0xFF:  # IAC IAC → 0xFF
                    result.append(0xFF)
                    i += 2
                    continue
                else:
                    i += 2
                    continue
            else:
                result.append(data[i])
                i += 1
        return bytes(result)

    def _is_terminal_msg(self, buff: bytes):
        """
        识别终端控制消息并决定是否切换波特率。
        返回 (is_terminal, new_baudrate)：
          - 若以 FF FE FD 开头：is_terminal=True, new_baudrate=解析到的波特率
          - 若全部为 0xFF（每 3 字节一组）：is_terminal=True, new_baudrate=None
          - 否则 (False, None)
        """
        # 1) 动态波特率指令：FF FE FD + 波特率 ASCII
        if buff[:3] == self._TERINFO_BAUDRATE:
            try:
                raw = buff[3:].split(b"\x00")[0].decode("ascii", "ignore").strip()
                baud = int(raw)
            except Exception:
                baud = None
            return True, baud

        # 2) 全 0xFF（每 3 字节一组）的终端控制消息
        n = len(buff)
        if n >= 3 and n % 3 == 0:
            ok = True
            for i in range(0, n, 3):
                if buff[i] != 0xFF:
                    ok = False
                    break
            if ok:
                return True, None

        return False, None

    def _reconfigure_baudrate(self, baudrate: int):
        """动态切换串口波特率并持久化配置"""
        if baudrate <= 0:
            self._log("warn", f"[{self.config.name}] 收到非法波特率: {baudrate}")
            return
        if baudrate == self.config.baudrate:
            return  # 已是该波特率，无需任何操作（含串口已打开或尚未连接的情况）

        # 先更新配置，确保重配置窗口内任何并发的 _open_serial 都使用新波特率
        self.config.baudrate = baudrate
        self._log("info", f"[{self.config.name}] 远端指令：波特率切换为 {baudrate}")
        # 关闭当前串口，_open_serial 会用新波特率重新打开
        self._close_serial()
        if not self._open_serial():
            self._log("error", f"[{self.config.name}] 波特率切换失败，请检查设备")
        # 无论串口是否成功重开，配置已更新，均持久化到 config.json
        # （串口未连接时也需保存用户设定）
        if self._persist_cb:
            try:
                self._persist_cb()
            except Exception:
                pass

    def set_baudrate(self, baudrate: int):
        """
        公开 API：实时切换串口波特率。
        串口已打开时立即关闭并以新波特率重开；未打开（如尚未指定 COM 口）时
        仅更新配置并持久化，待串口打开即用新波特率。
        """
        self._reconfigure_baudrate(baudrate)

    def _remove_client(self, client_sock: socket.socket):
        """移除并关闭一个客户端连接"""
        with self._lock:
            if client_sock in self._tcp_clients:
                del self._tcp_clients[client_sock]
            self.client_count = len(self._tcp_clients)
        try:
            client_sock.close()
        except Exception:
            pass

    # ── 工具方法 ──

    def _set_state(self, state: BridgeState):
        self.state = state
        if self._state_cb:
            try:
                self._state_cb(self.id, state)
            except Exception:
                pass

    def _log(self, level: str, msg: str):
        if self._log_cb:
            try:
                self._log_cb(level, msg)
            except Exception:
                pass

    def _notify_data(self, direction: str, data: bytes):
        if self._data_cb:
            try:
                self._data_cb(self.id, direction, data)
            except Exception:
                pass


# ── 桥接管理器 ────────────────────────────────────────────────


class BridgeManager:
    """
    管理多个 SerialTCPBridge 实例。
    """

    def __init__(
        self,
        log_cb: Optional[LogCallback] = None,
        state_cb: Optional[StateCallback] = None,
        data_cb: Optional[DataCallback] = None,
        persist_cb: Optional[Callable[[], None]] = None,
    ):
        self._log_cb = log_cb
        self._state_cb = state_cb
        self._data_cb = data_cb
        self._persist_cb = persist_cb
        self._bridges: Dict[int, SerialTCPBridge] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def add_bridge(self, config: BridgeConfig) -> int:
        """添加一个桥接配置，返回 bridge_id"""
        with self._lock:
            bridge_id = self._next_id
            self._next_id += 1
            bridge = SerialTCPBridge(
                bridge_id,
                config,
                self._log_cb,
                self._state_cb,
                self._data_cb,
                self._persist_cb,
            )
            self._bridges[bridge_id] = bridge
            return bridge_id

    def remove_bridge(self, bridge_id: int):
        """移除一个桥接"""
        with self._lock:
            bridge = self._bridges.get(bridge_id)
            if bridge:
                bridge.stop()
                del self._bridges[bridge_id]

    def get_bridge(self, bridge_id: int) -> Optional[SerialTCPBridge]:
        return self._bridges.get(bridge_id)

    def get_bridge_by_tcp_port(self, tcp_port: int) -> Optional[SerialTCPBridge]:
        """按 TCP 端口查找桥接（服务端波特率指令用 serial_port 对应 tcp_port）"""
        for bridge in self._bridges.values():
            if bridge.config.tcp_port == tcp_port:
                return bridge
        return None

    def get_all_bridges(self) -> Dict[int, SerialTCPBridge]:
        return dict(self._bridges)

    def start_all(self):
        """启动所有已启用的桥接"""
        for bridge in self._bridges.values():
            if bridge.config.enabled:
                bridge.start()

    def stop_all(self):
        """停止所有桥接"""
        for bridge in self._bridges.values():
            bridge.stop()

    def start_bridge(self, bridge_id: int):
        bridge = self._bridges.get(bridge_id)
        if bridge:
            bridge.start()

    def stop_bridge(self, bridge_id: int):
        bridge = self._bridges.get(bridge_id)
        if bridge:
            bridge.stop()

    def update_bridge(self, bridge_id: int, config: BridgeConfig):
        bridge = self._bridges.get(bridge_id)
        if bridge:
            bridge.update_config(config)

    def get_all_stats(self) -> Dict[int, dict]:
        return {bid: b.get_stats() for bid, b in self._bridges.items()}

    @staticmethod
    def list_serial_ports() -> List[tuple]:
        """列出所有可用串口，返回 [(device, description, hwid), ...]"""
        ports = serial.tools.list_ports.comports()
        return [(p.device, p.description, p.hwid) for p in ports]

    # ── 配置持久化 ──

    def save_config(self, filepath: str):
        """保存所有桥接配置到 JSON 文件"""
        configs = []
        for bridge in self._bridges.values():
            configs.append(bridge.config.to_dict())
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({"bridges": configs}, f, indent=2, ensure_ascii=False)

    def load_config(self, filepath: str) -> List[BridgeConfig]:
        """从 JSON 文件加载配置，返回配置列表"""
        if not os.path.exists(filepath):
            return []
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        configs = []
        for item in data.get("bridges", []):
            configs.append(BridgeConfig.from_dict(item))
        return configs

"""
heartbeat.py - UDP 心跳/注册客户端

UDP 心跳/注册客户端：
  - 定时向管理服务器发送 UDP 心跳报文（JSON 格式）
  - 心跳报文包含本机模块 ID、服务端模块 ID、消息类型与 MAC 等字段
  - 初始心跳间隔 1 秒；收到服务端 ACK（含 echointerval）后，
    心跳间隔改由服务端下发值接管
  - heartbeat_lost 计数：每发送一次 +1；收到 ACK 归零；
    超过 max_lost_reset 则重置 UDP socket 重新初始化
  - 收到服务端 ERR 则停止

所有模块 ID、消息类型、端口等均为可配置项（见 HeartbeatConfig），
默认值需与管理服务器一侧保持一致。
"""

import socket
import threading
import time
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional


# ── 配置数据类 ────────────────────────────────────────────────


@dataclass
class HeartbeatConfig:
    """UDP 心跳配置。

    协议常量默认已硬编码；如需自定义，界面里仍全部可改。
    """

    enabled: bool = False
    server_ip: str = "192.168.96.248"  # 管理服务器 IP
    server_port: int = 7777  # 管理服务器端口
    macaddr: str = "6892D0111128"  # 本机 MAC 标识
    client_module_id: int = 1  # 本机模块 ID
    server_module_id: int = 3  # 服务端模块 ID
    msgtype_heartbeat: int = 0  # 心跳消息类型
    msgtype_ack: int = 0  # 确认消息类型
    msgtype_err: int = -1  # 错误消息类型
    msgtype_report_ser2net: int = 13  # 波特率切换结果上报消息类型
    ser2net_module_id: int = 6  # 结果回包的模块 ID
    interval: float = 1.0  # 初始心跳间隔(秒)，收到 ACK 后被 echointerval 覆盖
    max_lost_reset: int = 30  # heartbeat_lost > 30 重置 socket
    local_port: int = 9999  # 本地端口（仅 bind_local_port=True 时绑定）
    bind_local_port: bool = False  # 默认不绑定本地端口，使用随机临时端口

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HeartbeatConfig":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid}
        return cls(**filtered)


# ── 回调类型 ──────────────────────────────────────────────────

HeartbeatLogCallback = Callable[[str, str], None]  # (level, message)
HeartbeatStateCallback = Callable[[str], None]  # (state_text)
# 服务端下发的波特率修改指令回调：(tcp_port, baudrate, info)
#   tcp_port: 对应桥接的 tcp_port（如 9001）
#   baudrate: 目标波特率（如 9600）
#   info:     附加信息 {ttycode, serial_code, obaudrate}
HeartbeatBaudrateCallback = Callable[[int, int, dict], None]


class HeartbeatClient:
    """UDP 心跳客户端，基于定时器驱动的状态机"""

    def __init__(
        self,
        config: HeartbeatConfig,
        log_cb: Optional[HeartbeatLogCallback] = None,
        state_cb: Optional[HeartbeatStateCallback] = None,
        baudrate_cb: Optional[HeartbeatBaudrateCallback] = None,
    ):
        self.config = config
        self._log_cb = log_cb
        self._state_cb = state_cb
        self._baudrate_cb = baudrate_cb

        self._sock: Optional[socket.socket] = None
        self._running = False
        self._heartbeat_lost = -1
        self._interval = config.interval
        self._send_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None
        self.state = "已停止"

    # ── 公共接口 ──

    def start(self):
        if self._running:
            return
        if not self.config.enabled:
            self._log("warn", "心跳未启用（enabled=false），已跳过")
            return
        if not self.config.server_ip:
            self._log("error", "心跳服务器地址为空，无法启动")
            return

        self._running = True
        self._heartbeat_lost = -1
        self._interval = self.config.interval
        self._init_socket()

        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="hb-send"
        )
        self._send_thread.start()
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="hb-recv"
        )
        self._recv_thread.start()

        self._set_state("运行中")
        self._log(
            "info",
            f"心跳客户端已启动，目标 {self.config.server_ip}:{self.config.server_port} "
            f"(mac={self.config.macaddr})",
        )

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._set_state("已停止")
        self._log("info", "心跳客户端已停止")

    # ── 内部实现 ──

    def _init_socket(self):
        """创建/重建 UDP socket

        默认不绑定本地端口，使用随机临时端口。
        若 bind_local_port=True 则绑定 local_port。
        """
        try:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(1.0)
            if self.config.bind_local_port and self.config.local_port > 0:
                self._sock.bind(("0.0.0.0", self.config.local_port))
                self._log(
                    "info", f"心跳 socket 已绑定本地端口 {self.config.local_port}"
                )
        except Exception as e:
            self._log("error", f"心跳 socket 创建失败: {e}")
            self._sock = None

    def _send_loop(self):
        """心跳发送循环（按当前间隔定时触发）"""
        while self._running:
            if self._sock and self.config.enabled:
                pkt = {
                    "src_moduleid": self.config.client_module_id,
                    "dst_moduleid": self.config.server_module_id,
                    "msgtype": self.config.msgtype_heartbeat,
                    "data": {
                        "macaddr": self.config.macaddr,
                        "heartbeat_lost": self._heartbeat_lost,
                    },
                }
                try:
                    msg = json.dumps(pkt, separators=(",", ":")).encode("utf-8")
                    self._sock.sendto(
                        msg, (self.config.server_ip, self.config.server_port)
                    )
                    self._log("debug", f"心跳发送: {msg.decode('utf-8', 'ignore')}")
                except Exception as e:
                    self._log("error", f"心跳发送失败: {e}")

                self._heartbeat_lost += 1

                # heartbeat_lost > 30 时重置 udp socket
                if self._heartbeat_lost > self.config.max_lost_reset:
                    self._log(
                        "warn", f"心跳丢失 {self._heartbeat_lost} 次，重置 UDP socket"
                    )
                    self._init_socket()
                    self._heartbeat_lost = 0

            time.sleep(self._interval)

    def _recv_loop(self):
        """接收服务端回包"""
        while self._running:
            if not self._sock:
                time.sleep(0.5)
                continue
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    time.sleep(0.5)
                continue
            self._handle_server_msg(data)

    def _handle_server_msg(self, data: bytes):
        """处理服务端消息：ACK 更新间隔；ERR 停止"""
        try:
            msg = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            self._log("debug", f"收到非 JSON 心跳回包: {data[:64]}")
            return

        msgtype = msg.get("msgtype")
        payload = msg.get("data", {})

        if msgtype == self.config.msgtype_ack:
            if isinstance(payload, dict):
                echointerval = payload.get("echointerval")
                maxretry = payload.get("maxretry")
                if echointerval:
                    self._interval = float(echointerval)
                if maxretry is not None:
                    self._log(
                        "info",
                        f"服务端已确认心跳，间隔更新为 {self._interval}s (maxretry={maxretry})",
                    )
                else:
                    self._log("info", "服务端已确认心跳")
            else:
                self._log("info", "服务端已确认心跳")
            self._heartbeat_lost = 0

        elif msgtype == self.config.msgtype_err:
            self._log("error", "管理服务器拒绝访问，停止心跳")
            self.stop()

        else:
            # 扩展点：服务端下发的其他指令（如远端波特率配置）在此处理
            self._log("debug", f"收到服务端消息: {msg}")
            self._try_apply_baudrate_command(msg)

    def _try_apply_baudrate_command(self, msg: dict):
        """解析服务端下发的波特率修改指令并转发给桥接模块。

        服务器报文示例（msgtype 一般为控制类，如 1025）：
            {
              "ttycode": "ttyUSB0", "serial_code": "Tatan",
              "baudrate": 9600, "obaudrate": 115200,
              "serial_port": 9001, "dst_moduleid": 6,
              "msgtype": 1025, "data": {}
            }
        baudrate / serial_port 可能在消息顶层，也可能嵌套在 data 中，两种位置都兼容。
        serial_port 对应桥接的 tcp_port（如 9001 → 该 TCP 端口的桥接）。
        """
        # 兼容：字段可能在顶层，也可能在 data 里
        data = msg.get("data")
        if not isinstance(data, dict):
            data = {}
        merged = dict(data)
        merged.update({k: v for k, v in msg.items() if k != "data"})

        baudrate = merged.get("baudrate")
        tcp_port = merged.get("serial_port")
        if baudrate is None or tcp_port is None:
            return  # 不是波特率指令，忽略

        try:
            baudrate = int(baudrate)
            tcp_port = int(tcp_port)
        except (TypeError, ValueError):
            self._log(
                "warn",
                f"波特率指令字段非法: baudrate={baudrate}, serial_port={tcp_port}",
            )
            return

        if baudrate <= 0:
            self._log("warn", f"收到非法波特率指令: {baudrate}")
            return

        info = {
            "ttycode": merged.get("ttycode"),
            "serial_code": merged.get("serial_code"),
            "obaudrate": merged.get("obaudrate"),
        }
        if self._baudrate_cb:
            self._baudrate_cb(tcp_port, baudrate, info)
            self._log(
                "info",
                f"已转发服务端波特率指令 → TCP端口 {tcp_port} 设为 {baudrate} bps "
                f"(ttycode={info['ttycode']})",
            )
            # 收到服务端波特率修改指令后，必须回发 msgtype=13 结果上报包，
            # 构造回送报文（含 macaddr）后经 UDP 回送服务端
            self._send_ser2net_report(baudrate, info)
        else:
            self._log("warn", "收到服务端波特率指令，但未配置桥接回调，已忽略且不回包")

    def _send_ser2net_report(self, baudrate: int, info: dict):
        """收到服务端 msgtype:1025 波特率指令并完成切换后，回发结果上报包（msgtype=13）。

        上报包含切换后的波特率、原始波特率、设备代号与本机 MAC，经 UDP 回送服务端。
        """
        pkt = {
            "src_moduleid": self.config.ser2net_module_id,  # 结果回包模块 ID
            "dst_moduleid": self.config.server_module_id,  # 服务端模块 ID
            "msgtype": self.config.msgtype_report_ser2net,  # 13
            "data": {
                "baudrate": baudrate,  # 切换后的新波特率
                "obaudrate": info.get("obaudrate"),  # 原始波特率
                "serial_code": info.get("serial_code"),  # 透传设备代号
                "macaddr": self.config.macaddr,  # 本机 MAC
            },
        }
        self._send_pkt(pkt)

    def _send_pkt(self, pkt: dict):
        """经 UDP 向服务端发送一个报文"""
        if not self._sock:
            self._log("warn", "心跳 socket 未就绪，无法发送回包")
            return
        try:
            msg = json.dumps(pkt, separators=(",", ":")).encode("utf-8")
            self._sock.sendto(msg, (self.config.server_ip, self.config.server_port))
            self._log("debug", f"已回包服务端: {msg.decode('utf-8', 'ignore')}")
        except Exception as e:
            self._log("error", f"回包发送失败: {e}")

    # ── 工具 ──

    def _set_state(self, state: str):
        self.state = state
        if self._state_cb:
            try:
                self._state_cb(state)
            except Exception:
                pass

    def _log(self, level: str, msg: str):
        if self._log_cb:
            try:
                self._log_cb(level, msg)
            except Exception:
                pass


# ── 配置持久化 ────────────────────────────────────────────────


def save_heartbeat_config(cfg: HeartbeatConfig, filepath: str):
    """把心跳配置写入 JSON 文件（与桥接配置合并存储时由调用方拼装）"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"heartbeat": cfg.to_dict()}, f, indent=2, ensure_ascii=False)


def load_heartbeat_config(filepath: str) -> Optional[HeartbeatConfig]:
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    hb = data.get("heartbeat")
    if not hb:
        return None
    return HeartbeatConfig.from_dict(hb)

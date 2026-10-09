"""
main.py - USB2Net 桌面应用

基于 tkinter/ttk 的串口转网络工具 GUI。
将串口网络服务、心跳注册与 Web 终端功能封装为友好的桌面应用。

功能：
  - 可视化配置串口↔TCP 网络映射（支持多条）
  - 实时状态监控 & 数据日志
  - 串口热插拔自动重连
  - 配置保存/加载
  - 统计信息（收发字节数、连接数、运行时间）
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import queue
import time
import os
import sys
import json
import shutil
import re
import socket
from datetime import datetime

# 确定应用目录（兼容 PyInstaller 打包）
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP_DIR)


def _resource_path(rel):
    """定位资源文件：打包后在 PyInstaller 释放目录 _MEIPASS，开发时在同目录。"""
    base = getattr(sys, "_MEIPASS", _APP_DIR)
    return os.path.join(base, rel)


try:
    from bridge import BridgeManager, BridgeConfig, BridgeState
    from heartbeat import HeartbeatConfig, HeartbeatClient, GetCmdConfig
    from webterm import WebTerminalHub, open_browser
    from single_instance import SingleInstance, raise_window
except ImportError as _imp_err:
    # 依赖缺失（如 pyserial 未装 / 用了错误的 Python）：弹窗提示而非静默闪退
    _need = (
        "pyserial"
        if "serial" in str(_imp_err)
        else (
            "websockets"
            if "websockets" in str(_imp_err) or "webterm" in str(_imp_err)
            else "所需模块"
        )
    )
    try:
        import tkinter as tk
        from tkinter import messagebox

        _root = tk.Tk()
        _root.withdraw()
        messagebox.showerror(
            "缺少依赖",
            f"无法导入所需模块:\n{_imp_err}\n\n"
            f"通常是因为 {_need} 未安装，或当前用来打开此文件的 Python 不正确。\n\n"
            f"👉 解决办法: 请双击本目录下的 run.bat 启动\n"
            f"（它会自动检查并安装依赖），\n"
            f"或在命令行执行: pip install pyserial websockets",
        )
        _root.destroy()
    except Exception:
        pass
    raise SystemExit(1)


# ── 颜色 & 样式常量 ───────────────────────────────────────────

COLOR_BG = "#f0f2f5"
COLOR_CARD = "#ffffff"
COLOR_PRIMARY = "#2563eb"
COLOR_PRIMARY_DARK = "#1d4ed8"
COLOR_PRIMARY_LIGHT = "#dbeafe"
COLOR_HEADER = "#1a1f36"
COLOR_HEADER_ACCENT = "#3b82f6"
COLOR_SUCCESS = "#16a34a"
COLOR_SUCCESS_LIGHT = "#dcfce7"
COLOR_WARNING = "#f59e0b"
COLOR_WARNING_LIGHT = "#fef3c7"
COLOR_ERROR = "#dc2626"
COLOR_ERROR_LIGHT = "#fee2e2"
COLOR_TEXT = "#1f2937"
COLOR_TEXT_SEC = "#6b7280"
COLOR_TEXT_MUTED = "#9ca3af"
COLOR_BORDER = "#e5e7eb"
COLOR_BORDER_LIGHT = "#f3f4f6"
COLOR_ROW_ALT = "#fafbfc"
COLOR_ROW_HOVER = "#eff6ff"
COLOR_LOG_BG = "#1a1b26"
COLOR_LOG_FG = "#c0caf5"
COLOR_STATUS_BG = "#f8f9fa"

# 侧栏（深色导航）
COLOR_SIDEBAR = "#1e293b"
COLOR_SIDEBAR_HOVER = "#334155"
COLOR_SIDEBAR_TEXT = "#cbd5e1"
COLOR_SIDEBAR_MUTED = "#64748b"

# 统计卡片底色
COLOR_TINT_BLUE = "#eff6ff"
COLOR_TINT_GREEN = "#ecfdf5"
COLOR_TINT_ORANGE = "#fff7ed"
COLOR_TINT_PURPLE = "#faf5ff"

APP_VERSION = "USB2Net V2.1"

# IP上报 功能入口密码（固定，输入正确才能进入设置）
GETCMD_PASSWORD = "9527"


def _get_local_ip():
    """获取本机局域网 IP 地址（通过 UDP connect 探测，不发实际数据）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# 状态 → 颜色 & 文字（● 前缀营造状态灯效果）
STATE_STYLE = {
    BridgeState.STOPPED: ("#9e9e9e", "● 已停止"),
    BridgeState.STARTING: ("#2196f3", "● 启动中"),
    BridgeState.RUNNING: ("#4caf50", "● 运行中"),
    BridgeState.ERROR: ("#f44336", "● 错误"),
    BridgeState.WAITING: ("#ff9800", "● 等待设备"),
}

# 状态→简化文字（用于状态栏/统计）
STATE_TEXT = {
    BridgeState.STOPPED: "已停止",
    BridgeState.STARTING: "启动中",
    BridgeState.RUNNING: "运行中",
    BridgeState.ERROR: "错误",
    BridgeState.WAITING: "等待设备",
}


class AddEditBridgeDialog(tk.Toplevel):
    """添加/编辑串口配置的弹窗"""

    def __init__(self, parent, config: BridgeConfig = None, title="添加串口"):
        super().__init__(parent)
        self.title("USB2Net")
        self._title = title
        self.result = None
        self._config = config or BridgeConfig()

        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self._build_ui()
        self._load_values()

        # 居中
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build_ui(self):
        # 顶部标题带
        banner = tk.Frame(self, bg=COLOR_PRIMARY, height=40)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(
            banner,
            text=self._title,
            bg=COLOR_PRIMARY,
            fg="white",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16)

        main = ttk.Frame(self, padding=(16, 14))
        main.pack(fill="both", expand=True)

        # 串口设置
        serial_frame = ttk.LabelFrame(main, text=" 串口设置 ", padding=(10, 6))
        serial_frame.pack(fill="x", pady=(0, 10))
        self._build_field_grid(
            serial_frame,
            [
                ("名称", "name", "entry", {}),
                ("串口", "serial_port", "combo", {"values_func": self._get_ports}),
                (
                    "波特率",
                    "baudrate",
                    "combo",
                    {
                        "values": [
                            "9600",
                            "19200",
                            "38400",
                            "57600",
                            "115200",
                            "230400",
                            "460800",
                            "921600",
                        ]
                    },
                ),
                ("数据位", "databits", "combo", {"values": ["5", "6", "7", "8"]}),
                (
                    "校验位",
                    "parity",
                    "combo",
                    {"values": ["N (无)", "E (偶)", "O (奇)", "S (空)", "M (标记)"]},
                ),
                ("停止位", "stopbits", "combo", {"values": ["1", "1.5", "2"]}),
                (
                    "流控",
                    "flow_control",
                    "combo",
                    {"values": ["none", "xonxoff", "rtscts"]},
                ),
            ],
        )

        # 网络设置
        net_frame = ttk.LabelFrame(main, text=" 网络设置 ", padding=(10, 6))
        net_frame.pack(fill="x", pady=(0, 10))
        self._build_field_grid(
            net_frame,
            [
                ("TCP 端口", "tcp_port", "entry", {}),
                ("Web 端口", "web_port", "entry", {}),
            ],
        )

        # 复选框
        cb_frame = ttk.Frame(main)
        cb_frame.pack(fill="x", pady=(0, 8))

        self._enabled_var = tk.BooleanVar()
        ttk.Checkbutton(cb_frame, text="启用", variable=self._enabled_var).pack(
            side="left", padx=(0, 20)
        )

        self._auto_reconn_var = tk.BooleanVar()
        ttk.Checkbutton(
            cb_frame, text="自动重连（热插拔）", variable=self._auto_reconn_var
        ).pack(side="left", padx=(0, 20))

        self._telnet_var = tk.BooleanVar()
        ttk.Checkbutton(cb_frame, text="Telnet 模式", variable=self._telnet_var).pack(
            side="left"
        )

        # 按钮行
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(12, 0))
        ttk.Button(
            btn_frame, text="取消", command=self._on_cancel, style="Ghost.TButton"
        ).pack(side="right")
        ttk.Button(
            btn_frame, text="确定", command=self._on_ok, style="Primary.TButton"
        ).pack(side="right", padx=(0, 8))

    def _build_field_grid(self, parent, fields):
        """在 parent 内按 2 列网格生成字段控件，结果存入 self._widgets[attr]。"""
        if not hasattr(self, "_widgets"):
            self._widgets = {}
        for i, (label, attr, wtype, opts) in enumerate(fields):
            ttk.Label(parent, text=label, width=10).grid(
                row=i, column=0, sticky="w", pady=4, padx=(0, 10)
            )
            if wtype == "entry":
                widget = ttk.Entry(parent, width=28)
            elif wtype == "combo":
                if "values_func" in opts:
                    values = opts["values_func"]()
                    widget = ttk.Combobox(
                        parent, width=25, values=values, state="readonly"
                    )
                else:
                    widget = ttk.Combobox(
                        parent, width=25, values=opts.get("values", [])
                    )
                    if attr in (
                        "baudrate",
                        "databits",
                        "stopbits",
                        "parity",
                        "flow_control",
                    ):
                        widget.configure(state="readonly")
            widget.grid(row=i, column=1, sticky="w", pady=4)
            self._widgets[attr] = widget

    def _get_ports(self):
        ports = BridgeManager.list_serial_ports()
        return [p[0] for p in ports] if ports else []

    def _load_values(self):
        c = self._config
        self._widgets["name"].insert(0, c.name)
        self._widgets["serial_port"].set(c.serial_port)
        self._widgets["baudrate"].set(str(c.baudrate))
        self._widgets["databits"].set(str(c.databits))
        # parity 显示带说明
        parity_display = {
            "N": "N (无)",
            "E": "E (偶)",
            "O": "O (奇)",
            "S": "S (空)",
            "M": "M (标记)",
        }
        self._widgets["parity"].set(parity_display.get(c.parity, "N (无)"))
        self._widgets["stopbits"].set(str(c.stopbits))
        self._widgets["flow_control"].set(c.flow_control)
        self._widgets["tcp_port"].insert(0, str(c.tcp_port))
        self._widgets["web_port"].insert(0, str(c.web_port) if c.web_port else "")
        self._enabled_var.set(c.enabled)
        self._auto_reconn_var.set(c.auto_reconnect)
        self._telnet_var.set(c.telnet_mode)

    def _on_ok(self):
        try:
            parity_raw = self._widgets["parity"].get()
            parity_val = parity_raw[0] if parity_raw else "N"

            self.result = BridgeConfig(
                name=self._widgets["name"].get().strip() or "Bridge",
                serial_port=self._widgets["serial_port"].get().strip(),
                baudrate=int(self._widgets["baudrate"].get()),
                databits=int(self._widgets["databits"].get()),
                parity=parity_val,
                stopbits=float(self._widgets["stopbits"].get()),
                flow_control=self._widgets["flow_control"].get(),
                tcp_port=int(self._widgets["tcp_port"].get()),
                web_port=int(self._widgets["web_port"].get().strip() or 0),
                enabled=self._enabled_var.get(),
                auto_reconnect=self._auto_reconn_var.get(),
                telnet_mode=self._telnet_var.get(),
            )
            self.destroy()
        except ValueError as e:
            messagebox.showerror("输入错误", f"请检查输入值：{e}", parent=self)

    def _on_cancel(self):
        self.destroy()


class HeartbeatSettingsDialog(tk.Toplevel):
    """心跳配置弹窗。所有协议常量可配置以对齐管理服务器。"""

    def __init__(self, parent, config: HeartbeatConfig):
        super().__init__(parent)
        self.title("USB2Net")
        self._title = "心跳设置"
        self.result = None
        self._config = config

        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self._build_ui()
        self._load_values()

        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build_ui(self):
        banner = tk.Frame(self, bg=COLOR_PRIMARY, height=40)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(
            banner,
            text=self._title,
            bg=COLOR_PRIMARY,
            fg="white",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16)

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        # 设置列权重，让内容自适应
        main.columnconfigure(0, weight=2)  # 标签列
        main.columnconfigure(1, weight=3)  # 输入框列

        ttk.Label(
            main,
            text="通过 UDP 与远程服务器通信，实现消息转发和心跳保活",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            main,
            text="作为服务端监听本地模块连接，接收本地模块发往服务器的消息。\n"
            "作为客户端与远程服务器通信，发送本地模块消息并接收服务器响应",
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 8),
            wraplength=500,  # 添加自动换行
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        fields = [
            ("服务器 IP 地址", "server_ip", "str"),
            ("服务器 UDP 端口", "server_port", "int"),
            ("设备 MAC 地址", "macaddr", "str"),
            ("本地模块 ID（发送方）", "client_module_id", "int"),
            ("服务端模块 ID（接收方）", "server_module_id", "int"),
            ("心跳请求消息类型值", "msgtype_heartbeat", "int"),
            ("心跳确认消息类型值", "msgtype_ack", "int"),
            ("错误响应消息类型值", "msgtype_err", "int"),
            ("首次心跳超时间隔（秒）", "interval", "float"),
            ("心跳丢失重置阈值", "max_lost_reset", "int"),
            ("本地绑定 UDP 端口", "local_port", "int"),
        ]

        self._widgets = {}
        for i, (label, attr, vtype) in enumerate(fields):
            r = i + 2
            # 移除固定width，使用 sticky="w" 让标签左对齐
            ttk.Label(main, text=label, anchor="w").grid(
                row=r, column=0, sticky="w", pady=3, padx=(0, 8)
            )
            widget = ttk.Entry(main, width=30)  # 输入框宽度稍微加大
            widget.grid(row=r, column=1, sticky="w", pady=3)
            self._widgets[attr] = (widget, vtype)

        # 启用复选框
        cb_frame = ttk.Frame(main)
        cb_frame.grid(
            row=len(fields) + 2, column=0, columnspan=2, pady=(10, 0), sticky="w"
        )
        self._enabled_var = tk.BooleanVar()
        ttk.Checkbutton(cb_frame, text="启用心跳", variable=self._enabled_var).pack(
            side="left"
        )
        self._bind_local_var = tk.BooleanVar()
        ttk.Checkbutton(
            cb_frame, text="绑定本地端口(默认关)", variable=self._bind_local_var
        ).pack(side="left", padx=(10, 0))

        # 按钮行
        btn_frame = ttk.Frame(main)
        btn_frame.grid(
            row=len(fields) + 3, column=0, columnspan=2, pady=(12, 0), sticky="e"
        )
        ttk.Button(
            btn_frame, text="取消", command=self._on_cancel, style="Ghost.TButton"
        ).pack(side="right")
        ttk.Button(
            btn_frame, text="确定", command=self._on_ok, style="Primary.TButton"
        ).pack(side="right", padx=(0, 8))

    def _load_values(self):
        c = self._config
        for attr, (widget, vtype) in self._widgets.items():
            widget.insert(0, str(getattr(c, attr)))
        self._enabled_var.set(c.enabled)
        self._bind_local_var.set(c.bind_local_port)

    def _on_ok(self):
        try:
            vals = {}
            for attr, (widget, vtype) in self._widgets.items():
                raw = widget.get().strip()
                if vtype == "int":
                    vals[attr] = int(raw)
                elif vtype == "float":
                    vals[attr] = float(raw)
                else:
                    vals[attr] = raw
            vals["bind_local_port"] = self._bind_local_var.get()
            # 基于现有配置更新可见字段，保留 local_port/bind_local_port 等全部字段
            merged = {
                **self._config.to_dict(),
                "enabled": self._enabled_var.get(),
                **vals,
            }
            self.result = HeartbeatConfig(**merged)
            self.destroy()
        except ValueError as e:
            messagebox.showerror("输入错误", f"请检查数值输入：{e}", parent=self)

    def _on_cancel(self):
        self.destroy()


class PasswordDialog(tk.Toplevel):
    """IP上报 入口密码验证弹窗。输入正确返回 True，否则不进入设置。"""

    def __init__(self, parent, password: str, title="身份验证"):
        super().__init__(parent)
        self.title("USB2Net")
        self._title = title
        self._password = password
        self.verified = False

        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self._build_ui()

        # 居中
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        # 焦点定位到输入框，回车即验证
        self._ent_pwd.focus_set()
        self.bind("<Return>", lambda e: self._on_ok())

    def _build_ui(self):
        banner = tk.Frame(self, bg=COLOR_PRIMARY, height=40)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(
            banner,
            text=self._title,
            bg=COLOR_PRIMARY,
            fg="white",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16)

        main = ttk.Frame(self, padding=(20, 16))
        main.pack(fill="both", expand=True)

        ttk.Label(
            main,
            text="🔒 IP上报 为受保护功能，请输入访问密码",
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(main, text="密码", width=8).grid(
            row=1, column=0, sticky="w", padx=(0, 8)
        )
        self._ent_pwd = ttk.Entry(main, width=24, show="*")
        self._ent_pwd.grid(row=1, column=1, sticky="w")

        # 错误提示（默认隐藏）
        self._err_label = ttk.Label(
            main, text="", foreground=COLOR_ERROR, font=("Segoe UI", 9)
        )
        self._err_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=(10, 0), sticky="e")
        ttk.Button(
            btn_frame, text="取消", command=self._on_cancel, style="Ghost.TButton"
        ).pack(side="right")
        ttk.Button(
            btn_frame, text="确认", command=self._on_ok, style="Primary.TButton"
        ).pack(side="right", padx=(0, 8))

    def _on_ok(self):
        if self._ent_pwd.get() == self._password:
            self.verified = True
            self.destroy()
        else:
            self._err_label.config(text="✕ 密码错误，请重试")
            self._ent_pwd.delete(0, "end")
            self._ent_pwd.focus_set()

    def _on_cancel(self):
        self.destroy()


class GetCmdDialog(tk.Toplevel):
    """IP上报 (GetCmd) 弹窗。

    按需发送一次 GetCmd 指令包（UDP），独立于心跳循环。
    对应设备端 C 代码的 comm_send_getcmd() + comm_sendpkt()。
    字段名与 C 代码完全一致，msgtype/模块ID 独立可配置（不复用心跳配置）。
    仅复用心跳配置中的 server_ip/server_port/macaddr。
    """

    def __init__(
        self, parent, getcmd_config: GetCmdConfig, hb_config: HeartbeatConfig, send_cb
    ):
        super().__init__(parent)
        self.title("USB2Net")
        self._title = "IP上报"
        self._config = getcmd_config  # GetCmd 独立配置（字段无前缀）
        self._hb_config = hb_config  # 心跳配置（仅用于显示服务器/MAC 复用信息）
        # callback(version, ipaddr, mainstep, substep, msgtype, src_moduleid, dst_moduleid)
        self._send_cb = send_cb

        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self._build_ui()
        self._load_values()

        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build_ui(self):
        banner = tk.Frame(self, bg=COLOR_PRIMARY, height=40)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(
            banner,
            text=self._title,
            bg=COLOR_PRIMARY,
            fg="white",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16)

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        # 设置列权重
        main.columnconfigure(0, weight=2)  # 标签列
        main.columnconfigure(1, weight=3)  # 输入框列
        main.columnconfigure(2, weight=1)  # 按钮列

        ttk.Label(
            main,
            text="向服务器发送'获取命令'请求，告知服务器当前设备的状态信息，并请求服务器下发指令。",
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 8),
            wraplength=500,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # ── 可编辑字段 ──
        # 修改点1: 移除 width 限制，使用 anchor="w"
        ttk.Label(main, text="程序版本号", anchor="w").grid(
            row=1, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_version = ttk.Entry(main, width=28)
        self._ent_version.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Button(main, text="填充软件版本", command=self._fill_version).grid(
            row=1, column=2, padx=(8, 0), pady=3
        )

        ttk.Label(main, text="设备 IP 地址", anchor="w").grid(
            row=2, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        ip_frame = ttk.Frame(main)
        ip_frame.grid(row=2, column=1, columnspan=2, sticky="w", pady=3)
        self._ent_ipaddr = ttk.Entry(ip_frame, width=20)
        self._ent_ipaddr.pack(side="left")
        ttk.Button(ip_frame, text="获取本机IP", command=self._fetch_local_ip).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(main, text="设备主步骤", anchor="w").grid(
            row=3, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_mainstep = ttk.Entry(main, width=28)
        self._ent_mainstep.grid(row=3, column=1, sticky="w", pady=3)

        ttk.Label(main, text="设备子步骤", anchor="w").grid(
            row=4, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_substep = ttk.Entry(main, width=28)
        self._ent_substep.grid(row=4, column=1, sticky="w", pady=3)

        ttk.Label(main, text="消息类型值", anchor="w").grid(
            row=5, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_msgtype = ttk.Entry(main, width=28)
        self._ent_msgtype.grid(row=5, column=1, sticky="w", pady=3)

        ttk.Label(main, text="本地模块 ID（发送方）", anchor="w").grid(
            row=6, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_src_moduleid = ttk.Entry(main, width=28)
        self._ent_src_moduleid.grid(row=6, column=1, sticky="w", pady=3)

        ttk.Label(main, text="服务端模块 ID（接收方）", anchor="w").grid(
            row=7, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self._ent_dst_moduleid = ttk.Entry(main, width=28)
        self._ent_dst_moduleid.grid(row=7, column=1, sticky="w", pady=3)

        # ── 只读复用字段（来自心跳配置）──
        ttk.Separator(main, orient="horizontal").grid(
            row=8, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )

        ro_frame = ttk.Frame(main)
        ro_frame.grid(row=9, column=0, columnspan=3, sticky="w")

        c = self._hb_config
        ro_fields = [
            ("服务器 IP 地址", f"{c.server_ip}:{c.server_port}"),
            ("设备 MAC 地址", c.macaddr),
        ]
        for i, (label, value) in enumerate(ro_fields):
            ttk.Label(ro_frame, text=label, anchor="w", foreground=COLOR_TEXT_SEC).grid(
                row=i, column=0, sticky="w", padx=(0, 8), pady=1
            )
            ttk.Label(ro_frame, text=value).grid(row=i, column=1, sticky="w", pady=1)

        # ── 按钮行 ──
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=10, column=0, columnspan=3, pady=(12, 0), sticky="e")
        ttk.Button(
            btn_frame, text="关闭", command=self._on_close, style="Ghost.TButton"
        ).pack(side="right")
        ttk.Button(
            btn_frame,
            text="发送",
            command=self._on_send,
            style="Primary.TButton",
        ).pack(side="right", padx=(0, 8))

    def _load_values(self):
        c = self._config
        # version: 用保存的值或软件版本
        self._ent_version.insert(0, c.version or APP_VERSION)
        # ipaddr: 用保存的值或空（用户手动获取）
        self._ent_ipaddr.insert(0, c.ipaddr or "")
        self._ent_mainstep.insert(0, str(c.mainstep))
        self._ent_substep.insert(0, str(c.substep))
        self._ent_msgtype.insert(0, str(c.msgtype))
        self._ent_src_moduleid.insert(0, str(c.src_moduleid))
        self._ent_dst_moduleid.insert(0, str(c.dst_moduleid))

    def _fill_version(self):
        self._ent_version.delete(0, "end")
        self._ent_version.insert(0, APP_VERSION)

    def _fetch_local_ip(self):
        ip = _get_local_ip()
        self._ent_ipaddr.delete(0, "end")
        self._ent_ipaddr.insert(0, ip)

    def _save_fields_to_config(self):
        """把当前输入框值写回 config（供发送和关闭时调用）"""
        self._config.version = self._ent_version.get().strip()
        self._config.ipaddr = self._ent_ipaddr.get().strip()
        try:
            self._config.mainstep = int(self._ent_mainstep.get().strip() or "0")
            self._config.substep = int(self._ent_substep.get().strip() or "0")
            self._config.msgtype = int(self._ent_msgtype.get().strip() or "4")
            self._config.src_moduleid = int(self._ent_src_moduleid.get().strip() or "1")
            self._config.dst_moduleid = int(self._ent_dst_moduleid.get().strip() or "3")
        except ValueError:
            pass  # 关闭时容忍非法输入

    def _on_send(self):
        try:
            version = self._ent_version.get().strip()
            ipaddr = self._ent_ipaddr.get().strip()
            mainstep = int(self._ent_mainstep.get().strip() or "0")
            substep = int(self._ent_substep.get().strip() or "0")
            msgtype = int(self._ent_msgtype.get().strip() or "4")
            src_moduleid = int(self._ent_src_moduleid.get().strip() or "1")
            dst_moduleid = int(self._ent_dst_moduleid.get().strip() or "3")
        except ValueError as e:
            messagebox.showerror("输入错误", f"数字字段必须为整数：{e}", parent=self)
            return

        if not ipaddr:
            if not messagebox.askyesno(
                "确认",
                "IP 地址为空，确定要发送吗？",
                parent=self,
            ):
                return

        # 保存当前值到配置
        self._save_fields_to_config()

        # 调用发送回调（不关闭弹窗，可连续发送）
        self._send_cb(
            version, ipaddr, mainstep, substep, msgtype, src_moduleid, dst_moduleid
        )

    def _on_close(self):
        self._save_fields_to_config()
        self.destroy()


class Toast:
    """轻提示（Toast）管理类"""

    _toast_window = None
    _hide_timer = None

    @classmethod
    def show(cls, parent, message, duration=2000, is_success=True):
        """显示轻提示 - 居中显示"""
        # 关闭已有的toast
        if cls._toast_window and cls._toast_window.winfo_exists():
            cls._toast_window.destroy()
        if cls._hide_timer:
            parent.after_cancel(cls._hide_timer)

        cls._toast_window = tk.Toplevel(parent)
        cls._toast_window.overrideredirect(True)
        cls._toast_window.configure(bg=COLOR_SUCCESS if is_success else COLOR_ERROR)
        cls._toast_window.attributes("-topmost", True)

        # 添加图标
        icon = "✓ " if is_success else "✕ "
        tk.Label(
            cls._toast_window,
            text=icon + message,
            bg=COLOR_SUCCESS if is_success else COLOR_ERROR,
            fg="white",
            font=("Segoe UI", 12, "bold"),
            padx=30,
            pady=16,
        ).pack()

        # 强制更新布局以获取实际尺寸
        cls._toast_window.update_idletasks()

        # 获取父窗口的位置和尺寸
        parent.update_idletasks()
        px = parent.winfo_x()
        py = parent.winfo_y()
        pw = parent.winfo_width()
        ph = parent.winfo_height()

        # 计算 Toast 自身尺寸
        tw = cls._toast_window.winfo_reqwidth()
        th = cls._toast_window.winfo_reqheight()

        # 居中于父窗口
        x = px + (pw - tw) // 2
        y = py + (ph - th) // 2 - 30  # 稍微偏上一点更自然

        cls._toast_window.geometry(f"+{x}+{y}")

        # 自动隐藏
        cls._hide_timer = parent.after(duration, lambda: cls._hide(parent))

    @classmethod
    def _hide(cls, parent):
        if cls._toast_window and cls._toast_window.winfo_exists():
            cls._toast_window.destroy()
            cls._toast_window = None
        cls._hide_timer = None


class USB2NetApp:
    """主应用窗口"""

    CONFIG_FILE = os.path.join(_APP_DIR, "config.json")

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("USB2Net - 网络串口服务器")
        self.root.geometry("1000x680")
        self.root.minsize(880, 560)
        self.root.configure(bg=COLOR_BG)

        # 运行窗口的标题栏/任务栏图标：exe 文件图标由 USB2Net.spec 的 icon= 设置，
        # 但 tkinter 窗口自身默认显示 Tk 的羽毛图标，需在此显式指定本应用图标。
        # 优先用 PNG + iconphoto（兼容性最好），失败再用 .ico + iconbitmap。
        try:
            _png = _resource_path("USB2Net.png")
            if os.path.exists(_png):
                self.root.iconphoto(True, tk.PhotoImage(file=_png))
            else:
                _ico = _resource_path("USB2Net.ico")
                if os.path.exists(_ico):
                    self.root.iconbitmap(_ico)
        except Exception:
            pass

        # 顶栏 logo（缩小显示）
        try:
            _logo = _resource_path("USB2Net.png")
            if os.path.exists(_logo):
                _img = tk.PhotoImage(file=_logo)
                self._header_logo = _img.subsample(8, 8)
            else:
                self._header_logo = None
        except Exception:
            self._header_logo = None

        # 线程安全的 UI 回调队列：工作线程(bridge/heartbeat/webterm)只入队，
        # 主线程定时轮询取出并在主线程中执行，避免跨线程触碰 tkinter 导致
        # "main thread is not in main loop" 崩溃。必须在任何回调被赋值/调用前创建。
        self._ui_queue = queue.Queue()
        self._ui_running = True

        # 数据
        self.manager = BridgeManager(
            log_cb=self._on_log,
            state_cb=self._on_state_change,
            data_cb=self._on_data,
            persist_cb=lambda: self._save_config(),
        )
        self._bridge_rows = {}  # bridge_id -> item_id (treeview)
        self._auto_scroll = True
        self._show_hex = False
        self._log_expanded = True  # 日志面板是否展开

        # 心跳配置 & 客户端
        self.heartbeat_cfg = HeartbeatConfig()
        self.heartbeat = HeartbeatClient(
            self.heartbeat_cfg,
            log_cb=self._on_log,
            state_cb=self._on_heartbeat_state,
            baudrate_cb=self._on_baudrate_command,
        )

        # GetCmd (IP上报) 配置（独立于心跳，仅服务器/MAC 复用心跳配置）
        self.getcmd_cfg = GetCmdConfig()

        # Web 终端：每个串口一个独立、可配置端口（默认 9211~9214）
        self.webhub = WebTerminalHub(log_cb=self._on_log)

        self._build_ui()
        self._apply_styles()
        self._load_config_on_start()

        # 定时刷新统计
        self._refresh_stats()

        # 启动线程安全的 UI 回调轮询（仅主线程执行 tkinter 操作）
        self.root.after(200, self._drain_ui_queue)

        # 关闭处理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 构建 ──

    def _apply_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        # 全局基调
        style.configure(
            ".", background=COLOR_BG, foreground=COLOR_TEXT, font=("Segoe UI", 9)
        )
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TCheckbutton", background=COLOR_BG)
        style.configure(
            "TCombobox",
            selectbackground=COLOR_PRIMARY,
            fieldbackground=COLOR_CARD,
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            lightcolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            arrowcolor=COLOR_PRIMARY,
            padding=(4, 3),
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", COLOR_PRIMARY)],
        )
        style.configure(
            "TEntry",
            fieldbackground=COLOR_CARD,
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            insertcolor=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            lightcolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            padding=(4, 3),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", COLOR_PRIMARY)],
        )

        # 卡片 / 标题
        style.configure(
            "Card.TFrame", background=COLOR_CARD, relief="flat", borderwidth=0
        )
        style.configure(
            "Title.TLabel",
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            font=("Segoe UI", 14, "bold"),
        )
        style.configure(
            "SubTitle.TLabel",
            background=COLOR_CARD,
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Status.TLabel",
            background=COLOR_CARD,
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 9),
        )

        # 分组框（对话框内「串口设置 / 网络设置」）
        style.configure(
            "TLabelframe",
            background=COLOR_BG,
            foreground=COLOR_TEXT_SEC,
            bordercolor=COLOR_BORDER,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=COLOR_BG,
            foreground=COLOR_PRIMARY_DARK,
            font=("Segoe UI", 9, "bold"),
        )

        # 彩色扁平按钮（加宽 padding 营造圆润大气感）
        _btn_font = ("Segoe UI", 9, "bold")
        _btn_pad = (14, 6)
        style.configure(
            "Primary.TButton",
            background=COLOR_PRIMARY,
            foreground="white",
            font=_btn_font,
            borderwidth=0,
            padding=_btn_pad,
            relief="flat",
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLOR_PRIMARY_DARK), ("disabled", "#cbd5e1")],
        )
        style.configure(
            "Success.TButton",
            background=COLOR_SUCCESS,
            foreground="white",
            font=_btn_font,
            borderwidth=0,
            padding=_btn_pad,
            relief="flat",
        )
        style.map(
            "Success.TButton",
            background=[("active", "#15803d"), ("disabled", "#cbd5e1")],
        )
        style.configure(
            "Danger.TButton",
            background=COLOR_ERROR,
            foreground="white",
            font=_btn_font,
            borderwidth=0,
            padding=_btn_pad,
            relief="flat",
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#b91c1c"), ("disabled", "#cbd5e1")],
        )
        # 次级幽灵按钮（白底 + 边框，用于编辑/刷新等辅助操作）
        style.configure(
            "Ghost.TButton",
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            font=("Segoe UI", 9),
            borderwidth=1,
            bordercolor=COLOR_BORDER,
            padding=(14, 6),
            relief="solid",
        )
        style.map(
            "Ghost.TButton",
            background=[("active", COLOR_BORDER_LIGHT), ("disabled", "#f3f4f6")],
            foreground=[("disabled", COLOR_TEXT_MUTED)],
        )

        # Treeview：宽松行高 + 选中高亮 + 精致表头
        style.configure(
            "Treeview",
            background=COLOR_CARD,
            fieldbackground=COLOR_CARD,
            foreground=COLOR_TEXT,
            rowheight=32,
            font=("Segoe UI", 9),
            selectbackground=COLOR_PRIMARY,
            selectforeground="white",
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background=COLOR_BG,
            foreground=COLOR_PRIMARY_DARK,
            relief="flat",
            borderwidth=0,
            padding=(0, 6),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", COLOR_PRIMARY_LIGHT)],
        )

        # 状态行 tag：仅活动/异常态上色（运行中绿、等待橙、错误红、其余中性灰）
        self.tree.tag_configure("st_ok", foreground=COLOR_SUCCESS)
        self.tree.tag_configure(
            "st_warn", foreground=COLOR_WARNING, font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure(
            "st_err", foreground=COLOR_ERROR, font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure("st_idle", foreground=COLOR_TEXT_SEC)
        # 交替行背景（与状态 tag 叠加，background 由 row_alt 提供）
        self.tree.tag_configure("row_alt", background=COLOR_ROW_ALT)

    def _make_side_btn(self, parent, icon, text, command):
        """侧栏导航按钮：深色底、左对齐、hover 高亮。"""
        btn = tk.Button(
            parent,
            text=f" {icon}  {text}",
            bg=COLOR_SIDEBAR,
            fg=COLOR_SIDEBAR_TEXT,
            activebackground=COLOR_SIDEBAR_HOVER,
            activeforeground="white",
            font=("Segoe UI", 10),
            relief="flat",
            bd=0,
            anchor="w",
            padx=16,
            pady=9,
            cursor="hand2",
            highlightthickness=0,
            command=command,
        )

        def on_enter(e):
            btn.config(bg=COLOR_SIDEBAR_HOVER, fg="white")

        def on_leave(e):
            btn.config(bg=COLOR_SIDEBAR, fg=COLOR_SIDEBAR_TEXT)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _make_primary_btn(self, parent, text, command):
        """内容区主操作按钮：强调蓝实底、hover 加深。"""
        btn = tk.Button(
            parent,
            text=text,
            bg=COLOR_PRIMARY,
            fg="white",
            activebackground=COLOR_PRIMARY_DARK,
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            bd=0,
            padx=18,
            pady=7,
            cursor="hand2",
            highlightthickness=0,
            command=command,
        )

        def on_enter(e):
            btn.config(bg=COLOR_PRIMARY_DARK)

        def on_leave(e):
            btn.config(bg=COLOR_PRIMARY)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _make_stat_card(self, parent, caption, icon, tint, accent):
        """统计卡片：彩色底 + 大号数字 + 小标题 + 阴影效果"""
        # 外层容器模拟阴影
        shadow = tk.Frame(parent, bg="#e0e0e0", relief="flat", bd=0)
        shadow.pack(side="left", fill="both", expand=True, padx=6, pady=4)

        # 内层卡片（偏移制造阴影效果）
        card = tk.Frame(
            shadow,
            bg=tint,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightcolor=COLOR_BORDER,
        )
        card.pack(fill="both", expand=True, padx=(0, 2), pady=(0, 2))

        head = tk.Frame(card, bg=tint)
        head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(head, text=icon, bg=tint, fg=accent, font=("Segoe UI", 14)).pack(
            side="left"
        )
        val = tk.Label(
            card,
            text="-",
            bg=tint,
            fg=accent,
            font=("Segoe UI", 22, "bold"),
        )
        val.pack(anchor="w", padx=14, pady=(2, 0))
        tk.Label(
            card, text=caption, bg=tint, fg=COLOR_TEXT_SEC, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=14, pady=(0, 10))
        return val

    def _build_ui(self):
        # 状态栏先占底部（pack 顺序保证横跨整个窗口底部）
        self.status_bar = tk.Label(
            self.root,
            text=" 就绪",
            bg=COLOR_STATUS_BG,
            fg=COLOR_TEXT_SEC,
            anchor="w",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
        )
        self.status_bar.pack(fill="x", side="bottom")

        # ── 主容器：左侧深色导航栏 + 右侧浅色内容区 ──
        main_wrap = tk.Frame(self.root, bg=COLOR_BG)
        main_wrap.pack(fill="both", expand=True)

        # ═══ 侧栏 ═══
        sidebar = tk.Frame(main_wrap, bg=COLOR_SIDEBAR, width=190)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # logo
        logo_row = tk.Frame(sidebar, bg=COLOR_SIDEBAR)
        logo_row.pack(fill="x", padx=16, pady=(18, 4))
        if self._header_logo:
            tk.Label(logo_row, image=self._header_logo, bg=COLOR_SIDEBAR).pack(
                side="left"
            )
        tk.Label(
            logo_row,
            text="USB2Net",
            bg=COLOR_SIDEBAR,
            fg="white",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left", padx=6)
        tk.Label(
            sidebar,
            text="网络串口服务器",
            bg=COLOR_SIDEBAR,
            fg=COLOR_SIDEBAR_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=18)

        # 分组：串口操作
        tk.Label(
            sidebar,
            text="串口操作",
            bg=COLOR_SIDEBAR,
            fg=COLOR_SIDEBAR_MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 2))
        self._btn_start_all = self._make_side_btn(
            sidebar, "▶", "全部启动", self._on_start_all
        )
        self._btn_start_all.pack(fill="x")
        self._btn_stop_all = self._make_side_btn(
            sidebar, "■", "全部停止", self._on_stop_all
        )
        self._btn_stop_all.pack(fill="x")

        # 分组：管理服务器
        tk.Label(
            sidebar,
            text="管理服务器",
            bg=COLOR_SIDEBAR,
            fg=COLOR_SIDEBAR_MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(14, 2))
        self._btn_heartbeat = self._make_side_btn(
            sidebar, "♥", "心跳设置", self._on_heartbeat_settings
        )
        self._btn_heartbeat.pack(fill="x")
        self._btn_getcmd = self._make_side_btn(sidebar, "⇧", "IP上报", self._on_getcmd)
        self._btn_getcmd.pack(fill="x")

        # 弹性占位，把心跳状态推到底部
        tk.Frame(sidebar, bg=COLOR_SIDEBAR).pack(fill="both", expand=True)

        # 心跳状态指示（底部）
        hb_card = tk.Frame(sidebar, bg=COLOR_SIDEBAR_HOVER)
        hb_card.pack(fill="x", padx=10, pady=(0, 6))
        self._hb_dot = tk.Label(
            hb_card,
            text="●",
            bg=COLOR_SIDEBAR_HOVER,
            fg=COLOR_TEXT_MUTED,
            font=("Segoe UI", 10),
        )
        self._hb_dot.pack(side="left", padx=(10, 4), pady=8)
        self._stat_hb = tk.Label(
            hb_card,
            text="心跳：已停止",
            bg=COLOR_SIDEBAR_HOVER,
            fg=COLOR_SIDEBAR_TEXT,
            font=("Segoe UI", 9),
        )
        self._stat_hb.pack(side="left")

        tk.Label(
            sidebar,
            text=APP_VERSION,
            bg=COLOR_SIDEBAR,
            fg=COLOR_SIDEBAR_MUTED,
            font=("Segoe UI", 8),
        ).pack(side="bottom", pady=(0, 10))

        # ═══ 内容区 ═══
        content = tk.Frame(main_wrap, bg=COLOR_BG)
        content.pack(side="left", fill="both", expand=True)

        # ── 页头：标题 + 主操作按钮 ──
        page_head = tk.Frame(content, bg=COLOR_BG)
        page_head.pack(fill="x", padx=18, pady=(14, 2))
        title_col = tk.Frame(page_head, bg=COLOR_BG)
        title_col.pack(side="left")
        tk.Label(
            title_col,
            text="串口管理",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_col,
            text="USB / 串口设备通过 TCP 暴露到网络",
            bg=COLOR_BG,
            fg=COLOR_TEXT_SEC,
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        self._btn_add = self._make_primary_btn(page_head, "＋ 添加串口", self._on_add)
        self._btn_add.pack(side="right", pady=(4, 0))

        # ── 统计卡片行 ──
        stats_row = tk.Frame(content, bg=COLOR_BG)
        stats_row.pack(fill="x", padx=14, pady=(8, 2))
        self._stat_total = self._make_stat_card(
            stats_row, "串口总数", "▤", COLOR_TINT_BLUE, COLOR_PRIMARY
        )
        self._stat_running = self._make_stat_card(
            stats_row, "运行中", "▶", COLOR_TINT_GREEN, COLOR_SUCCESS
        )
        self._stat_traffic = self._make_stat_card(
            stats_row, "累计流量", "⇅", COLOR_TINT_ORANGE, "#ea580c"
        )
        self._stat_clients = self._make_stat_card(
            stats_row, "TCP 客户端", "◉", COLOR_TINT_PURPLE, "#9333ea"
        )

        # ── 串口列表区 ──
        list_frame = tk.Frame(content, bg=COLOR_BG)
        list_frame.pack(fill="both", expand=True, padx=18, pady=(4, 0))

        # Treeview（白卡片 + 细边框）
        tree_frame = tk.Frame(list_frame, bg=COLOR_BORDER, bd=1)
        tree_frame.pack(fill="both", expand=True)

        columns = (
            "name",
            "serial",
            "baudrate",
            "tcp_port",
            "web",
            "state",
            "clients",
            "rx_bytes",
            "tx_bytes",
            "uptime",
        )
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)

        col_config = [
            ("name", "名称", 100, "center"),
            ("serial", "串口", 110, "center"),
            ("baudrate", "波特率", 80, "center"),
            ("tcp_port", "TCP端口", 70, "center"),
            ("web", "Web端口", 70, "center"),
            ("state", "状态", 100, "center"),
            ("clients", "客户端数", 70, "center"),
            ("rx_bytes", "串口→TCP", 90, "center"),
            ("tx_bytes", "TCP→串口", 90, "center"),
            ("uptime", "运行时间", 80, "center"),
        ]
        for cid, text, width, anchor in col_config:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor, minwidth=width)

        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        # 右键菜单
        self._ctx_menu = tk.Menu(self.root, tearoff=0)
        self._ctx_menu.add_command(label="启动", command=self._ctx_start)
        self._ctx_menu.add_command(label="停止", command=self._ctx_stop)
        self._ctx_menu.add_command(label="Web 终端", command=self._ctx_web_term)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="编辑", command=self._ctx_edit)
        self._ctx_menu.add_command(label="删除", command=self._ctx_delete)

        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", lambda e: self._ctx_edit())
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # 操作按钮行
        op_frame = tk.Frame(list_frame, bg=COLOR_BG)
        op_frame.pack(fill="x", pady=6)

        # 使用tk.Button实现联动控制（ttk.Button禁用状态样式不好控制）
        self._btn_start = tk.Button(
            op_frame,
            text="▶ 启动选中",
            bg=COLOR_SUCCESS,
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            command=self._ctx_start,
            state="normal",
        )
        self._btn_start.pack(side="left", padx=3)

        self._btn_stop = tk.Button(
            op_frame,
            text="⏹ 停止选中",
            bg=COLOR_ERROR,
            fg="white",
            activebackground="#b91c1c",
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            command=self._ctx_stop,
            state="normal",
        )
        self._btn_stop.pack(side="left", padx=3)

        ttk.Button(
            op_frame,
            text="🌐 Web 终端",
            command=self._ctx_web_term,
            style="Primary.TButton",
        ).pack(side="left", padx=3)
        ttk.Button(
            op_frame, text="✎ 编辑", command=self._ctx_edit, style="Ghost.TButton"
        ).pack(side="left", padx=3)
        ttk.Button(
            op_frame, text="🗑 删除", command=self._ctx_delete, style="Danger.TButton"
        ).pack(side="left", padx=3)
        ttk.Button(
            op_frame,
            text="⟳ 刷新串口",
            command=self._refresh_ports,
            style="Ghost.TButton",
        ).pack(side="left", padx=3)

        # 波特率实时调整（选中串口后，下拉选值并点"应用"即实时切换串口波特率）
        baud_frame = tk.Frame(op_frame, bg=COLOR_BG)
        baud_frame.pack(side="right", padx=8)
        tk.Label(
            baud_frame,
            text="波特率",
            bg=COLOR_BG,
            fg=COLOR_TEXT_SEC,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(4, 2))
        self._baud_combo = ttk.Combobox(
            baud_frame,
            width=9,
            state="normal",
            justify="center",
            values=(
                "9600",
                "19200",
                "38400",
                "57600",
                "115200",
                "230400",
                "460800",
                "921600",
            ),
        )
        self._baud_combo.pack(side="left", padx=2)
        ttk.Button(
            baud_frame,
            text="应用",
            style="Primary.TButton",
            command=self._on_apply_baud,
        ).pack(side="left", padx=2)

        # ── 日志面板（可折叠卡片式深色终端） ──
        log_wrapper = tk.Frame(content, bg=COLOR_BG)
        log_wrapper.pack(fill="both", expand=True, padx=18, pady=(2, 8))

        # 日志面板标题栏（可点击折叠）
        log_header = tk.Frame(log_wrapper, bg=COLOR_BG)
        log_header.pack(fill="x")
        self._btn_toggle_log = tk.Button(
            log_header,
            text="▼ 运行日志",
            bg=COLOR_BG,
            fg=COLOR_TEXT_SEC,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            command=self._toggle_log_panel,
        )
        self._btn_toggle_log.pack(side="left")

        # 日志面板主体（可折叠）
        self._log_frame = tk.Frame(log_wrapper, bg=COLOR_CARD, bd=1, relief="solid")
        self._log_frame.pack(fill="both", expand=True, pady=(2, 0))

        # 日志工具栏
        log_toolbar = tk.Frame(self._log_frame, bg=COLOR_CARD)
        log_toolbar.pack(fill="x", pady=(6, 2), padx=8)

        self._hex_var = tk.BooleanVar()
        tk.Checkbutton(
            log_toolbar,
            text="HEX 显示",
            variable=self._hex_var,
            bg=COLOR_CARD,
            command=self._toggle_hex,
        ).pack(side="left", padx=4)

        self._autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            log_toolbar,
            text="自动滚动",
            variable=self._autoscroll_var,
            bg=COLOR_CARD,
            command=self._toggle_autoscroll,
        ).pack(side="left", padx=4)

        tk.Button(
            log_toolbar,
            text="清空日志",
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SEC,
            relief="flat",
            cursor="hand2",
            padx=8,
            command=self._clear_log,
        ).pack(side="left", padx=4)

        # ── 日志文本框容器（使用 grid 布局） ──
        log_text_container = tk.Frame(self._log_frame, bg=COLOR_BORDER)
        log_text_container.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # 配置 grid 行列权重
        log_text_container.grid_rowconfigure(0, weight=1)  # 文本区域占大部分
        log_text_container.grid_rowconfigure(1, weight=0)  # 水平滚动条不占 expand
        log_text_container.grid_columnconfigure(0, weight=1)  # 列可扩展

        # 上方区域：文本 + 垂直滚动条
        log_text_top = tk.Frame(log_text_container, bg=COLOR_BORDER)
        log_text_top.grid(row=0, column=0, sticky="nsew")
        log_text_top.grid_columnconfigure(0, weight=1)
        log_text_top.grid_rowconfigure(0, weight=1)

        # 文本控件
        self.log_text = tk.Text(
            log_text_top,
            wrap="none",  # 不换行，让水平滚动条起作用
            font=("Consolas", 9),
            bg=COLOR_LOG_BG,
            fg=COLOR_LOG_FG,
            insertbackground="white",
            state="disabled",
            relief="flat",
            padx=6,
            pady=4,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # 垂直滚动条
        v_scroll = ttk.Scrollbar(
            log_text_top, orient="vertical", command=self.log_text.yview
        )
        v_scroll.grid(row=0, column=1, sticky="ns")

        # ── 底部区域：水平滚动条（固定高度，确保可见） ──
        h_scroll = ttk.Scrollbar(
            log_text_container, orient="horizontal", command=self.log_text.xview
        )
        h_scroll.grid(row=1, column=0, sticky="ew", pady=(1, 0))

        # 绑定滚动条
        self.log_text.configure(
            yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set
        )

        # 日志颜色 tag
        # 优化：时间戳颜色调淡，与正文区分更明显
        self.log_text.tag_config("timestamp", foreground="#565f89")
        self.log_text.tag_config("info", foreground="#7aa2f7")
        self.log_text.tag_config("warn", foreground="#e0af68")
        self.log_text.tag_config("error", foreground="#f7768e")
        self.log_text.tag_config("rx", foreground="#9ece6a")
        self.log_text.tag_config("tx", foreground="#bb9af7")
        self.log_text.tag_config("system", foreground="#737aa2")

    # ── 新增功能：日志折叠 ──

    def _toggle_log_panel(self):
        """折叠/展开日志面板"""
        if self._log_expanded:
            self._log_frame.pack_forget()
            self._log_expanded = False
            self._btn_toggle_log.config(text="▶ 运行日志")
        else:
            self._log_frame.pack(fill="both", expand=True, pady=(2, 0))
            self._log_expanded = True
            self._btn_toggle_log.config(text="▼ 运行日志")

    # ── 新增功能：Toast 提示 ──

    def _show_toast(self, message, duration=2000, is_success=True):
        """显示轻提示"""
        Toast.show(self.root, message, duration, is_success)

    # ── 新增功能：按钮状态联动 ──

    def _update_button_states(self, bridge_id: int):
        """根据选中串口的状态更新启动/停止按钮"""
        bridge = self.manager.get_bridge(bridge_id)
        if not bridge:
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="disabled")
            return

        state = bridge.state
        if state == BridgeState.RUNNING:
            # 运行中：启动禁用，停止可用
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="normal")
        elif state == BridgeState.STARTING:
            # 启动中：启动禁用，停止可用（可以取消启动）
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="normal")
        elif state == BridgeState.WAITING:
            # 等待设备：启动禁用，停止可用
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="normal")
        elif state == BridgeState.STOPPED:
            # 已停止：启动可用，停止禁用
            self._btn_start.config(state="normal")
            self._btn_stop.config(state="disabled")
        elif state == BridgeState.ERROR:
            # 错误状态：启动可用（重试），停止禁用
            self._btn_start.config(state="normal")
            self._btn_stop.config(state="disabled")
        else:
            # 未知状态：都禁用
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="disabled")

    # ── 事件处理 ──

    def _on_add(self):
        # 计算下一个默认名称：网络串口-N（N 取已有同名序号最大值 +1，避免撞名）
        max_n = 0
        for b in self.manager.get_all_bridges().values():
            m = re.match(r"^网络串口-(\d+)$", b.config.name or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
        default_cfg = BridgeConfig(name=f"网络串口-{max_n + 1}")
        dlg = AddEditBridgeDialog(self.root, config=default_cfg, title="添加串口")
        self.root.wait_window(dlg)
        if dlg.result:
            bridge_id = self.manager.add_bridge(dlg.result)
            self._add_tree_row(bridge_id, dlg.result)
            self.webhub.register(
                bridge_id,
                dlg.result.web_port,
                "127.0.0.1",
                dlg.result.tcp_port,
                dlg.result.name,
            )
            self._on_log("info", f"已添加串口: {dlg.result.name}")
            url = self.webhub.url_for(bridge_id)
            self._on_log(
                "info",
                f"Web 终端地址: {url}"
                if url
                else f"Web 终端: 未配置端口（可在编辑中设置）",
            )
            self._save_config()
            self._show_toast(f"已添加: {dlg.result.name}")

    def _on_start_all(self):
        self.manager.start_all()
        self._on_log("info", "已启动所有已启用的串口")
        self._show_toast("已启动所有串口")

    def _on_stop_all(self):
        self.manager.stop_all()
        self._on_log("info", "已停止所有串口")
        self._show_toast("已停止所有串口")

    # ── 心跳 ──

    def _on_heartbeat_state(self, state: str):
        """心跳状态变更回调（可能来自子线程）"""
        self.heartbeat_state = state

    def _on_heartbeat_settings(self):
        dlg = HeartbeatSettingsDialog(self.root, self.heartbeat_cfg)
        self.root.wait_window(dlg)
        if dlg.result:
            was_running = self.heartbeat._running
            if was_running:
                self.heartbeat.stop()
            self.heartbeat_cfg = dlg.result
            self.heartbeat.config = self.heartbeat_cfg
            self._save_config()
            if self.heartbeat_cfg.enabled:
                self.heartbeat.start()
            self._on_log(
                "info",
                "心跳配置已更新" + ("并启动" if self.heartbeat_cfg.enabled else ""),
            )
            self._show_toast("心跳配置已保存")

    # ── GetCmd (IP上报) ──

    def _on_getcmd(self):
        """打开 IP上报 弹窗（需先通过密码验证）"""
        pwd_dlg = PasswordDialog(self.root, GETCMD_PASSWORD, title="IP上报 - 身份验证")
        self.root.wait_window(pwd_dlg)
        if not pwd_dlg.verified:
            self._on_log("warn", "IP上报：密码验证未通过，已拒绝访问")
            self._show_toast("密码验证失败", is_success=False)
            return
        self._on_log("info", "IP上报：密码验证通过")

        dlg = GetCmdDialog(
            self.root, self.getcmd_cfg, self.heartbeat_cfg, self._send_getcmd
        )
        self.root.wait_window(dlg)
        # 弹窗关闭后保存配置（GetCmd 字段已写回 getcmd_cfg）
        self._save_config()

    def _send_getcmd(
        self,
        version: str,
        ipaddr: str,
        mainstep: int,
        substep: int,
        msgtype: int,
        src_moduleid: int,
        dst_moduleid: int,
    ):
        """GetCmd 发送回调——调用 heartbeat 客户端发送一次 GetCmd 包"""
        self.heartbeat.send_getcmd(
            version, ipaddr, mainstep, substep, msgtype, src_moduleid, dst_moduleid
        )
        self._show_toast(f"IP上报已发送 ({version})")

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self._ctx_menu.post(event.x_root, event.y_root)

    def _get_selected_bridge_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        item = sel[0]
        for bid, iid in self._bridge_rows.items():
            if iid == item:
                return bid
        return None

    def _on_tree_select(self, event=None):
        """选中串口时，更新状态栏 + 按钮联动 + 波特率下拉"""
        bid = self._get_selected_bridge_id()
        if bid is None:
            self.status_bar.config(text=" 就绪")
            self._btn_start.config(state="disabled")
            self._btn_stop.config(state="disabled")
            return
        bridge = self.manager.get_bridge(bid)
        if bridge:
            state_text = STATE_TEXT.get(bridge.state, str(bridge.state))
            client_count = bridge.client_count if hasattr(bridge, "client_count") else 0
            # 状态栏显示详情
            self.status_bar.config(
                text=f" 选中: {bridge.config.name} | "
                f"串口: {bridge.config.serial_port} | "
                f"TCP: {bridge.config.tcp_port} | "
                f"状态: {state_text} | "
                f"客户端: {client_count}"
            )
            # 回填波特率到下拉框
            self._baud_combo.set(str(bridge.config.baudrate))
            # 更新按钮状态
            self._update_button_states(bid)

    def _on_apply_baud(self):
        """把下拉框里的波特率实时应用到选中的串口"""
        bid = self._get_selected_bridge_id()
        if bid is None:
            self._show_toast("请先选中一个串口", is_success=False)
            return
        bridge = self.manager.get_bridge(bid)
        if bridge is None:
            return

        raw = self._baud_combo.get().strip()
        if not raw:
            self._show_toast("请输入波特率", is_success=False)
            return
        try:
            baud = int(raw)
        except ValueError:
            self._show_toast("波特率必须是数字", is_success=False)
            return
        if baud <= 0:
            self._show_toast("波特率必须为正数", is_success=False)
            return

        bridge.set_baudrate(baud)
        self._baud_combo.set(str(bridge.config.baudrate))
        self._update_tree_row(bid, bridge.config)

        opened = bridge.serial_conn is not None and bridge.serial_conn.is_open
        suffix = "" if opened else "（串口未连接，仅更新配置，连接后生效）"
        self._on_log(
            "info",
            f"[{bridge.config.name}] 波特率已实时切换为 "
            f"{bridge.config.baudrate} {suffix}",
        )
        self._show_toast(f"波特率已切换为 {bridge.config.baudrate}")

    def _ctx_start(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            bridge = self.manager.get_bridge(bid)
            self.manager.start_bridge(bid)
            if bridge:
                self._show_toast(f"正在启动: {bridge.config.name}")

    def _ctx_stop(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            bridge = self.manager.get_bridge(bid)
            self.manager.stop_bridge(bid)
            if bridge:
                self._show_toast(f"已停止: {bridge.config.name}")

    def _ctx_web_term(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            self._open_web_term(bid)

    def _open_web_term(self, bridge_id: int):
        """在浏览器打开该串口的 Web 终端页面（固定端口，无需 id）"""
        url = self.webhub.url_for(bridge_id)
        bridge = self.manager.get_bridge(bridge_id)
        name = bridge.config.name if bridge else f"Bridge-{bridge_id}"
        if not url:
            self._on_log(
                "warn", f"[{name}] 未配置 Web 终端端口，请在编辑中设置 Web 端口。"
            )
            self._show_toast("请先配置 Web 端口", is_success=False)
            return
        if bridge and bridge.state != BridgeState.RUNNING:
            self._on_log(
                "warn",
                f"[{name}] 串口未运行，Web 终端可能无法连接串口。请先启动该串口。",
            )
        self._on_log("info", f"正在打开 Web 终端: {url}")
        self._show_toast(f"正在打开: {name}")
        open_browser(url)

    def _ctx_edit(self):
        bid = self._get_selected_bridge_id()
        if bid is None:
            return
        bridge = self.manager.get_bridge(bid)
        if not bridge:
            return
        dlg = AddEditBridgeDialog(self.root, config=bridge.config, title="编辑串口")
        self.root.wait_window(dlg)
        if dlg.result:
            self.manager.update_bridge(bid, dlg.result)
            self._update_tree_row(bid, dlg.result)
            # 端口/目标可能变化，重新注册 Web 终端以生效
            self.webhub.register(
                bid,
                dlg.result.web_port,
                "127.0.0.1",
                dlg.result.tcp_port,
                dlg.result.name,
            )
            self._on_log("info", f"已更新串口: {dlg.result.name}")
            self._save_config()
            self._show_toast(f"已更新: {dlg.result.name}")

    def _ctx_delete(self):
        bid = self._get_selected_bridge_id()
        if bid is None:
            return
        bridge = self.manager.get_bridge(bid)
        name = bridge.config.name if bridge else f"ID:{bid}"
        if messagebox.askyesno("确认删除", f"确定要删除「{name}」吗？"):
            self.manager.remove_bridge(bid)
            self.webhub.unregister(bid)
            iid = self._bridge_rows.pop(bid, None)
            if iid:
                self.tree.delete(iid)
            self._on_log("info", f"已删除串口: {name}")
            self._save_config()
            self._show_toast(f"已删除: {name}")

    def _refresh_ports(self):
        ports = BridgeManager.list_serial_ports()
        if ports:
            port_list = "\n".join(f"  {p[0]} - {p[1]}" for p in ports)
            self._on_log("info", f"检测到 {len(ports)} 个串口:\n{port_list}")
            self._show_toast(f"检测到 {len(ports)} 个串口")
        else:
            self._on_log("warn", "未检测到任何串口设备")
            self._show_toast("未检测到串口", is_success=False)

    def _toggle_hex(self):
        self._show_hex = self._hex_var.get()

    def _toggle_autoscroll(self):
        self._auto_scroll = self._autoscroll_var.get()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._show_toast("日志已清空")

    # ── Treeview 操作 ──

    def _add_tree_row(self, bridge_id: int, config: BridgeConfig):
        web_disp = str(config.web_port) if config.web_port else "-"
        iid = self.tree.insert(
            "",
            "end",
            values=(
                config.name,
                config.serial_port,
                config.baudrate,
                config.tcp_port,
                web_disp,
                "● 已停止",
                0,
                "0",
                "0",
                "00:00:00",
            ),
        )
        self._bridge_rows[bridge_id] = iid

    def _update_tree_row(self, bridge_id: int, config: BridgeConfig):
        iid = self._bridge_rows.get(bridge_id)
        if iid:
            current = self.tree.item(iid, "values")
            web_disp = str(config.web_port) if config.web_port else "-"
            self.tree.item(
                iid,
                values=(
                    config.name,
                    config.serial_port,
                    config.baudrate,
                    config.tcp_port,
                    web_disp,
                    current[5],
                    current[6],
                    current[7],
                    current[8],
                    current[9],
                ),
            )

    # ── 回调（从工作线程调用，需线程安全）──
    # 关键：工作线程决不直接调用 tkinter，只把任务塞进 _ui_queue，
    # 由主线程的 _drain_ui_queue 取出执行，彻底避免跨线程触碰 tkinter。

    def _call_on_main(self, fn, *args):
        """线程安全地调度一个在主线程执行的函数（供工作线程调用）"""
        try:
            self._ui_queue.put((fn, args))
        except Exception:
            pass

    def _on_log(self, level: str, msg: str):
        """日志回调，可能来自子线程"""
        self._call_on_main(self._append_log, level, msg)

    def _on_state_change(self, bridge_id: int, state: BridgeState):
        """状态变更回调"""
        self._call_on_main(self._update_bridge_state, bridge_id, state)

    def _on_data(self, bridge_id: int, direction: str, data: bytes):
        """数据回调"""
        # 限制日志量，避免高频数据刷屏
        if len(data) > 256:
            preview = data[:256]
            suffix = f"... (+{len(data) - 256} bytes)"
        else:
            preview = data
            suffix = ""

        if self._show_hex:
            text = " ".join(f"{b:02X}" for b in preview) + suffix
        else:
            try:
                text = preview.decode("utf-8", errors="replace")
            except Exception:
                text = " ".join(f"{b:02X}" for b in preview)
            text += suffix

        bridge = self.manager.get_bridge(bridge_id)
        name = bridge.config.name if bridge else f"Bridge-{bridge_id}"
        arrow = "←" if direction == "rx" else "→"
        tag = "rx" if direction == "rx" else "tx"
        label = "串口→TCP" if direction == "rx" else "TCP→串口"

        self._call_on_main(
            self._append_data_log, tag, name, arrow, label, text, len(data)
        )

    def _on_baudrate_command(self, tcp_port: int, baudrate: int, info: dict):
        """服务端下发的波特率指令（心跳 recv 线程调用）→ 转到主线程执行串口切换"""
        self._call_on_main(self._apply_baudrate_command, tcp_port, baudrate, info)

    def _apply_baudrate_command(self, tcp_port: int, baudrate: int, info: dict):
        """主线程：按 tcp_port 找到对应串口并实时切换串口波特率"""
        target = self.manager.get_bridge_by_tcp_port(tcp_port)
        if not target:
            self._on_log(
                "warn",
                f"收到服务端波特率指令，但无 TCP端口={tcp_port} 的串口，已忽略 "
                f"(ttycode={info.get('ttycode')})",
            )
            return
        bid = target.id if hasattr(target, "id") else None
        name = target.config.name
        try:
            target.set_baudrate(baudrate)
            if bid is not None:
                self._update_tree_row(bid, target.config)  # 刷新波特率列
                # 若当前选中的正是该串口，同步刷新操作区波特率下拉，避免页面仍显示旧值
                if self._get_selected_bridge_id() == bid:
                    self._baud_combo.set(str(target.config.baudrate))
            self._on_log(
                "info",
                f"[{name}] 服务端指令：波特率已切换为 {baudrate} "
                f"(ttycode={info.get('ttycode')}, 原 {info.get('obaudrate')})",
            )
            self._show_toast(f"[{name}] 波特率已切换为 {baudrate}")
        except Exception as e:
            self._on_log("error", f"[{name}] 服务端波特率指令执行失败: {e}")

    def _drain_ui_queue(self):
        """主线程定时取出 UI 任务并执行（只由主线程调用）"""
        if not self._ui_running:
            return
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    pass
                self._ui_queue.task_done()
        except queue.Empty:
            pass
        if self._ui_running:
            self.root.after(200, self._drain_ui_queue)

    # ── 日志写入 ──

    def _append_log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state="normal")
        # 优化：时间戳去掉方括号，颜色更淡
        self.log_text.insert("end", f"{ts} ", "timestamp")
        self.log_text.insert(
            "end", f"{msg}\n", level if level in ("info", "warn", "error") else "system"
        )
        self.log_text.configure(state="disabled")

        if self._auto_scroll:
            self.log_text.see("end")

        # 限制日志行数
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", f"{lines - 2000}.0")
            self.log_text.configure(state="disabled")

    def _append_data_log(self, tag, name, arrow, label, text, data_len):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state="normal")
        # 优化：时间戳去掉方括号，颜色更淡
        self.log_text.insert("end", f"{ts} ", "timestamp")
        self.log_text.insert("end", f"{name} {arrow} ", tag)
        self.log_text.insert("end", f"[{label}] ", "system")
        self.log_text.insert("end", f"{text}\n", tag)
        self.log_text.configure(state="disabled")

        if self._auto_scroll:
            self.log_text.see("end")

    def _update_bridge_state(self, bridge_id: int, state: BridgeState):
        iid = self._bridge_rows.get(bridge_id)
        if not iid:
            return
        _color, text = STATE_STYLE.get(state, ("#9e9e9e", str(state)))
        values = list(self.tree.item(iid, "values"))
        values[5] = text
        # 仅对活动/异常态上色（运行中绿、等待橙、错误红、其余中性灰），
        # 让正常行保持清爽、异常行一目了然，视觉层级更克制高级
        if state == BridgeState.ERROR:
            self.tree.item(iid, values=values, tags=("st_err",))
        elif state == BridgeState.WAITING:
            self.tree.item(iid, values=values, tags=("st_warn",))
        elif state == BridgeState.RUNNING:
            self.tree.item(iid, values=values, tags=("st_ok",))
        else:
            self.tree.item(iid, values=values, tags=("st_idle",))

        # 如果选中的是这个串口，更新按钮状态
        if self._get_selected_bridge_id() == bridge_id:
            self._update_button_states(bridge_id)
            # 同时更新状态栏
            bridge = self.manager.get_bridge(bridge_id)
            if bridge:
                state_text = STATE_TEXT.get(state, str(state))
                client_count = (
                    bridge.client_count if hasattr(bridge, "client_count") else 0
                )
                self.status_bar.config(
                    text=f" 选中: {bridge.config.name} | "
                    f"串口: {bridge.config.serial_port} | "
                    f"TCP: {bridge.config.tcp_port} | "
                    f"状态: {state_text} | "
                    f"客户端: {client_count}"
                )

    # ── 统计刷新 ──

    def _refresh_stats(self):
        """每秒刷新统计数据"""
        stats = self.manager.get_all_stats()
        for bid, stat in stats.items():
            iid = self._bridge_rows.get(bid)
            if iid:
                values = list(self.tree.item(iid, "values"))
                values[6] = stat["client_count"]
                values[7] = self._fmt_bytes(stat["bytes_serial_to_tcp"])
                values[8] = self._fmt_bytes(stat["bytes_tcp_to_serial"])
                values[9] = self._fmt_uptime(stat["uptime"])
                self.tree.item(iid, values=values)

        # 概览统计卡片
        total_bridges = len(stats)
        running = sum(1 for s in stats.values() if s["state"] == "运行中")
        traffic = sum(
            s["bytes_serial_to_tcp"] + s["bytes_tcp_to_serial"] for s in stats.values()
        )
        clients = sum(s["client_count"] for s in stats.values())
        hb = self.heartbeat.state
        self._stat_total.config(text=str(total_bridges))
        self._stat_running.config(text=str(running))
        self._stat_traffic.config(text=self._fmt_bytes(traffic))
        self._stat_clients.config(text=str(clients))
        self._update_hb_display(hb)

        # 状态栏
        self.status_bar.config(
            text=f" 串口总数: {total_bridges}  |  运行中: {running}  |  "
            f"心跳: {hb}  |  {datetime.now().strftime('%H:%M:%S')}"
        )

        self.root.after(1000, self._refresh_stats)

    def _update_hb_display(self, state: str):
        """更新侧栏底部心跳状态指示灯（文字 + 圆点颜色）。"""
        text = str(state)
        self._stat_hb.config(text=f"心跳：{text}")
        if text == "运行中":
            color = COLOR_SUCCESS
        elif text == "已停止":
            color = COLOR_TEXT_MUTED
        else:
            color = COLOR_WARNING
        self._hb_dot.config(fg=color)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        elif n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        else:
            return f"{n / (1024 * 1024):.2f} MB"

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        if seconds <= 0:
            return "00:00:00"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ── 配置持久化 ──

    def _save_config(self):
        try:
            bridges = [
                b.config.to_dict() for b in self.manager.get_all_bridges().values()
            ]
            data = {
                "bridges": bridges,
                "heartbeat": self.heartbeat_cfg.to_dict(),
                "getcmd": self.getcmd_cfg.to_dict(),
            }
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._on_log("error", f"保存配置失败: {e}")

    def _load_config_on_start(self):
        try:
            # 打包后首次运行：若 exe 旁没有 config.json，从内置副本释放到 exe 目录
            if (
                getattr(sys, "frozen", False)
                and not os.path.exists(self.CONFIG_FILE)
                and hasattr(sys, "_MEIPASS")
            ):
                bundled = os.path.join(sys._MEIPASS, "config.json")
                if os.path.exists(bundled):
                    try:
                        shutil.copyfile(bundled, self.CONFIG_FILE)
                    except Exception as _e:
                        self._on_log("warn", f"释放内置配置失败: {_e}")

            raw = {}
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)

            # 串口配置
            configs = [BridgeConfig.from_dict(i) for i in raw.get("bridges", [])]
            if not configs:
                # 首次运行，添加一个示例配置
                demo = BridgeConfig(
                    name="网络串口-1",
                    serial_port="",
                    baudrate=115200,
                    tcp_port=5000,
                    enabled=False,
                )
                bid = self.manager.add_bridge(demo)
                self._add_tree_row(bid, demo)
                self.webhub.register(
                    bid, demo.web_port, "127.0.0.1", demo.tcp_port, demo.name
                )
                self._on_log("system", "首次运行，已创建示例配置。请编辑后启用。")
            else:
                for cfg in configs:
                    bid = self.manager.add_bridge(cfg)
                    self._add_tree_row(bid, cfg)
                    self.webhub.register(
                        bid, cfg.web_port, "127.0.0.1", cfg.tcp_port, cfg.name
                    )
                self._on_log("system", f"已加载 {len(configs)} 条串口配置")
                self._on_log(
                    "system",
                    "Web 终端已就绪（每串口独立端口，见各串口的'Web 终端'按钮）",
                )

            # 心跳配置
            hb = raw.get("heartbeat")
            if hb:
                self.heartbeat_cfg = HeartbeatConfig.from_dict(hb)
                self.heartbeat.config = self.heartbeat_cfg
            if self.heartbeat_cfg.enabled:
                self.heartbeat.start()
                self._on_log(
                    "system",
                    f"心跳客户端已按配置启动 -> "
                    f"{self.heartbeat_cfg.server_ip}:{self.heartbeat_cfg.server_port}",
                )

            # GetCmd (IP上报) 配置
            gc = raw.get("getcmd")
            if gc:
                self.getcmd_cfg = GetCmdConfig.from_dict(gc)
            elif hb:
                # 兼容旧版：getcmd_* 字段曾寄生在 heartbeat 节点下
                legacy = {
                    k[len("getcmd_") :]: v
                    for k, v in hb.items()
                    if k.startswith("getcmd_")
                }
                if legacy:
                    self.getcmd_cfg = GetCmdConfig.from_dict(legacy)
        except Exception as e:
            self._on_log("error", f"加载配置失败: {e}")

    # ── 关闭 ──

    def _on_close(self):
        if messagebox.askyesno("退出确认", "确定要退出吗？所有活动的串口将被停止。"):
            self._ui_running = False
            self._on_log("system", "正在停止所有串口、心跳与 Web 终端并退出...")
            self.manager.stop_all()
            self.heartbeat.stop()
            try:
                self.webhub.stop()
            except Exception:
                pass
            self._save_config()
            self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ── 入口 ──


def _show_crash_error(exc: Exception):
    """启动阶段的错误弹窗，避免静默退出"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        msg = (
            f"USB2Net 启动失败:\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"常见解决办法:\n"
            f"1. 确保已安装 pyserial: pip install pyserial\n"
            f"2. 确认 Python 已加入 PATH\n"
            f"3. 用 run.bat 启动以获得错误提示"
        )
        messagebox.showerror("启动错误", msg)
        root.destroy()
    except Exception:
        pass
    # 同时打印到控制台
    import traceback

    traceback.print_exc()


def _show_already_running(notified: bool):
    """
    重复启动时的提示弹窗。

    此时主窗口尚未创建，因此自建临时 root 承载弹窗。用 Toplevel 自绘而非
    messagebox，原因有二：
      1. messagebox 弹出的窗口不继承 root 的 topmost 属性，会被刚刚
         置前的已有实例窗口盖住；
      2. 可以沿用应用配色与图标，视觉上与主程序一致。
    弹窗加 topmost 后还会周期性重置一次，抵御已有实例置前抢占 Z 序。
    """
    try:
        import tkinter as tk

        holder = tk.Tk()
        holder.withdraw()

        dlg = tk.Toplevel(holder)
        dlg.withdraw()
        dlg.title("USB2Net 已在运行")
        dlg.configure(bg=COLOR_CARD)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

        # 沿用应用图标
        try:
            _icon_ref = None
            _png = _resource_path("USB2Net.png")
            if os.path.exists(_png):
                _icon_ref = tk.PhotoImage(file=_png)
                dlg.iconphoto(True, _icon_ref)
            else:
                _ico = _resource_path("USB2Net.ico")
                if os.path.exists(_ico):
                    dlg.iconbitmap(_ico)
        except Exception:
            _icon_ref = None

        # ── 内容 ──
        header = tk.Frame(dlg, bg=COLOR_HEADER, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="USB2Net 已在运行",
            bg=COLOR_HEADER,
            fg="#ffffff",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left", padx=20)

        body = tk.Frame(dlg, bg=COLOR_CARD)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 8))

        detail = (
            "已把正在运行的窗口切换到前台，\n本次启动不会创建新的后台进程。"
            if notified
            else "本次启动不会创建新的后台进程。\n（该实例可能正在退出，稍后重试即可）"
        )
        tk.Label(
            body,
            text=detail,
            bg=COLOR_CARD,
            fg=COLOR_TEXT,
            font=("Microsoft YaHei UI", 10),
            justify="left",
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            body,
            text="如需重启，请先关闭正在运行的窗口。",
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SEC,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        # ── 按钮 ──
        footer = tk.Frame(dlg, bg=COLOR_CARD)
        footer.pack(fill="x", padx=24, pady=(8, 20))

        def _close():
            try:
                dlg.destroy()
            finally:
                holder.destroy()

        btn = tk.Button(
            footer,
            text="知道了",
            command=_close,
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_DARK,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10),
            padx=28,
            pady=7,
            highlightthickness=0,
        )
        btn.pack(side="right")
        # 回车 / Esc 都能关闭
        dlg.bind("<Return>", lambda _e: _close())
        dlg.bind("<Escape>", lambda _e: _close())

        # 居中于屏幕
        dlg.update_idletasks()
        _w, _h = dlg.winfo_width(), dlg.winfo_height()
        _sw, _sh = holder.winfo_screenwidth(), holder.winfo_screenheight()
        dlg.geometry(f"{_w}x{_h}+{(_sw - _w) // 2}+{(_sh - _h) // 3}")
        dlg.deiconify()
        dlg.lift()
        dlg.focus_force()
        btn.focus_set()

        # 已有实例的置前动作可能稍后才发生，这里周期性把弹窗拉回顶层，
        # 避免提示一闪就被主窗口压住看不见。
        def _keep_on_top():
            if dlg.winfo_exists():
                try:
                    dlg.attributes("-topmost", True)
                    dlg.lift()
                    dlg.focus_force()
                except Exception:
                    return
                dlg.after(250, _keep_on_top)

        dlg.after(250, _keep_on_top)
        holder.mainloop()
    except Exception:
        pass


if __name__ == "__main__":
    # ── 单实例保护 ──
    # 同一台机器只允许一个 USB2Net 后台运行。判重在创建任何串口/心跳/
    # Web 终端之前完成，重复启动时提示用户并把已有窗口顶到前台，然后退出。
    _guard = SingleInstance()
    if not _guard.acquire():
        _notified = _guard.notify_running()
        _show_already_running(_notified)
        # 退出码保持 0：这是正常流程，run.bat 会把非 0 当作异常来报错
        sys.exit(0)

    try:
        app = USB2NetApp()
        # 已有实例请求置前：激活服务在后台线程发信号，必须转回主线程执行
        _guard.start_activation_server(
            lambda: app._call_on_main(raise_window, app.root)
        )
        app.run()
    except Exception as exc:
        _show_crash_error(exc)
    finally:
        _guard.release()

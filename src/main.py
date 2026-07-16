"""
main.py - USB2Net 桌面应用

基于 tkinter/ttk 的串口转网络桥接工具 GUI。
将串口桥接、心跳注册与 Web 终端功能封装为友好的桌面应用。

功能：
  - 可视化配置串口↔TCP 桥接（支持多条）
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
    from heartbeat import HeartbeatConfig, HeartbeatClient
    from webterm import WebTerminalHub, open_browser
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

COLOR_BG = "#eef1f6"
COLOR_CARD = "#ffffff"
COLOR_PRIMARY = "#2563eb"
COLOR_PRIMARY_DARK = "#1d4ed8"
COLOR_HEADER = "#15294d"
COLOR_HEADER_ACCENT = "#3b82f6"
COLOR_SUCCESS = "#16a34a"
COLOR_WARNING = "#f59e0b"
COLOR_ERROR = "#dc2626"
COLOR_TEXT = "#1f2937"
COLOR_TEXT_SEC = "#6b7280"
COLOR_BORDER = "#dfe3ea"

# 状态 → 颜色 & 文字
STATE_STYLE = {
    BridgeState.STOPPED: ("#9e9e9e", "已停止"),
    BridgeState.STARTING: ("#2196f3", "启动中"),
    BridgeState.RUNNING: ("#4caf50", "运行中"),
    BridgeState.ERROR: ("#f44336", "错误"),
    BridgeState.WAITING: ("#ff9800", "等待设备"),
}


class AddEditBridgeDialog(tk.Toplevel):
    """添加/编辑桥接配置的弹窗"""

    def __init__(self, parent, config: BridgeConfig = None, title="添加桥接"):
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

        # 按钮
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_frame, text="取消", command=self._on_cancel, width=10).pack(
            side="right"
        )
        ttk.Button(btn_frame, text="确定", command=self._on_ok, width=10).pack(
            side="right", padx=(8, 0)
        )

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
        self._title = "心跳 / 管理服务器设置"
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

        ttk.Label(
            main, text="UDP 心跳注册到管理服务器", font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            main,
            text="模块ID/消息类型/端口需与管理服务器一侧保持一致",
            foreground=COLOR_TEXT_SEC,
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # (label, attr, type)
        fields = [
            ("服务器地址", "server_ip", "str"),
            ("服务器端口", "server_port", "int"),
            ("模块ID / MAC", "macaddr", "str"),
            ("源模块ID", "client_module_id", "int"),
            ("服务端模块ID", "server_module_id", "int"),
            ("心跳消息类型", "msgtype_heartbeat", "int"),
            ("ACK 消息类型", "msgtype_ack", "int"),
            ("ERR 消息类型", "msgtype_err", "int"),
            ("初始间隔(秒)", "interval", "float"),
            ("最大丢失重置", "max_lost_reset", "int"),
            ("本地端口", "local_port", "int"),
        ]

        self._widgets = {}
        for i, (label, attr, vtype) in enumerate(fields):
            r = i + 2
            ttk.Label(main, text=label, width=14).grid(
                row=r, column=0, sticky="w", pady=3, padx=(0, 8)
            )
            widget = ttk.Entry(main, width=26)
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

        # 按钮
        btn_frame = ttk.Frame(main)
        btn_frame.grid(
            row=len(fields) + 3, column=0, columnspan=2, pady=(12, 0), sticky="e"
        )
        ttk.Button(btn_frame, text="确定", command=self._on_ok, width=10).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(btn_frame, text="取消", command=self._on_cancel, width=10).pack(
            side="right"
        )

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


class USB2NetApp:
    """主应用窗口"""

    CONFIG_FILE = os.path.join(_APP_DIR, "config.json")

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("USB2Net - 网络串口服务器")
        self.root.geometry("960x640")
        self.root.minsize(800, 520)
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

        # 心跳配置 & 客户端
        self.heartbeat_cfg = HeartbeatConfig()
        self.heartbeat = HeartbeatClient(
            self.heartbeat_cfg,
            log_cb=self._on_log,
            state_cb=self._on_heartbeat_state,
            baudrate_cb=self._on_baudrate_command,
        )

        # Web 终端：每个桥接一个独立、可配置端口（默认 9211~9214）
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
        )
        style.configure(
            "TEntry",
            fieldbackground=COLOR_CARD,
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            insertcolor=COLOR_TEXT,
        )

        # 卡片 / 标题（预留）
        style.configure(
            "Card.TFrame", background=COLOR_CARD, relief="solid", borderwidth=1
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

        # 分组框（对话框内的「串口设置 / 网络设置」）
        style.configure(
            "TLabelframe",
            background=COLOR_BG,
            foreground=COLOR_TEXT_SEC,
            bordercolor=COLOR_BORDER,
            relief="groove",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=COLOR_BG,
            foreground=COLOR_PRIMARY_DARK,
            font=("Segoe UI", 9, "bold"),
        )

        # 扁平彩色按钮（padding 营造圆润大气感）
        style.configure(
            "Primary.TButton",
            background=COLOR_PRIMARY,
            foreground="white",
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            padding=(12, 5),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLOR_PRIMARY_DARK), ("disabled", "#bdbdbd")],
        )
        style.configure(
            "Success.TButton",
            background=COLOR_SUCCESS,
            foreground="white",
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            padding=(12, 5),
        )
        style.map("Success.TButton", background=[("active", "#388e3c")])
        style.configure(
            "Danger.TButton",
            background=COLOR_ERROR,
            foreground="white",
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            padding=(12, 5),
        )
        style.map("Danger.TButton", background=[("active", "#c62828")])

        # Treeview：宽松行高 + 选中高亮 + 浅色精致表头
        style.configure(
            "Treeview",
            background=COLOR_CARD,
            fieldbackground=COLOR_CARD,
            foreground=COLOR_TEXT,
            rowheight=30,
            font=("Segoe UI", 9),
            selectbackground=COLOR_PRIMARY,
            selectforeground="white",
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background="#e8eef5",
            foreground=COLOR_PRIMARY_DARK,
            relief="flat",
            borderwidth=1,
        )
        style.map("Treeview.Heading", background=[("active", "#d6e4f0")])

        # 状态行 tag：仅活动/异常态上色（运行中绿、等待橙、错误红、其余中性灰）
        self.tree.tag_configure("st_ok", foreground=COLOR_SUCCESS)
        self.tree.tag_configure(
            "st_warn", foreground=COLOR_WARNING, font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure(
            "st_err", foreground=COLOR_ERROR, font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure("st_idle", foreground=COLOR_TEXT_SEC)

    @staticmethod
    def _darken(hex_color, factor=0.9):
        """将十六进制颜色按比例调暗，用于按钮 hover 高亮。"""
        hex_color = hex_color.lstrip("#")
        r, g, b = (
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )
        return f"#{max(0, int(r * factor)):02x}{max(0, int(g * factor)):02x}{max(0, int(b * factor)):02x}"

    def _make_ghost_btn(self, parent, text, command):
        """顶栏幽灵按钮：透明底 + 白字，hover 时填充强调蓝。"""
        btn = tk.Button(
            parent,
            text=text,
            bg=COLOR_HEADER,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=5,
            bd=0,
            highlightthickness=0,
            command=command,
        )
        hover = self._darken(COLOR_HEADER_ACCENT, 0.85)

        def on_enter(e):
            btn.config(bg=hover)

        def on_leave(e):
            btn.config(bg=COLOR_HEADER)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _make_stat_cell(self, parent, caption, accent):
        """概览统计条中的单个单元（大数值 + 小标题）。"""
        cell = tk.Frame(parent, bg=COLOR_CARD)
        cell.pack(side="left", fill="y", expand=True, padx=4, pady=6)
        val = tk.Label(
            cell, text="-", bg=COLOR_CARD, fg=accent, font=("Segoe UI", 15, "bold")
        )
        val.pack(anchor="center")
        cap = tk.Label(
            cell, text=caption, bg=COLOR_CARD, fg=COLOR_TEXT_SEC, font=("Segoe UI", 8)
        )
        cap.pack(anchor="center")
        return val

    def _build_ui(self):
        # ── 顶部标题栏 ──
        header = tk.Frame(self.root, bg=COLOR_HEADER, height=54)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        if self._header_logo:
            tk.Label(header, image=self._header_logo, bg=COLOR_HEADER).pack(
                side="left", padx=(16, 4)
            )
        tk.Label(
            header,
            text="USB2Net",
            bg=COLOR_HEADER,
            fg="white",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left", padx=(4, 8))
        tk.Label(
            header,
            text="网络串口服务器",
            bg=COLOR_HEADER,
            fg="#cbd5e1",
            font=("Segoe UI", 10),
        ).pack(side="left", padx=2)

        # 右侧按钮（幽灵风格，hover 高亮）
        btn_area = tk.Frame(header, bg=COLOR_HEADER)
        btn_area.pack(side="right", padx=14)

        self._btn_add = self._make_ghost_btn(btn_area, "+ 添加桥接", self._on_add)
        self._btn_add.pack(side="left", padx=4)

        self._btn_start_all = self._make_ghost_btn(
            btn_area, "全部启动", self._on_start_all
        )
        self._btn_start_all.pack(side="left", padx=4)

        self._btn_stop_all = self._make_ghost_btn(
            btn_area, "全部停止", self._on_stop_all
        )
        self._btn_stop_all.pack(side="left", padx=4)

        self._btn_heartbeat = self._make_ghost_btn(
            btn_area, "心跳设置", self._on_heartbeat_settings
        )
        self._btn_heartbeat.pack(side="left", padx=4)

        # ── 概览统计条 ──
        overview = tk.Frame(self.root, bg=COLOR_BG)
        overview.pack(fill="x", side="top", padx=12, pady=(8, 0))
        ov_card = tk.Frame(overview, bg=COLOR_CARD, bd=1, relief="solid")
        ov_card.pack(fill="x")

        self._stat_total = self._make_stat_cell(ov_card, "桥接总数", COLOR_PRIMARY)
        self._stat_running = self._make_stat_cell(ov_card, "运行中", COLOR_SUCCESS)
        self._stat_traffic = self._make_stat_cell(
            ov_card, "累计流量", COLOR_HEADER_ACCENT
        )
        self._stat_hb = self._make_stat_cell(ov_card, "心跳", COLOR_WARNING)

        # ── 主体内容 ──
        body = tk.Frame(self.root, bg=COLOR_BG)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 8))

        # ── 桥接列表区 ──
        list_frame = tk.Frame(body, bg=COLOR_BG)
        list_frame.pack(fill="both", expand=True)

        # Treeview
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
            ("state", "状态", 80, "center"),
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
        op_frame.pack(fill="x", pady=4)

        ttk.Button(
            op_frame, text="启动选中", command=self._ctx_start, style="Success.TButton"
        ).pack(side="left", padx=3)
        ttk.Button(
            op_frame, text="停止选中", command=self._ctx_stop, style="Danger.TButton"
        ).pack(side="left", padx=3)
        ttk.Button(
            op_frame,
            text="Web 终端",
            command=self._ctx_web_term,
            style="Primary.TButton",
        ).pack(side="left", padx=3)
        ttk.Button(op_frame, text="编辑", command=self._ctx_edit).pack(
            side="left", padx=3
        )
        ttk.Button(
            op_frame, text="删除", command=self._ctx_delete, style="Danger.TButton"
        ).pack(side="left", padx=3)
        ttk.Button(op_frame, text="刷新串口列表", command=self._refresh_ports).pack(
            side="left", padx=3
        )

        # 波特率实时调整（选中桥接后，下拉选值并点“应用”即实时切换串口波特率）
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

        # ── 日志面板 ──
        log_frame = tk.LabelFrame(
            self.root,
            text=" 数据日志 ",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=4,
        )
        log_frame.pack(fill="both", expand=False, padx=12, pady=(0, 4))
        log_frame.configure(height=200)
        log_frame.pack_propagate(False)

        # 日志工具栏
        log_toolbar = tk.Frame(log_frame, bg=COLOR_BG)
        log_toolbar.pack(fill="x", pady=(0, 4))

        self._hex_var = tk.BooleanVar()
        tk.Checkbutton(
            log_toolbar,
            text="HEX 显示",
            variable=self._hex_var,
            bg=COLOR_BG,
            command=self._toggle_hex,
        ).pack(side="left", padx=4)

        self._autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            log_toolbar,
            text="自动滚动",
            variable=self._autoscroll_var,
            bg=COLOR_BG,
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
        ).pack(side="right", padx=4)

        # 日志文本框
        log_text_frame = tk.Frame(log_frame, bg=COLOR_BORDER, bd=1)
        log_text_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_text_frame,
            wrap="none",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            state="disabled",
            relief="flat",
            padx=6,
            pady=4,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(
            log_text_frame, orient="vertical", command=self.log_text.yview
        )
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # 日志颜色 tag
        self.log_text.tag_config("timestamp", foreground="#858585")
        self.log_text.tag_config("info", foreground="#4fc3f7")
        self.log_text.tag_config("warn", foreground="#ffb74d")
        self.log_text.tag_config("error", foreground="#ef5350")
        self.log_text.tag_config("rx", foreground="#81c784")
        self.log_text.tag_config("tx", foreground="#ce93d8")
        self.log_text.tag_config("system", foreground="#90a4ae")

        # ── 状态栏 ──
        self.status_bar = tk.Label(
            self.root,
            text=" 就绪",
            bg="#e0e0e0",
            fg=COLOR_TEXT_SEC,
            anchor="w",
            font=("Segoe UI", 9),
            padx=10,
            pady=3,
        )
        self.status_bar.pack(fill="x", side="bottom")

    # ── 事件处理 ──

    def _on_add(self):
        # 计算下一个默认名称：网络串口-N（N 取已有同名序号最大值 +1，避免撞名）
        max_n = 0
        for b in self.manager.get_all_bridges().values():
            m = re.match(r"^网络串口-(\d+)$", b.config.name or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
        default_cfg = BridgeConfig(name=f"网络串口-{max_n + 1}")
        dlg = AddEditBridgeDialog(self.root, config=default_cfg, title="添加桥接")
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
            self._on_log("info", f"已添加桥接: {dlg.result.name}")
            url = self.webhub.url_for(bridge_id)
            self._on_log(
                "info",
                f"Web 终端地址: {url}"
                if url
                else f"Web 终端: 未配置端口（可在编辑中设置）",
            )
            self._save_config()

    def _on_start_all(self):
        self.manager.start_all()
        self._on_log("info", "已启动所有已启用的桥接")

    def _on_stop_all(self):
        self.manager.stop_all()
        self._on_log("info", "已停止所有桥接")

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
        """选中桥接时，把当前波特率回填到下拉框"""
        bid = self._get_selected_bridge_id()
        if bid is None:
            return
        bridge = self.manager.get_bridge(bid)
        if bridge:
            self._baud_combo.set(str(bridge.config.baudrate))

    def _on_apply_baud(self):
        """把下拉框里的波特率实时应用到选中的桥接"""
        bid = self._get_selected_bridge_id()
        if bid is None:
            messagebox.showwarning(
                "提示", "请先在列表中选择一个桥接。", parent=self.root
            )
            return
        bridge = self.manager.get_bridge(bid)
        if bridge is None:
            return

        raw = self._baud_combo.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请输入波特率。", parent=self.root)
            return
        try:
            baud = int(raw)
        except ValueError:
            messagebox.showerror(
                "输入错误", f"波特率必须是数字：{raw}", parent=self.root
            )
            return
        if baud <= 0:
            messagebox.showerror("输入错误", "波特率必须为正数。", parent=self.root)
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

    def _ctx_start(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            self.manager.start_bridge(bid)

    def _ctx_stop(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            self.manager.stop_bridge(bid)

    def _ctx_web_term(self):
        bid = self._get_selected_bridge_id()
        if bid is not None:
            self._open_web_term(bid)

    def _open_web_term(self, bridge_id: int):
        """在浏览器打开该桥接的 Web 终端页面（固定端口，无需 id）"""
        url = self.webhub.url_for(bridge_id)
        bridge = self.manager.get_bridge(bridge_id)
        name = bridge.config.name if bridge else f"Bridge-{bridge_id}"
        if not url:
            self._on_log(
                "warn", f"[{name}] 未配置 Web 终端端口，请在编辑中设置 Web 端口。"
            )
            messagebox.showwarning(
                "提示",
                f"[{name}] 未配置 Web 终端端口。\n"
                f"请编辑该桥接并填写“Web 端口”（如 9211）。",
                parent=self.root,
            )
            return
        if bridge and bridge.state != BridgeState.RUNNING:
            self._on_log(
                "warn",
                f"[{name}] 桥接未运行，Web 终端可能无法连接串口。请先启动该桥接。",
            )
        self._on_log("info", f"正在打开 Web 终端: {url}")
        open_browser(url)

    def _ctx_edit(self):
        bid = self._get_selected_bridge_id()
        if bid is None:
            return
        bridge = self.manager.get_bridge(bid)
        if not bridge:
            return
        dlg = AddEditBridgeDialog(self.root, config=bridge.config, title="编辑桥接")
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
            self._on_log("info", f"已更新桥接: {dlg.result.name}")
            self._save_config()

    def _ctx_delete(self):
        bid = self._get_selected_bridge_id()
        if bid is None:
            return
        if messagebox.askyesno("确认", "确定要删除这个桥接配置吗？"):
            self.manager.remove_bridge(bid)
            self.webhub.unregister(bid)
            iid = self._bridge_rows.pop(bid, None)
            if iid:
                self.tree.delete(iid)
            self._on_log("info", f"已删除桥接 (ID: {bid})")
            self._save_config()

    def _refresh_ports(self):
        ports = BridgeManager.list_serial_ports()
        if ports:
            port_list = "\n".join(f"  {p[0]} - {p[1]}" for p in ports)
            self._on_log("info", f"检测到 {len(ports)} 个串口:\n{port_list}")
        else:
            self._on_log("warn", "未检测到任何串口设备")

    def _toggle_hex(self):
        self._show_hex = self._hex_var.get()

    def _toggle_autoscroll(self):
        self._auto_scroll = self._autoscroll_var.get()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

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
                "已停止",
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
        """主线程：按 tcp_port 找到对应桥接并实时切换串口波特率"""
        target = self.manager.get_bridge_by_tcp_port(tcp_port)
        if not target:
            self._on_log(
                "warn",
                f"收到服务端波特率指令，但无 TCP端口={tcp_port} 的桥接，已忽略 "
                f"(ttycode={info.get('ttycode')})",
            )
            return
        bid = target.id if hasattr(target, "id") else None
        name = target.config.name
        try:
            target.set_baudrate(baudrate)
            if bid is not None:
                self._update_tree_row(bid, target.config)  # 刷新波特率列
                # 若当前选中的正是该桥接，同步刷新操作区波特率下拉，避免页面仍显示旧值
                if self._get_selected_bridge_id() == bid:
                    self._baud_combo.set(str(target.config.baudrate))
            self._on_log(
                "info",
                f"[{name}] 服务端指令：波特率已切换为 {baudrate} "
                f"(ttycode={info.get('ttycode')}, 原 {info.get('obaudrate')})",
            )
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
        self.log_text.insert("end", f"[{ts}] ", "timestamp")
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
        self.log_text.insert("end", f"[{ts}] ", "timestamp")
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

        # 概览统计条
        total_bridges = len(stats)
        running = sum(1 for s in stats.values() if s["state"] == "运行中")
        traffic = sum(
            s["bytes_serial_to_tcp"] + s["bytes_tcp_to_serial"] for s in stats.values()
        )
        hb = self.heartbeat.state
        self._stat_total.config(text=str(total_bridges))
        self._stat_running.config(text=str(running))
        self._stat_traffic.config(text=self._fmt_bytes(traffic))
        self._stat_hb.config(text=hb)

        # 状态栏
        self.status_bar.config(
            text=f" 桥接总数: {total_bridges}  |  运行中: {running}  |  "
            f"心跳: {hb}  |  {datetime.now().strftime('%H:%M:%S')}"
        )

        self.root.after(1000, self._refresh_stats)

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
            data = {"bridges": bridges, "heartbeat": self.heartbeat_cfg.to_dict()}
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

            # 桥接配置
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
                self._on_log("system", f"已加载 {len(configs)} 条桥接配置")
                self._on_log(
                    "system",
                    "Web 终端已就绪（每桥接独立端口，见各桥接的“Web 终端”按钮）",
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
        except Exception as e:
            self._on_log("error", f"加载配置失败: {e}")

    # ── 关闭 ──

    def _on_close(self):
        if messagebox.askyesno("退出确认", "确定要退出吗？所有活动的桥接将被停止。"):
            self._ui_running = False
            self._on_log("system", "正在停止所有桥接、心跳与 Web 终端并退出...")
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


if __name__ == "__main__":
    try:
        app = USB2NetApp()
        app.run()
    except Exception as exc:
        _show_crash_error(exc)

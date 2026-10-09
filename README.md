# USB2Net — 网络串口服务器（PC 桌面版）

将 USB/串口设备通过 TCP 服务暴露，配合 UDP 心跳向服务端注册，并提供 Web 终端（xterm.js）远程访问的 Windows 桌面工具。

![平台](https://img.shields.io/badge/platform-Windows-blue) ![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![许可](https://img.shields.io/badge/license-MIT-green)

## 功能

- **多路串口 ↔ TCP 桥接**：默认 4 路，TCP 端口 9001–9004，可增删改
- **Web 终端**：每路独立 HTTP + WebSocket（xterm.js），默认端口 9211–9214，浏览器直接连
- **UDP 心跳注册**：向服务端上报本机标识，支持服务端下发 `msgtype:1025` 动态波特率指令，并回发确认包
- **IP 上报（GetCmd）**：独立于心跳的地址上报通道
- **动态波特率**：主界面下拉框实时修改 / 应用，无需重启
- **Telnet 协商兼容**：`FF FE FD` + 波特率 ASCII 指令格式
- **单实例运行**：同一台机器只允许一个实例，重复启动会弹窗提示并把已有窗口顶到前台

## 快速开始

```bat
scripts\run.bat
```

脚本会自动检测 Python、安装缺失依赖（pyserial / websockets）并启动 GUI，无需手动准备环境。

## 目录结构

```
usb2net/
├── src/                              # 全部源码与配置
│   ├── main.py                       # GUI 主程序（tkinter，含界面与交互逻辑）
│   ├── bridge.py                     # 串口 ↔ TCP 桥接核心
│   ├── heartbeat.py                  # UDP 心跳 + 服务端指令处理
│   ├── webterm.py                    # Web 终端（WebSocket 桥接）
│   ├── single_instance.py            # 单实例守卫（命名互斥体 + 窗口置前）
│   ├── config.json                   # 运行配置（桥接 / 心跳 / IP 上报）
│   ├── requirements.txt              # Python 运行时依赖
│   ├── USB2Net.spec                  # PyInstaller 构建配置
│   ├── USB2Net.ico                   # 应用图标（打包进 exe）
│   ├── USB2Net.png                   # 窗口图标（PNG，兼容性更好，优先使用）
│   └── static/                       # Web 终端前端（xterm.js、addon-fit.js）
├── scripts/                          # Windows 批处理
│   ├── run.bat                       # 本地运行（自动检测环境并装依赖）
│   ├── build.bat                     # 构建 Windows exe
│   └── clean.bat                     # 清理构建产物
├── gen_icon.py                       # 用 Pillow 重新生成默认图标
├── .github/workflows/build.yml       # GitHub Actions 自动构建
├── .gitignore
└── README.md
```

## 构建

### 构建 exe

```bat
scripts\build.bat
```

首次运行会在项目根创建 `.venv` 并安装依赖（pyinstaller / pyserial / websockets），之后重编很快。产物：`dist/USB2Net.exe`（构建在 `src/` 内执行，产物输出到仓库根 `dist/`）。

### 清理构建产物

```bat
scripts\clean.bat
```

清理 `build/`、`dist/`、`.venv/`、`__pycache__/` 等，源码与配置会保留。完全重来时，先跑它再跑 `build.bat`。

### 单实例运行机制

程序启动时先申请 Windows 命名互斥体（`Local\USB2Net_SingleInstance_Mutex_v1`），并在实例内部开一个仅监听 `127.0.0.1` 的"激活服务"：

- 判重发生在**创建任何串口 / 心跳 / Web 终端之前**，因此重复启动不会产生多余后台线程或端口占用
- 检测到已有实例时，向其发送置前信号，并弹出「USB2Net 已在运行」提示窗（点击"知道了"或按回车 / Esc 关闭），随后新进程立即退出（`exit code 0`）
- 若置前信号发送失败（已有实例正在退出途中），提示窗会给出对应的稍后重试文案
- 提示窗使用 `topmost` 并周期性拉回顶层，避免被刚置前的已有实例窗口盖住

选用内核命名对象而非锁文件，是因为它随进程退出由系统自动释放，不会留下需要手工清理的死锁；开发模式与打包后的 exe 行为一致。非 Windows 平台会退化为本地 TCP 端口探测。

### 应用图标

`src/USB2Net.ico` 是打包进 `USB2Net.exe` 的图标，由 `USB2Net.spec` 的 `icon=` 引用。运行窗口的图标优先取 `src/USB2Net.png`（`iconphoto` 兼容性更好），缺失时回退到 `.ico`。

更换图标：直接覆盖 `src/USB2Net.ico` 和 `src/USB2Net.png` 即可（建议包含 16/32/48/256 多档尺寸）。若要改名，需同步修改 `USB2Net.spec` 的 `icon=` 路径与 `USB2Net.spec` 的 `datas` 列表。

仓库根的 `gen_icon.py` 可用 Pillow 重新生成当前这套默认图标，方便调整配色或图形。

## 自动构建（GitHub Actions）

push 到 `master`、提交 PR 到 `master`、打 `v*` 标签，或手动触发时，自动在 Windows 环境用 PyInstaller 构建：

- **Artifacts**：到仓库 Actions 页面对应运行下载 `USB2Net.exe`
- **Release**：打 `v*` 标签并 push 时自动创建 Release 并附上 exe

首次推送：

```bash
git remote add origin <你的仓库地址>
git push -u origin master
```

> CI 配置的触发分支是 `master`。若仓库默认分支为 `main`，到 **Settings → Branches** 改默认分支，或用 `gh repo edit --default-branch master`。

## 配置说明

编辑 `src/config.json`（程序退出时自动保存界面上的改动）。

### `bridges[]` — 桥接通道

| 字段 | 说明 |
| --- | --- |
| `name` | 通道显示名 |
| `serial_port` | 设备路径，Windows 如 `COM11`，Linux 如 `/dev/ttyUSB0` |
| `baudrate` | 波特率 |
| `tcp_port` | TCP 服务端口 |
| `web_port` | Web 终端端口 |
| `enabled` | 是否启用 |
| `databits` / `parity` / `stopbits` | 串口帧格式，默认 `8 / N / 1` |
| `flow_control` | 流控，默认 `none` |
| `auto_reconnect` | 串口断开后自动重连 |
| `telnet_mode` | Telnet 协商兼容 |

### `heartbeat` — UDP 心跳

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用心跳 |
| `server_ip` / `server_port` | 服务端地址与端口（默认 `192.168.96.248:7777`） |
| `macaddr` | 本机标识，**需按实际机器修改** |
| `interval` | 心跳间隔（秒） |
| `max_lost_reset` | 连续丢失多少个心跳后判定掉线 |
| `local_port` / `bind_local_port` | 本地端口及是否显式绑定 |
| `msgtype_report_ser2net` | 波特率指令的确认包类型（默认 `13`） |

服务端下发的 `msgtype:1025` 指令用于下发波特率变更，收到后回发确认包。

### `getcmd` — IP 上报

独立于心跳的上报通道，含 `version`、`ipaddr`、`msgtype`、`src_moduleid`、`dst_moduleid` 等字段。

> `macaddr`、`ipaddr` 等与具体机器相关的字段默认值仅作占位，部署时请按实际情况修改。

## 开发约定

- **线程安全**：工作线程只往 `_ui_queue` 塞任务，由主线程 `_drain_ui_queue` 取出执行；跨线程调度统一走 `_call_on_main(fn, *args)`，**不要直接碰 tkinter**
- **启动错误**：入口异常统一走 `_show_crash_error(exc)` 弹窗，避免静默闪退
- **依赖缺失**：pyserial / websockets 在 import 处捕获并弹窗提示

## 许可

MIT
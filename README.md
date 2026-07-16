# USB2Net — 网络串口服务器（PC 桌面版）

将 USB/串口设备通过 TCP 服务暴露，配合 UDP 心跳向服务端注册，并提供 Web 终端（xterm.js）远程访问的 Windows 桌面工具。

## 功能

- 多路串口 ↔ TCP 服务端桥接（默认 4 路，TCP 端口 9001–9004）
- Telnet 协商兼容；支持动态波特率指令（`FF FE FD` + 波特率 ASCII）
- UDP 心跳注册到服务端（默认 `192.168.96.248:7777`），支持服务端下发的 `msgtype:1025` 波特率指令，并回发 `msgtype:13` 确认包
- 每路独立 Web 终端（单端口 HTTP + WebSocket，默认端口 9211–9214）
- 主界面下拉框可实时修改 / 应用波特率

## 目录结构

```
usb2net/
├── src/                         # 全部源码与配置
│   ├── main.py                  # GUI 主程序（tkinter）
│   ├── bridge.py                # 串口 ↔ TCP 桥接核心
│   ├── heartbeat.py             # UDP 心跳 + 服务端指令处理
│   ├── webterm.py               # Web 终端（xterm.js 静态资源 + WebSocket 桥接）
│   ├── config.json              # 运行配置（桥接 / 心跳 / Web 端口）
│   ├── requirements.txt         # Python 运行时依赖
│   ├── USB2Net.spec             # PyInstaller 构建配置
│   ├── USB2Net.ico              # 应用图标（打包进 exe）
│   └── static/                  # Web 终端前端（xterm.js、addon-fit.js）
├── scripts/                     # Windows 批处理（构建 / 清理 / 运行）
│   ├── build.bat                # 构建 Windows exe
│   ├── clean.bat                # 清理构建产物（含 .venv）
│   └── run.bat                  # 本地直接运行（自动装依赖）
├── .github/workflows/build.yml  # GitHub Actions 自动构建
├── .gitignore
└── README.md
```

## 本地使用

### 运行（开发 / 自测）

```bat
scripts\run.bat
```

会自动检测 Python、安装缺失依赖（pyserial / websockets），并启动 GUI。

### 构建 exe

```bat
scripts\build.bat
```

首次运行会在项目根自动创建 `.venv` 并安装依赖（pyinstaller / pyserial / websockets），之后重编很快。产物：`dist/USB2Net.exe`（构建过程在 `src/` 内执行，产物输出到仓库根 `dist/`）。

### 清理构建产物

```bat
scripts\clean.bat
```

清理 `build/`、`dist/`、`.venv/`、`__pycache__/` 等所有构建产物（如需完全重来，运行它后再跑 `build.bat` 会重新装依赖）。

### 应用图标

`src/USB2Net.ico` 是打包进 `USB2Net.exe` 的图标，由 `USB2Net.spec` 中的 `icon='USB2Net.ico'` 引用，本地 `scripts\build.bat` 与 GitHub Actions 都会自动采用。

更换成自己的图标：用任意图标工具（如 GIMP、在线 ICO 转换器等）导出一个 `.ico` 文件，**直接覆盖** `src/USB2Net.ico` 即可（建议包含 16/32/48/256 等多档尺寸以保证清晰）。如需改名，同步修改 `USB2Net.spec` 里的 `icon=` 路径后重新构建。

> 仓库根目录的 `gen_icon.py` 可用 Pillow 重新生成当前这套默认图标，方便二次调整配色或图形。

## 自动构建（GitHub Actions）

push 到 `master` 分支或打 `v*` 标签时，自动在 Windows 环境用 PyInstaller 构建 `USB2Net.exe`：

- 普通 push / PR：到仓库 **Actions** 页面，从对应运行的 **Artifacts** 下载 `USB2Net.exe`
- 打标签（如 `v1.0.0`）并 push：自动创建 GitHub **Release**，并将 `USB2Net.exe` 作为附件

首次推送到 GitHub：

```bash
git remote add origin <你的仓库地址>
git push -u origin master
```

推送后若仓库默认分支仍为 `main`，到仓库 **Settings → Branches** 将默认分支改为 `master`（命令行可用 `gh repo edit --default-branch master`）。改完后每次 push 到 `master` 都会触发自动构建。`gen_icon.py` 与 `src/USB2Net.ico` / `src/USB2Net.png` 均已纳入版本管理，CI 能取到图标并打包进 exe 及窗口图标。

## 配置说明

编辑 `src/config.json`：

- `bridges[]`：每路桥接的 `serial_port`（设备路径，Windows 如 `COM11`，Linux 如 `/dev/ttyUSB0`）、
  `tcp_port`、`baudrate`、`web_port`、`enabled`
- `heartbeat`：`enabled`、`server`（服务端地址）、`macaddr`（本机标识）等

> 注意：`build/`、`dist/`、`__pycache__/` 等已由 `.gitignore` 忽略，不应提交到仓库。

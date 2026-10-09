"""
single_instance.py - 单实例运行保护

需求：同一台机器上只允许一个 USB2Net 实例运行。重复启动时不新建后台进程，
而是把已经运行的窗口顶到前台，然后退出新进程。

实现要点：
  - 主判重手段：Windows 命名互斥体（CreateMutexW）。
    命名内核对象由系统持有，进程一旦异常退出内核会自动释放，
    不会像"锁文件"那样留下需要手工清理的死锁。开发模式与 PyInstaller
    打包后的 exe 都适用（依赖内核对象而非可执行文件路径）。
  - 窗口激活：新实例检测到已有实例后，向第一个实例在本机开的
    "激活服务"（127.0.0.1 上的单连接 TCP 端口）发一个信号，
    第一个实例收到后把自己的窗口恢复到前台。
  - 非 Windows 平台：退化用本地 TCP 端口探测作为判重依据。
"""

import os
import socket
import threading

# Windows 命名互斥体名称。Local\ 前缀表示"当前登录会话"，
# 同一用户重复登录/多用户各自跑一份互不影响，也避免需要全局权限。
MUTEX_NAME = r"Local\USB2Net_SingleInstance_Mutex_v1"

# 窗口激活服务端口（仅本机回环，不对外暴露）
ACTIVATE_HOST = "127.0.0.1"
ACTIVATE_PORT = 59321

_IS_WIN = os.name == "nt"


class SingleInstance:
    """
    单实例守卫。用法：

        guard = SingleInstance()
        if not guard.acquire():
            guard.notify_running()          # 通知已有实例激活窗口
            sys.exit(0)
        guard.start_activation_server(on_activate)   # 已有实例才需要开
        ...
        guard.release()                     # 退出时释放（可选）
    """

    def __init__(self, name=MUTEX_NAME, port=ACTIVATE_PORT):
        self._name = name
        self._port = port
        self._mutex = None      # ctypes HANDLE
        self._own = False       # 是否由本进程持有
        self._server = None     # 激活服务 socket
        self._stop_evt = threading.Event()
        self._thread = None

    # ── 判重 / 加锁 ──

    def acquire(self):
        """尝试成为唯一实例。返回 True=本进程是首个实例，False=已有实例在跑。"""
        if _IS_WIN:
            return self._acquire_win()
        return self._acquire_port()

    def _acquire_win(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, self._name)
        err = ctypes.get_last_error()
        if not handle:
            # 创建失败（极端情况）时不能误判成"已存在"，否则会永久无法启动，
            # 退化为端口判重。
            return self._acquire_port()

        # ERROR_ALREADY_EXISTS(183) 表示同名互斥体已存在 → 已有实例在跑
        if err == 183:
            kernel32.CloseHandle(handle)
            return False

        self._mutex = handle
        self._own = True
        return True

    def _acquire_port(self):
        """非 Windows / 互斥体不可用时的兜底：抢占激活端口即视为首个实例。"""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((ACTIVATE_HOST, self._port))
            srv.listen(1)
            self._server = srv
            self._own = True
            return True
        except OSError:
            return False

    # ── 通知已有实例激活窗口 ──

    def notify_running(self):
        """给已在运行的实例发一个"把我顶到前台"的信号，失败也不影响退出。"""
        try:
            with socket.create_connection((ACTIVATE_HOST, self._port), timeout=1.0) as s:
                s.sendall(b"activate")
            return True
        except OSError:
            return False

    # ── 激活服务（只有首个实例需要开） ──

    def start_activation_server(self, on_activate):
        """后台线程监听激活请求，收到信号后执行 on_activate 回调。"""
        srv = self._server
        if srv is None:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((ACTIVATE_HOST, self._port))
                srv.listen(1)
                self._server = srv
            except OSError:
                return False

        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._serve,
            args=(srv, on_activate),
            daemon=True,
            name="single-instance-activator",
        )
        self._thread.start()
        return True

    def _serve(self, srv, on_activate):
        srv.settimeout(0.5)
        while not self._stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                if conn.recv(64) == b"activate":
                    try:
                        on_activate()
                    except Exception:
                        pass
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    # ── 释放 ──

    def release(self):
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._own and self._mutex is not None:
            import ctypes

            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None
            self._own = False


def raise_window(root):
    """把 tkinter 窗口恢复并置前。"""
    try:
        root.deiconify()
        root.lift()
        root.focus_force()
    except Exception:
        pass
    if not _IS_WIN:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 配合常见的"锁定窗口到任务栏"限制：先置顶再还原
        root_id = root.winfo_id()
        parent = user32.GetParent(root_id) or root_id
        SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
        user32.SetWindowPos(parent, 0, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
        user32.SetWindowPos(parent, 0, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
    except Exception:
        pass


if __name__ == "__main__":
    # 自检：手动运行本文件可快速验证锁是否有效
    import sys

    g = SingleInstance()
    first = g.acquire()
    print("首个实例" if first else "检测到已有实例")
    if first:
        g.start_activation_server(lambda: None)
        g.release()
    sys.exit(0)
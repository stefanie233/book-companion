"""留白（LiuBai）— 桌面版入口：原生窗口 + 只启 Web 服务，不带 CLI。

窗口用 pywebview 承载（Windows 下是 WebView2），关窗即退出、服务同停；
pywebview 不可用或 WebView2 初始化失败时，退回 Edge ``--app`` 窗口，再退回
系统浏览器。这是 PyInstaller 冻结产物的入口，也是开发期验证「装完就能用」
的那条路径（``python desktop.py``）。

服务跑在后台线程的独立事件循环里（主线程必须留给 pywebview 的 GUI 循环）。

冻结后同一个 exe 还兼任子进程脚本宿主：``--run-script <name>`` 由
``runtime_paths.run_script`` 用 runpy 执行 ``scripts/<name>``，让作者细节
采集这类独立进程在安装版里照常工作（源码版的 ``sys.executable`` 是 python）。

CLI 入口 ``main.py`` 保留给源码开发者，不参与打包。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
PORT = int(os.environ.get("BC_PORT", "8766") or "8766")
APP_TITLE = "留白"
WINDOW_SIZE = (1280, 900)
WINDOW_MIN_SIZE = (960, 640)


# ── 进程级兜底 ────────────────────────────────────────────────────────────

def _ensure_stdio() -> None:
    """windowed 产物里 stdout/stderr 可能是 None，print 会炸。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _fatal(message: str) -> None:
    """启动失败要让人看见：先写 stderr，Windows 再弹一个消息框。"""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
        except Exception:
            pass


def _port_occupied(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _edge_exe() -> str:
    """Edge 可执行文件；找不到返回空串。"""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _open_app_window(url: str) -> bool:
    """用 Edge ``--app`` 开一个无地址栏窗口，观感接近独立桌面程序。

    独立 user-data-dir 放在用户根下：不污染日常 Edge 配置，也避免和已开的
    Edge 进程混用标志位。``--disable-background-mode`` 让「关掉最后一个窗口」
    真的结束 Edge 进程，而不是缩进后台常驻。
    找不到 Edge 时返回 False，由调用方退回系统浏览器。
    """
    import subprocess

    exe = _edge_exe()
    if not exe:
        return False
    try:
        from runtime_paths import data_path

        profile = data_path("edge-app")
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [exe, f"--app={url}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-background-mode",
             "--disable-features=msEdgeSidebarV2",
             "--window-size=1280,900"],
            close_fds=True,
        )
        return True
    except Exception:
        return False


def _window_present(title: str) -> bool:
    """当前桌面是否存在标题恰为 ``title`` 的可见顶层窗口（Windows）。

    不用 Edge 进程句柄判活：同一个 user-data-dir 已经跑着时，新起的
    msedge.exe 会把命令转交给旧进程后立即退出，句柄不可靠。窗口标题是
    唯一稳定的「界面还在不在」信号。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hit: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                         wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value.strip() == title:
                        hit.append(1)
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return bool(hit)
    except Exception:
        return True          # 探测失败按「还在」处理，绝不误杀服务


def _focus_window(title: str) -> bool:
    """把已在运行的窗口提到前台；单实例复用用。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hit: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                         wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value.strip() == title:
                        hit.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not hit:
            return False
        hwnd = hit[0]
        user32.ShowWindow(hwnd, 9)       # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _open_ui(url: str, app_window: bool) -> bool:
    """打开界面；返回 True 表示用了 Edge 独立窗口（可监视其生命周期）。"""
    if app_window and _open_app_window(url):
        return True
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return False


async def _await_app_window(title: str = APP_TITLE,
                            appear_timeout: float = 20.0,
                            miss_limit: int = 4,
                            interval: float = 1.5) -> None:
    """等应用窗口出现，再等它消失。

    用户预期：关掉界面 = 退出整个程序。窗口一直没出现（Edge 被杀软拦下等）
    就退化成常驻等待，宁可让用户手动结束进程，也不误杀正在加载的服务。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + appear_timeout
    appeared = False
    while loop.time() < deadline:
        if await asyncio.to_thread(_window_present, title):
            appeared = True
            break
        await asyncio.sleep(0.5)
    if not appeared:
        await asyncio.Event().wait()
        return

    misses = 0
    while True:
        await asyncio.sleep(interval)
        if await asyncio.to_thread(_window_present, title):
            misses = 0
            continue
        misses += 1
        if misses >= miss_limit:
            return


# ── 启动 ──────────────────────────────────────────────────────────────────

async def _start_services(config):
    """把 Web 版需要的所有服务装配好，返回 (api_app, watcher, cleanup)。"""
    from api_server import create_app as create_api_app, warm_asr_background
    from aiohttp import web
    from hooks import HookPipeline
    from memory_manager import MemoryManager
    from memory_logging import setup_memory_logging_from_config
    from memory_store import BuiltinMemoryProvider, MemoryStore
    from persona import save_default_soul
    from provider_store import ProviderStore
    from services.session_service import SessionService
    from services.watcher_service import WatchService
    from storage.db import BookDB
    from storage.db_session import SessionDB
    from runtime import OpenAIClient
    from runtime_paths import data_path

    # 控制台连接档案覆盖 .env：必须在任何字段被读取前应用。
    provider_store = ProviderStore(config.providers_path)
    provider_store.apply_to_config(config)

    setup_memory_logging_from_config(config)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    save_default_soul(config)

    memory_store = MemoryStore(config)
    memory_manager = MemoryManager()
    memory_manager.add_provider(BuiltinMemoryProvider(memory_store))

    from wingman.service import WingmanMemoryProvider
    wingman = WingmanMemoryProvider(config.project_dir / "data" / "memory.db",
                                    config)
    memory_manager.add_provider(wingman)

    books_db = BookDB(config.books_db_path)
    session_db = SessionDB(config.sessions_db_path)
    books_db.initialize()
    session_db.initialize()

    hook_pipeline = HookPipeline()
    hook_pipeline.discover(config.resource_dir / "hooks")
    session_service = SessionService(session_db, config,
                                     hook_pipeline=hook_pipeline)

    # 导入后的自动清理 hook（与 CLI 同一条链路）
    try:
        from main import _register_cleaning_hook
        _register_cleaning_hook(hook_pipeline, books_db)
    except Exception:
        pass

    api_client = OpenAIClient(config)
    from flight_recorder import create_recorder
    context_recorder = create_recorder(config.project_dir)

    # 复用 CLI 的装配函数，避免两套 runtime 构造分叉。
    from main import _setup_runtime, _wire_review_tool_context
    runtime = _setup_runtime(
        config, memory_store, service=session_service,
        hook_pipeline=hook_pipeline, bookbank_db=books_db,
        session_db=session_db, memory_manager=memory_manager,
        api_client=api_client, wingman_db=wingman._db,
        context_recorder=context_recorder,
    )
    memory_manager.initialize_all(
        runtime.session.session_id, api_client=api_client, config=config,
    )
    _wire_review_tool_context(runtime.tool_registry, session_db, wingman._db,
                              api_client, config)

    api_app = create_api_app(books_db, runtime, session_service)
    api_app["api_client"] = api_client
    api_app["config"] = config
    api_app["provider_store"] = provider_store
    api_app["context_recorder"] = context_recorder

    watcher = None
    try:
        watcher = WatchService(books_db, data_path("watch"))
        api_app["watcher"] = watcher
    except Exception as exc:
        print(f"  Watcher: not created ({exc})")

    return api_app, watcher, warm_asr_background


# ── 服务进程（后台线程） ──────────────────────────────────────────────────

@dataclass
class _ServerHandle:
    """一副跑着的服务：runner + 预热任务 + watcher。"""

    runner: Any
    asr_task: asyncio.Task | None
    watcher: Any
    url: str


def _bootstrap():
    """用户根 + 配置。主线程和后台线程都要用，所以单独抽出来。"""
    from config import load_config
    from runtime_paths import ensure_user_layout

    root = ensure_user_layout()
    # 兜底：仍有少量相对路径调用点，cwd 钉在用户根上。
    try:
        os.chdir(root)
    except OSError:
        pass
    return load_config()


async def _serve(config) -> _ServerHandle:
    """装配并启动 HTTP 服务（含预热），返回可关闭的把手。"""
    from aiohttp import web

    api_app, watcher, warm_asr_background = await _start_services(config)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    site = web.TCPSite(api_runner, HOST, PORT, reuse_address=True)
    await site.start()
    url = f"http://{HOST}:{PORT}"
    print(f"{APP_TITLE} 正在运行：{url}")

    # tokenizer 预热：与 CLI 同理，留在监听之前（约 1s，纯冷启动代价）。
    try:
        from context_meter import resolve_profile
        from tokenizer_service import count_tokens
        profile = resolve_profile(getattr(config, "model", ""), config)
        if profile["tokenizer_path"]:
            await asyncio.to_thread(count_tokens, "warmup",
                                    profile["tokenizer_path"])
    except Exception:
        pass

    asr_task = asyncio.create_task(warm_asr_background())

    if watcher is not None:
        try:
            watcher.start()
        except Exception:
            pass

    return _ServerHandle(runner=api_runner, asr_task=asr_task, watcher=watcher,
                         url=url)


async def _stop_services(handle: _ServerHandle) -> None:
    """先停 watcher，再收 runner；ASR 预热是后台任务，取消即止。"""
    if handle.watcher is not None:
        try:
            handle.watcher.stop()
        except Exception:
            pass
    if handle.asr_task is not None:
        handle.asr_task.cancel()
        try:
            await handle.asr_task
        except BaseException:
            pass
    try:
        await handle.runner.cleanup()
    except Exception:
        pass


async def _wait_for_stop(stop: threading.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(0.25)


def _serve_thread(ready: threading.Event, state: dict[str, Any],
                  stop: threading.Event) -> None:
    """后台线程：自己的事件循环里把服务跑起来，直到 ``stop`` 被置位。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    handle: _ServerHandle | None = None
    try:
        async def _run() -> None:
            nonlocal handle
            config = _bootstrap()
            if _port_occupied():
                state["already_running"] = True
                return
            handle = await _serve(config)
            state["handle"] = handle
            ready.set()
            await _wait_for_stop(stop)

        loop.run_until_complete(_run())
    except BaseException as exc:  # noqa: BLE001 — 交给主线程报错/兜底
        state["error"] = exc
    finally:
        ready.set()
        if handle is not None:
            try:
                loop.run_until_complete(_stop_services(handle))
            except Exception:
                pass
        try:
            loop.close()
        except Exception:
            pass


# ── 原生窗口（pywebview / WebView2） ──────────────────────────────────────

def _webview_storage() -> str:
    """WebView2 的 profile 目录：localStorage/IndexedDB 要跨次保留。

    ``private_mode=True``（pywebview 默认）用临时目录并在退出时删除，
    我们的页面状态存在 localStorage（书架排序、阅读位置等），必须持久化。
    """
    from runtime_paths import data_path

    path = data_path("webview")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _app_icon() -> str | None:
    from runtime_paths import resource_path

    icon = resource_path("packaging", "icon", "liubai.ico")
    return str(icon) if icon.is_file() else None


def _window_geometry() -> tuple[int | None, int | None, int, int]:
    """窗口 (x, y, w, h)：默认 1280×900，不超出可用桌面并居中。

    小屏（如 1536×864）上 900 高会被 Windows 直接撑成最大化，这里先按
    可用区的 92% 收一档，再算居中坐标。
    """
    width, height = WINDOW_SIZE
    x: int | None = None
    y: int | None = None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Rect(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG),
                            ("bottom", wintypes.LONG)]

            rect = _Rect()
            # SPI_GETWORKAREA：不含任务栏的可用桌面
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                avail_w = rect.right - rect.left
                avail_h = rect.bottom - rect.top
                width = min(width, int(avail_w * 0.92))
                height = min(height, int(avail_h * 0.92))
                x = rect.left + (avail_w - width) // 2
                y = rect.top + (avail_h - height) // 2
        except Exception:
            pass
    width = max(width, WINDOW_MIN_SIZE[0])
    height = max(height, WINDOW_MIN_SIZE[1])
    return x, y, width, height


def _show_window(url: str, stop: threading.Event,
                 thread: threading.Thread) -> int:
    """主线程跑 pywebview；窗口关掉才返回，随后停服务。"""
    try:
        import webview
    except Exception as exc:
        print(f"  pywebview 不可用（{exc}），退回 Edge/浏览器窗口")
        return _fallback_window(url, stop, thread)

    # WebView2 初始化失败时 pywebview 只写日志、窗口空白，start() 立刻返回；
    # 用日志内容当失败信号，精确退回旧窗口，不误伤「用户秒关窗口」。
    init_failed = threading.Event()

    class _FailureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                if "initialization failed" in record.getMessage():
                    init_failed.set()
            except Exception:
                pass

    logger = logging.getLogger("pywebview")
    handler = _FailureHandler()
    logger.addHandler(handler)

    x, y, width, height = _window_geometry()
    webview.create_window(
        APP_TITLE, url,
        x=x, y=y, width=width, height=height,
        min_size=WINDOW_MIN_SIZE,
        text_select=True,          # 阅读器要能划词
        background_color="#fbf8f2",
    )
    try:
        webview.start(private_mode=False, storage_path=_webview_storage(),
                      icon=_app_icon())
    except Exception as exc:
        print(f"  窗口启动失败：{exc}")
        init_failed.set()
    finally:
        logger.removeHandler(handler)

    if init_failed.is_set():
        return _fallback_window(url, stop, thread)

    stop.set()
    thread.join(timeout=15)
    return 0


def _fallback_window(url: str, stop: threading.Event,
                     thread: threading.Thread) -> int:
    """pywebview 起不来时的退路：Edge 独立窗口 → 系统浏览器。"""
    if not _open_ui(url, app_window=True):
        # 系统浏览器：关标签页拿不到生命周期信号，服务常驻（旧行为）。
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return 0
    try:
        asyncio.run(_await_app_window(APP_TITLE))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(timeout=15)
    return 0


def _run_native() -> int:
    """默认模式：后台线程起服务，主线程开原生窗口。"""
    ready = threading.Event()
    stop = threading.Event()
    state: dict[str, Any] = {}
    thread = threading.Thread(target=_serve_thread, args=(ready, state, stop),
                              name="liubai-server", daemon=True)
    thread.start()

    if not ready.wait(120):
        _fatal(f"{APP_TITLE} 服务启动超时。\n\n"
               "详细日志：<用户数据目录>\\data\\logs")
        return 1
    error = state.get("error")
    if error is not None:
        import traceback

        detail = "".join(
            traceback.format_exception_only(type(error), error)).strip()
        _fatal(f"{APP_TITLE} 启动失败：\n{detail}\n\n"
               "详细日志：<用户数据目录>\\data\\logs")
        return 1
    if state.get("already_running"):
        print(f"{APP_TITLE} 已在运行")
        if not _focus_window(APP_TITLE):
            _open_ui(f"http://{HOST}:{PORT}", app_window=True)
        return 0

    handle: _ServerHandle = state["handle"]
    return _show_window(handle.url, stop, thread)


async def main_async(open_browser: bool = True, app_window: bool = False) -> int:
    """非原生窗口路径：Edge ``--app`` / 系统浏览器 / 只起服务。"""
    _ensure_stdio()
    config = _bootstrap()

    if _port_occupied():
        url = f"http://{HOST}:{PORT}"
        print(f"{APP_TITLE} 已在运行：{url}")
        if open_browser:
            _open_ui(url, app_window)
        return 0

    handle = await _serve(config)
    app_window_opened = False
    try:
        if open_browser:
            app_window_opened = _open_ui(handle.url, app_window)
        if app_window_opened:
            # 关掉应用窗口 = 退出程序（顺带停掉后端，端口释放）。
            await _await_app_window(APP_TITLE)
        else:
            await asyncio.Event().wait()
    finally:
        await _stop_services(handle)
    return 0


def main() -> int:
    _ensure_stdio()
    argv = sys.argv[1:]

    # 冻结后子进程宿主：作者细节等独立脚本
    if argv[:1] == ["--run-script"]:
        if len(argv) < 2:
            _fatal("--run-script 缺少脚本名")
            return 2
        from runtime_paths import run_script
        return run_script(argv[1], argv[2:])

    open_browser = "--no-browser" not in argv
    # 默认是 pywebview 原生窗口；``--app-window`` 保留为旧窗口（Edge --app）。
    edge_window = ("--app-window" in argv
                   or os.environ.get("LIUBAI_APP_WINDOW", "") == "1")
    try:
        if not open_browser:
            return asyncio.run(main_async(open_browser=False))
        if edge_window:
            return asyncio.run(main_async(open_browser=True, app_window=True))
        return _run_native()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _fatal(f"{APP_TITLE} 启动失败：\n{detail}\n\n"
               "详细日志：<用户数据目录>\\data\\logs")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

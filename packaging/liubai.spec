# -*- mode: python ; coding: utf-8 -*-
"""留白（LiuBai）PyInstaller 规格：Web-only 桌面版。

入口是 ``desktop.py``，不是 ``main.py`` —— CLI 不参与打包。

打进去的只读资源：
  reader/ prompts/ skills/ hooks/ roles/ agents/ scripts/
  packaging/icon/ + SOUL.md（首次启动播种到用户根）

settings.json **不打包**：它是用户个人配置（称呼/性别/模型），带上会把打包机
上的个人信息带进每个新安装；缺文件时 Config 走默认值。

用户数据不在 exe 里：运行时落在 %LOCALAPPDATA%\\LiuBai（见 runtime_paths.py），
升级只替换安装目录，数据不动。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent          # packaging/ 的上一级 = 仓库根
ICON = ROOT / "packaging" / "icon" / "liubai.ico"
CONSOLE = os.environ.get("LIUBAI_CONSOLE", "1") == "1"

datas = [
    (str(ROOT / "reader"), "reader"),
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "skills"), "skills"),
    (str(ROOT / "hooks"), "hooks"),
    (str(ROOT / "roles"), "roles"),
    (str(ROOT / "agents"), "agents"),
    (str(ROOT / "scripts"), "scripts"),
    (str(ROOT / "flight_recorder" / "viewer.html"), "flight_recorder"),
    (str(ICON.parent), "packaging/icon"),
    (str(ROOT / "SOUL.md"), "."),
]
if (ROOT / ".env.example").is_file():
    datas.append((str(ROOT / ".env.example"), "."))

# jieba 的词典按数据文件加载，静态分析看不到。
datas += collect_data_files("jieba")

hiddenimports = [
    "aiohttp", "aiohttp.web",
    # authorgraph 只被 scripts/run_author_details.py（数据文件）导入，服务进程
    # 不引用 → 静态分析看不到。作者细节采集的整条依赖闭包显式带上。
    *collect_submodules("authorgraph"),
    "watchdog.observers.winapi",
    "PIL.Image", "PIL.ImageFile", "PIL.JpegImagePlugin",
    "rank_bm25",
    # 原生窗口：pywebview 的后端按名字动态导入，静态分析看不见；
    # Windows 后端是 winforms + WebView2（pythonnet/clr）。
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr", "clr_loader",
]

excludes = [
    "tkinter", "matplotlib", "pytest", "PyQt5", "PySide6",
    # ASR 本地模型链（funasr_onnx → onnxruntime/torch/librosa）不随安装包分发：
    # 缺模型时 /api/asr 自行降级，绝不能把 2GB 的推理栈打进安装包。
    "funasr", "funasr_onnx", "modelscope",
    "onnxruntime", "torch", "librosa", "soundfile", "audioread",
    "numba", "llvmlite", "scipy", "transformers",
]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Liubai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    icon=str(ICON),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Liubai",
)

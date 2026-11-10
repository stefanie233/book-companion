"""运行路径的唯一真相源：程序根（只读）与用户根（可写）分开。

冻结（PyInstaller）后 ``__file__`` 落在 ``_MEIPASS`` 临时目录、``cwd`` 取决于
快捷方式，两者都不能用来存数据。这里把两类路径钉死：

* **程序根** ``code_root()`` —— reader/ prompts/ skills/ hooks/ roles/ agents/
  scripts/ 等只读资源。源码运行时是仓库根，冻结后是 ``_MEIPASS``。
* **用户根** ``user_root()`` —— data/、SOUL.md、settings.json、.env 等可写内容。
  默认 ``%LOCALAPPDATA%\\LiuBai``；``BC_HOME`` 可覆盖；exe 同级有
  ``portable.flag`` 时退回安装目录（便携模式）。源码运行时等于仓库根，
  保持开发期行为不变。

升级只需替换程序根；安装器不碰用户根，用户数据自然零迁移。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: 产品名对应的用户数据目录（%LOCALAPPDATA%\\<APP_DIR_NAME>）。
APP_DIR_NAME = "LiuBai"

#: 首次启动从程序根复制到用户根的默认文件（不存在才复制）。
#: settings.json 刻意不播种：它是**用户个人配置**（称呼、性别、模型），
#: 由第一次保存时生成；随包播种会把打包机上的个人信息带进每个新安装。
_SEED_FILES = ("SOUL.md",)

#: 用户根下必须存在的目录（相对 data/）。
_DATA_DIRS = (
    "",
    "config",
    "logs",
    "graphs",
    "backups",
    "memory",
    "sessions",
    "user",
    "agent",
    "voice",
    "bookbank",
    "watch",
    "writing",
    "versions",
    "models",
)


def is_frozen() -> bool:
    """是否运行在 PyInstaller 等冻结产物里。"""
    return bool(getattr(sys, "frozen", False))


def code_root() -> Path:
    """程序根：只读资源的落脚点。"""
    if is_frozen():
        bundled = getattr(sys, "_MEIPASS", "")
        if bundled:
            return Path(bundled)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def install_root() -> Path:
    """安装根：exe 所在目录（源码运行时等于仓库根）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return code_root()


def resource_path(*parts: str) -> Path:
    """程序根下的资源路径（reader/, prompts/, skills/ ...）。"""
    return code_root().joinpath(*parts)


def _portable_flag() -> bool:
    """便携模式的标记：exe 同级放 ``portable.flag`` 文件或 ``portable`` 目录。"""
    for base in (install_root(), Path.cwd()):
        try:
            if (base / "portable.flag").exists() or (base / "portable").is_dir():
                return True
        except OSError:
            continue
    return False


def user_root() -> Path:
    """用户根：可写数据（data/、SOUL.md、settings.json、.env）。

    优先级：``BC_HOME`` > 便携标记（安装根）> 平台默认目录。
    源码运行且未设 ``BC_HOME`` 时返回仓库根 —— 开发期路径不变。
    """
    override = os.environ.get("BC_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _portable_flag():
        return install_root()
    if not is_frozen():
        return code_root()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def data_dir() -> Path:
    """用户数据目录，始终创建好再返回。"""
    path = user_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_path(*parts: str) -> Path:
    """``data/`` 下的绝对路径（不创建父目录，缓存/日志等按需 mkdir）。"""
    return data_dir().joinpath(*parts)


def ensure_user_layout() -> Path:
    """首次运行把用户根建好，并从程序根播种默认文件。

    * 目录：data/ 及各子目录（幂等）。
    * 文件：只播种 SOUL.md，且**不存在**时才复制，绝不覆盖用户改动。
      settings.json 不播种——那是用户个人配置，随包带会把打包机的
      称呼/性别/模型带给每个新安装；缺文件时 Config 走默认值。
    * 源码运行（用户根 == 程序根）不做复制，避免把仓库文件当成种子。
    """
    root = user_root()
    for rel in _DATA_DIRS:
        (root / "data" / rel).mkdir(parents=True, exist_ok=True)
    if root.resolve() != code_root().resolve():
        for name in _SEED_FILES:
            dst = root / name
            src = code_root() / name
            if dst.exists() or not src.is_file():
                continue
            try:
                shutil.copy2(src, dst)
            except OSError:
                # 播种失败不阻断启动：缺 SOUL 由 save_default_soul 兜底。
                pass
    return root


def script_command(script_name: str, args: list[str] | None = None,
                   root: Path | None = None) -> list[str]:
    """独立进程脚本的启动命令。

    源码：``python <root>/scripts/<name> ...``；冻结后 ``sys.executable``
    是自己，必须走 ``--run-script`` 分流，由入口进程用 runpy 执行同一份脚本。
    """
    argv = list(args or [])
    if is_frozen():
        return [sys.executable, "--run-script", script_name, *argv]
    base = Path(root) if root is not None else resource_path("scripts").parent
    return [sys.executable, str(base / "scripts" / script_name), *argv]


def run_script(script_name: str, argv: list[str] | None = None) -> int:
    """在当前进程里执行 ``scripts/<script_name>``（冻结入口的 ``--run-script``）。

    脚本按 ``__main__`` 运行，``sys.argv`` 与直接命令行调用一致；退出码取
    ``SystemExit.code``。脚本文件必须随打包进程序根（spec 的 datas）。
    """
    import runpy

    path = resource_path("scripts", script_name)
    if not path.is_file():
        print(f"[run-script] 找不到脚本：{path}", file=sys.stderr)
        return 2
    if str(code_root()) not in sys.path:
        sys.path.insert(0, str(code_root()))
    old_argv = sys.argv
    sys.argv = [str(path), *(argv or [])]
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = old_argv
    return 0

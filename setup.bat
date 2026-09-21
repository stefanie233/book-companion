@echo off
rem ==========================================================================
rem 本文件编码为 GBK(cp936)，请勿另存为 UTF-8！
rem 中文 Windows 控制台默认就是 936，文件与代码页一致时 cmd 逐字节读取，
rem 不会错位；一旦在文件里 chcp 切换代码页，cmd 会按旧代码页解出错误的
rem 行边界，把框线行劈成两半当命令执行（2026-09-21 实测）。
rem ==========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ================================================
echo    书伴 Book Companion - 初始化向导
echo ================================================
echo.

:: -- 1. 环境检测 ------------------------------------------------------
echo [1/5] 检查 Python 环境...

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.12+
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set pyver=%%v
echo         Python !pyver! √
echo.

:: -- 2. 虚拟环境 ------------------------------------------------------
echo [2/5] 创建虚拟环境 (.venv)...

if exist ".venv\Scripts\python.exe" (
    echo         虚拟环境已存在，跳过 √
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] venv 创建失败
        pause
        exit /b 1
    )
    echo         虚拟环境创建完成 √
)
echo.

:: -- 3. 安装依赖 ------------------------------------------------------
echo [3/5] 安装 Python 依赖...

call .venv\Scripts\activate.bat
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo         依赖安装完成 √
echo.

:: -- 4. 配置文件 ------------------------------------------------------
echo [4/5] 配置文件...

if exist ".env" (
    echo         .env 已存在，跳过 √
) else (
    copy .env.example .env >nul
    echo         已从 .env.example 创建 .env（Key 可留空，启动后在网页引导中填写）
)
echo.

:: -- 5. ASR 语音识别模型（可选）--------------------------------------
echo [5/5] 语音识别模型 (可选, ~231 MB)

set /p asr_choice="         下载 ASR 模型？(y/n，默认 n): "
if /i "!asr_choice!"=="y" (
    echo         正在安装 modelscope + funasr_onnx...
    pip install modelscope funasr_onnx --quiet
    echo         正在下载模型文件...
    python scripts\download_asr_model.py
    if errorlevel 1 (
        echo [警告] ASR 模型下载失败，语音输入功能将不可用
        echo        可稍后手动运行: python scripts\download_asr_model.py
    ) else (
        echo         ASR 模型下载完成 √
    )
) else (
    echo         已跳过（可稍后运行: python scripts\download_asr_model.py）
)
echo.

:: -- 启动 ---------------------------------------------------------------
echo ================================================
echo.
echo ================================================
echo    初始化完成！
echo.
echo    即将自动启动书伴。浏览器打开后，在引导弹窗
echo    中填入 API Key 即可开始使用。
echo ================================================
echo.
call start.bat

@echo off
rem ==========================================================================
rem 本文件编码为 GBK(cp936)，请勿另存为 UTF-8！
rem 中文 Windows 控制台默认即 936：文件与代码页一致时 cmd 逐字节读取不会错位；
rem 若在文件里 chcp 切换代码页，cmd 会按旧代码页解出行边界，把整行劈成两半
rem 当命令执行（2026-09-21 实测）。
rem 注意：批处理里不要用制表符画框——GBK 尾字节 0x5E 是 cmd 的转义符，会让
rem 解析错位吞行。
rem ==========================================================================
cd /d "%~dp0"

echo ============================================
echo    书伴 Liubai 启动中...
echo ============================================
echo.

:: 0. 端口闸：8766 上已有实例就直接退出
::    Windows 的 SO_REUSEADDR 允许两个进程同时监听同一端口，新实例既不
::    报错也收不到流量，于是「重启了但其实没生效」。这里先拦住。
echo [1/3] 检查端口 8766...
set "OWNERPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:8766 .*LISTENING"') do set "OWNERPID=%%p"
if not defined OWNERPID goto port_free
echo.
echo [错误] 端口 8766 已被进程 %OWNERPID% 占用
echo        先结束它再启动：taskkill /PID %OWNERPID% /F
echo        或关掉那个 Agent 窗口后重跑本脚本。
echo.
pause
exit /b 1

:port_free

:: 1. 后台启动 agent（独立窗口，可交互）
::    优先用 .venv（setup.bat 建的），没建过则回退系统 python
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo [2/3] 启动后端 Agent... (%PY%)
start "书伴 Agent" cmd /c "%PY% main.py"

:: 2. 等待服务就绪（最多 60 秒；超时明确报错，不再无限死等）
echo [3/3] 等待服务就绪...
set /a tries=0
:wait
rem ping 代替 timeout 当 sleep：timeout 在输入被重定向时会直接报错退出
ping -n 2 127.0.0.1 >nul
curl -s http://127.0.0.1:8766/api/health >nul 2>&1
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% lss 60 goto wait

echo.
echo [错误] 60 秒内服务未就绪。
echo        常见原因：依赖未安装（先运行 setup.bat），或端口 8766 被占用。
echo        手动排查：在本目录运行  python main.py  查看具体报错。
echo.
pause
exit /b 1

:ready
:: 3. 打开前端
echo 启动前端...
start "" "http://127.0.0.1:8766/"

echo.
echo 书伴已就绪！浏览器已打开前端页面。
echo 关闭此窗口不会影响正在运行的书伴。

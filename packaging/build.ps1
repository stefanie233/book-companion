# 留白（LiuBai）打包脚本 — Windows
#
# 用法（仓库根执行）：
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1              # 控制台版，便于看报错
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Windowed    # 无控制台（体验版）
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Installer   # 顺带出 Inno 安装包
#
# 前置：Python 3.12 与 pip install pyinstaller；装安装包时另需 Inno Setup 6（ISCC.exe）。

param(
    [switch]$Windowed,
    [switch]$Installer,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "== 留白 打包 ==" -ForegroundColor Cyan
& $Python -c "import sys; print('Python', sys.version.split()[0]); assert sys.version_info[:2] == (3, 12), '需要 Python 3.12'"
if ($LASTEXITCODE -ne 0) { throw "Python 版本不符" }

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "未安装 PyInstaller：$Python -m pip install pyinstaller"
}

if ($Windowed) { $env:LIUBAI_CONSOLE = "0" } else { $env:LIUBAI_CONSOLE = "1" }

Write-Host "-- PyInstaller --" -ForegroundColor Cyan
& $Python -m PyInstaller --noconfirm --clean "packaging\liubai.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $Root "dist\Liubai\Liubai.exe"
if (-not (Test-Path $exe)) { throw "没有产出 $exe" }
Write-Host "产物：$exe" -ForegroundColor Green

if ($Installer) {
    $iscc = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    }
    if (-not $iscc) {
        throw "未找到 Inno Setup 6 的 ISCC.exe（装好后重跑，或手动编译 packaging\installer.iss）"
    }
    Write-Host "-- Inno Setup --" -ForegroundColor Cyan
    & $iscc "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失败" }
    Write-Host "安装包：dist\installer\LiuBai-Setup-*.exe" -ForegroundColor Green
}

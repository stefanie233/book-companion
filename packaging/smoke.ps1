# 冻结产物的冒烟测试：干净用户根里启动 exe，确认服务与静态页能起来。
#
# 用法（仓库根）：
#   powershell -ExecutionPolicy Bypass -File packaging\smoke.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\smoke.ps1 -Exe dist\Liubai\Liubai.exe -Port 8799
#
# 覆盖的正是「源码能跑、编译后才炸」的几类：数据根写不进去、静态资源漏打、
# 启动期导入失败。任何一步失败都会打印退出码与用户根位置，便于排查。

param(
    [string]$Exe = "dist\Liubai\Liubai.exe",
    [int]$Port = 8799,
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ExePath = if ([System.IO.Path]::IsPathRooted($Exe)) { $Exe } else { Join-Path $Root $Exe }
if (-not (Test-Path $ExePath)) { throw "找不到 $ExePath，先跑 build.ps1" }

$Home_ = Join-Path $env:TEMP ("liubai-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $Home_ | Out-Null

$oldHome = $env:BC_HOME
$oldPort = $env:BC_PORT
$env:BC_HOME = $Home_
$env:BC_PORT = "$Port"

Write-Host "== 留白 冒烟 ==" -ForegroundColor Cyan
Write-Host "exe: $ExePath"
Write-Host "用户根: $Home_"

$proc = Start-Process -FilePath $ExePath -ArgumentList "--no-browser" -PassThru -WindowStyle Hidden
try {
    $base = "http://127.0.0.1:$Port"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if ($proc.HasExited) { throw "进程提前退出（exit=$($proc.ExitCode)）" }
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "$base/api/health" -TimeoutSec 3
            if ($health.StatusCode -eq 200) { break }
        } catch { }
    }
    if (-not $health -or $health.StatusCode -ne 200) { throw "健康检查超时" }
    Write-Host "[OK] /api/health 200" -ForegroundColor Green

    $page = Invoke-WebRequest -UseBasicParsing -Uri "$base/" -TimeoutSec 10
    if ($page.StatusCode -ne 200) { throw "静态页 $($page.StatusCode)" }
    if ($page.Content -notmatch "bookPage") { throw "静态页内容异常（缺 bookPage）" }
    Write-Host "[OK] 阅读器页面 200" -ForegroundColor Green

    foreach ($rel in @("data\books.db", "data\sessions.db", "SOUL.md")) {
        $p = Join-Path $Home_ $rel
        if (-not (Test-Path $p)) { throw "用户根缺文件：$rel" }
    }
    # settings.json 是用户个人配置，绝不能随包播种（会把打包机的称呼/性别带出去）。
    if (Test-Path (Join-Path $Home_ "settings.json")) {
        throw "settings.json 被播种了（应只在用户保存设置时生成）"
    }
    Write-Host "[OK] 用户根目录与播种文件" -ForegroundColor Green

    $favicon = Invoke-WebRequest -UseBasicParsing -Uri "$base/favicon.png" -TimeoutSec 5
    if ($favicon.StatusCode -ne 200) { throw "favicon $($favicon.StatusCode)" }
    Write-Host "[OK] favicon 静态资源" -ForegroundColor Green
}
finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    $env:BC_HOME = $oldHome
    $env:BC_PORT = $oldPort
}
Write-Host "冒烟通过（用户根保留在 $Home_，可手动删除）" -ForegroundColor Green

# 留白 · 打包说明

Web-only 桌面版：入口 `desktop.py`，CLI（`main.py`）不参与打包。

## 前置

| 依赖 | 说明 |
|---|---|
| Python 3.12 | 必须与开发环境一致 |
| `pip install pyinstaller` | 打包前先安装（开发环境验证过 6.22.3） |
| `pip install pywebview pythonnet` | 原生窗口依赖（开发环境验证过 pywebview 6.2.1 / pythonnet 3.2.1） |
| Inno Setup 6 | 只在出安装包时需要；默认从常见安装路径或 `ISCC.exe` 的 PATH 查找 |

## 构建

```powershell
# 控制台版（第一次务必用它，错误直接看得见）
powershell -ExecutionPolicy Bypass -File packaging\build.ps1

# 体验版（无控制台窗口）
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Windowed

# 顺带出安装包
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Windowed -Installer
```

产物：

- `dist\Liubai\Liubai.exe`（onedir，整个目录即绿色版）
- `dist\installer\LiuBai-Setup-0.1.0.exe`

## 窗口

默认是 **pywebview 原生窗口**（Windows 下 WebView2）：标准系统标题栏（标题
「留白」）、任务栏独立图标，窗口按桌面可用区居中（默认 1280×900，小屏自动
收一档）。**关掉窗口即退出程序**（服务同时停止、端口释放）。

WebView2 profile 在 `<用户根>\data\webview`（`private_mode=False`）——书架的
排序、阅读位置等 localStorage 状态要跨次保留，不能是退出即删的临时目录。

兜底链（自动）：pywebview 缺失 / WebView2 初始化失败 → Edge `--app` 独立
窗口（profile 在 `<用户根>\data\edge-app`）→ 系统默认浏览器（此时关标签页
不会停服务，需结束进程）。

手动指定：

```text
Liubai.exe                     # 原生窗口（默认）
Liubai.exe --app-window        # 退回 Edge --app 窗口
Liubai.exe --no-browser        # 只起服务（调试/冒烟）
```

WebView2 Runtime 在 Win10/11 上随 Edge 预装；Win7/8 或精简系统需另装
「Microsoft Edge WebView2 Runtime」，否则会走 Edge 兜底。

## 数据与升级

- 程序根（安装目录）只读，升级整目录替换。
- 用户根：`%LOCALAPPDATA%\LiuBai`（`data/`、`SOUL.md`、`settings.json`、`.env`）。
  - 覆盖升级不动用户根；Inno 卸载默认保留。
  - 便携模式：exe 同级放 `portable.flag`，数据落在 exe 旁边。
  - 换机迁移：整个用户根拷走即可。
- 首次启动只从安装包播种 `SOUL.md`（不存在时复制）。
- **`settings.json` 不打包也不播种**：称呼、性别、模型这些是用户个人配置，
  由第一次保存时生成；随包带会把打包机上的个人信息带进每个新安装。
- 每次版本升级前建议把 `data/*.db` 备份到 `data/backups/upgrade-<版本>/`。

## 已知注意点

- **用 `pwsh`（PowerShell 7）执行脚本**。Windows PowerShell 5.1 按 GBK 读
  UTF-8 脚本，中文会变乱码并把字符串引号吃掉（实测报 ParserError）。
- **`installer.iss` 必须带 UTF-8 BOM**。Inno 看到 BOM 才按 UTF-8 解析，
  否则按系统 ANSI（GBK）读，中文应用名与提示全变乱码；改完用编辑器保存为
  「UTF-8 with BOM」。

- **不要打包字体**。界面与正文只用系统字体名，发行包内不得出现 `.ttf/.otf/.ttc`。
- 作者细节采集走独立进程：冻结后由同一 exe 的 `--run-script` 分流执行
  `scripts/run_author_details.py`，脚本内部的数据根走 `runtime_paths.user_root()`。
- ASR（SenseVoice / funasr）与语义索引模型不随安装包分发；缺失时对应功能
  自行降级，不影响启动。
- 安装包当前是英文向导：Inno 6 官方不带简体中文 `ChineseSimplified.isl`，
  需要中文向导时把该文件放进 Inno 的 `Languages\` 再在 `installer.iss` 的
  `[Languages]` 段启用。
- 未签名 exe 首次运行会被 SmartScreen 拦一次，属正常；正式发布再考虑签名。

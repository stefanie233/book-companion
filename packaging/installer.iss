; 留白（LiuBai）安装包 — Inno Setup 6
;
; 约定：
;  * 程序装到 {localappdata}\Programs\LiuBai（免管理员）。
;  * 用户数据在 {localappdata}\LiuBai，卸载默认**不删**；升级只替换程序目录。
;  * 便携包不走本脚本：exe 同级放 portable.flag 即可（见 runtime_paths.py）。

#define AppName "留白"
#define AppVersion "0.1.0"
#define AppExe "Liubai.exe"
#define AppPublisher "LiuBai"

[Setup]
AppId={{7E2C4F51-9B3A-4D6E-8C21-5A0F4B7D9E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\LiuBai
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=LiuBai-Setup-{#AppVersion}
SetupIconFile=icon\liubai.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "..\dist\Liubai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清掉程序目录里可能残留的文件；用户数据不在这个目录。
Type: filesandordirs; Name: "{app}"

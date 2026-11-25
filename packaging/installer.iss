; QuantDesk Windows 安装器（Inno Setup 6）
; 产出：QuantDesk-Setup-<version>.exe
; 数据策略：程序装到 Program Files；运行时数据由 qdpaths.py 落到 %LOCALAPPDATA%\QuantDesk，
; 卸载时默认保留用户数据（data/ 里有真实账本与密钥，绝不能静默删除）。

#define MyAppName "QuantDesk"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "yu3rich"
#define MyAppExeName "QuantDesk.exe"

[Setup]
AppId={{8E4A2C1D-9F3B-4B7A-A6C5-QUANTDESK001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputBaseFilename=QuantDesk-Setup-{#MyAppVersion}
OutputDir=dist
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=quantdesk.ico

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\QuantDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理安装目录；%LOCALAPPDATA%\QuantDesk\data 一律保留

; 灵境造片厂 — Windows 10 x64 轻量客户端安装包
; 该脚本只能读取 build_release.ps1 生成并审核过的 staging 目录。
; ComfyUI、Torch、CUDA、Cloudflared 和模型通过独立运行环境包安装。

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by scripts/build_release.ps1
#endif

#ifndef StageDir
  #error StageDir must be supplied by scripts/build_release.ps1
#endif

#ifndef ReleaseOutputDir
  #error ReleaseOutputDir must be supplied by scripts/build_release.ps1
#endif

#define MyAppName "灵境造片厂"
#define MyAppPublisher "Yaro-lu"
#define MyAppURL "https://github.com/Yaro-lu/LingJingAPI"
#define MyAppExe "runtime\python\pythonw.exe"

[Setup]
AppId={{8587F9B2-C36E-49A6-942C-DC321E26510E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
AppComments=轻量客户端；首次打开后可安装或导入独立 AI 运行环境包
DefaultDirName={localappdata}\Programs\LingJingAI
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AllowNoIcons=yes
DisableProgramGroupPage=yes
OutputBaseFilename=LingJingAI-Setup-{#MyAppVersion}-win-x64
OutputDir={#ReleaseOutputDir}
SetupIconFile={#StageDir}\app\gui\assets\app.ico
UninstallDisplayIcon={app}\app\gui\assets\app.ico
Compression=lzma2/ultra64
SolidCompression=yes
InternalCompressLevel=ultra64
WizardStyle=modern
ShowLanguageDialog=no
RestartApplications=no
CloseApplications=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
CreateUninstallRegKey=yes
Uninstallable=yes
VersionInfoDescription=灵境造片厂轻量客户端
VersionInfoProductName=灵境造片厂
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; StageDir 已由 build_release.ps1 做过白名单复制、敏感信息扫描和成员校验。
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; 这些目录可在首次安装后由环境修复、模型下载或工作流导入继续写入。
; 只删除 {app} 下明确的客户端自有目录，根目录 outputs 不在卸载清单中。
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\workflows"

; 清理意外中断的环境安装和 ComfyUI 更新事务残留。
Type: filesandordirs; Name: "{app}\.runtime-install-staging-*"
Type: filesandordirs; Name: "{app}\.runtime-install-backup-*"
Type: filesandordirs; Name: "{app}\.comfyui-update-staging-*"
Type: filesandordirs; Name: "{app}\.comfyui-update-overlay-*"
Type: filesandordirs; Name: "{app}\.comfyui-update-backup-*"
Type: files; Name: "{app}\.comfyui-update-requirements-*.txt"
Type: files; Name: "{app}\.comfyui-update-release-*.zip"
Type: files; Name: "{app}\.comfyui-update-release-*.zip.part"
Type: files; Name: "{app}\.comfyui-update-manifest-*.json"
Type: files; Name: "{app}\.comfyui-update-manifest-*.json.tmp"
Type: files; Name: "{app}\.comfyui-update-journal-*.json"

; 本地放在客户端根目录的官方环境包及其校验文件也属于可重新下载内容。
Type: files; Name: "{app}\runtime-nvidia-*.7z"
Type: files; Name: "{app}\runtime-nvidia-*.7z.sha256"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Icons]
Name: "{app}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Comment: "启动 {#MyAppName}"
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Tasks: desktopicon
Name: "{autodesktop}\灵境造片厂示例页"; Filename: "{app}\灵境造片厂示例页.html"; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Comment: "打开灵境造片厂本地 API 示例页"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; Description: "启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent runascurrentuser

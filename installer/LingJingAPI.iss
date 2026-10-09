; LingJingAPI — Windows 10 x64 轻量客户端安装包
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

#define MyAppName "LingJingAPI"
#define MyAppPublisher "Yaro-lu"
#define MyAppURL "https://github.com/Yaro-lu/LingJingAPI"
#define MyAppExe "runtime\python\pythonw.exe"

[Setup]
; Keep the installed product identity so renaming upgrades the existing app.
AppId={{8587F9B2-C36E-49A6-942C-DC321E26510E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
AppComments=一键调用算力，简单好用。首次打开后可安装或导入独立 AI 运行环境包
DefaultDirName={localappdata}\Programs\LingJingAPI
DisableDirPage=no
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AllowNoIcons=yes
DisableProgramGroupPage=yes
OutputBaseFilename=LingJingAPI-Setup-{#MyAppVersion}-win-x64
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
UsePreviousGroup=no
CreateUninstallRegKey=yes
Uninstallable=yes
; 2.0.2 的卸载记录包含递归删除 models/runtime。升级时必须舍弃旧记录，
; 否则默认 append 会在卸载 2.0.3 时执行旧版的删除规则。
UninstallLogMode=overwrite
VersionInfoDescription=LingJingAPI 轻量客户端
VersionInfoProductName=LingJingAPI
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; StageDir 已由 build_release.ps1 做过白名单复制、敏感信息扫描和成员校验。
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 升级时移除旧品牌的快捷方式，保留用户数据和旧文件名的兼容入口。
Type: files; Name: "{app}\灵境造片厂.lnk"
Type: files; Name: "{group}\灵境造片厂.lnk"
Type: files; Name: "{autodesktop}\灵境造片厂.lnk"
Type: files; Name: "{autodesktop}\灵境造片厂示例页.lnk"
Type: files; Name: "{app}\灵境.lnk"
Type: files; Name: "{autodesktop}\灵境.lnk"
Type: files; Name: "{app}\灵境 · LingJingAPI.lnk"
Type: files; Name: "{autodesktop}\灵境 · LingJingAPI.lnk"
Type: files; Name: "{autodesktop}\灵境 · LingJingAPI 示例页.lnk"
Type: files; Name: "{userprograms}\灵境造片厂\灵境造片厂.lnk"
Type: files; Name: "{userprograms}\灵境\灵境.lnk"
Type: files; Name: "{userprograms}\灵境 · LingJingAPI\灵境 · LingJingAPI.lnk"

[UninstallDelete]
; 只清理程序生成的代码目录。运行环境中也可能有用户放入的模型，
; 因此不递归删除 runtime、models、workflows、outputs 或更新备份。
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\bin"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Icons]
Name: "{app}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Comment: "启动 {#MyAppName}"
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Tasks: desktopicon
Name: "{autodesktop}\LingJingAPI 示例页"; Filename: "{app}\LingJingAPI示例页.html"; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Comment: "打开 LingJingAPI 本地 API 示例页"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; Description: "启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent runascurrentuser

[Code]
var
  PreservedModelsPath: String;
  PreservedOutputsPath: String;

function ConfiguredDataPath(const DirectoryName: String): String;
var
  Lines: TArrayOfString;
  I, Separator: Integer;
  Line, SectionName, KeyName, Value: String;
begin
  Result := ExpandConstant('{app}\' + DirectoryName);
  if not LoadStringsFromFile(ExpandConstant('{app}\runtime\config.local.txt'), Lines) then
    Exit;

  SectionName := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    if Line = '' then
      Continue;
    if (Line[1] = '#') or (Line[1] = ';') then
      Continue;
    if (Line[1] = '[') and (Line[Length(Line)] = ']') then
    begin
      SectionName := Lowercase(Trim(Copy(Line, 2, Length(Line) - 2)));
      Continue;
    end;
    if SectionName <> 'directories' then
      Continue;
    Separator := Pos('=', Line);
    if Separator <= 1 then
      Continue;
    KeyName := Lowercase(Trim(Copy(Line, 1, Separator - 1)));
    if KeyName <> DirectoryName then
      Continue;
    Value := Trim(Copy(Line, Separator + 1, Length(Line)));
    if Value <> '' then
      Result := Value;
    Exit;
  end;
end;

function InitializeUninstall: Boolean;
begin
  PreservedModelsPath := ConfiguredDataPath('models');
  PreservedOutputsPath := ConfiguredDataPath('outputs');
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    SuppressibleMsgBox(
      '程序卸载已结束。模型和生成结果没有自动删除。' + #13#10#13#10 +
      '模型位置：' + PreservedModelsPath + #13#10 +
      '生成结果位置：' + PreservedOutputsPath + #13#10#13#10 +
      '运行环境、配置和导入的工作流也可能保留在安装目录。' + #13#10 +
      '确认不再需要后，请自行检查并删除这些文件。',
      mbInformation, MB_OK, IDOK);
end;

#define MyAppName "Stream Doctor"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "Stream Doctor"
#define MyAppExeName "StreamDoctor.exe"

[Setup]
AppId={{B9E0AFA0-E0F0-4FE8-9E9D-STREAMDOCTOR01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Stream Doctor
DefaultGroupName={#MyAppName}
OutputDir=installer-output
OutputBaseFilename=StreamDoctor-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableFinishedPage=yes
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\StreamDoctor.exe

[Files]
Source: "dist\StreamDoctor\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Stream Doctor"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Stream Doctor"; Flags: nowait skipifsilent runasoriginaluser

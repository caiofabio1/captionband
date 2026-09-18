; Inno Setup script for CaptionBand
; Compile with: iscc installer.iss
; Output: Output\CaptionBandSetup.exe

#define AppName "CaptionBand"
#define AppVersion "0.7.0"
#define AppPublisher "CaptionBand"
#define AppExeName "CaptionBand.exe"

[Setup]
; DO NOT CHANGE AppId. Inno keys the uninstall entry, the Start Menu group
; and "is this an upgrade?" on it. It is deliberately the same string the
; app used under its previous name, so an existing install is UPDATED in
; place instead of leaving a second, stale entry in Programs and Features.
AppId={{8C4F1A2D-7B3E-4F88-9A52-CABSINTLT0001}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\CaptionBand
DefaultGroupName=CaptionBand
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=CaptionBandSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "brazilian"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"

[Files]
; One-dir PyInstaller output: the exe plus its _internal\ tree.
Source: "dist\CaptionBand\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\CaptionBand"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Configurações"; Filename: "{app}\{#AppExeName}"; Parameters: "--settings"
Name: "{group}\Desinstalar"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CaptionBand"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Iniciar agora"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Stale files from a previous one-file install or an older _internal tree.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

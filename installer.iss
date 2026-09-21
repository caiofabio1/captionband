; Inno Setup script for CaptionBand
; Compile with: iscc installer.iss
; Output: Output\CaptionBandSetup.exe

#define AppName "CaptionBand"
#define AppVersion "0.8.0"
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

; A instalacao antiga, de quando o app se chamava TeamsLiveTranslation.
;
; O AppId nao mudou no rename (de proposito: e ele que da continuidade a
; entrada de desinstalacao), mas DefaultDirName e DefaultGroupName mudaram.
; O Inno chaveia a entrada de desinstalacao pelo AppId, entao a instalacao
; nova SOBRESCREVEU a entrada da antiga apontando para a pasta nova -- e a
; pasta antiga ficou orfa: 159 MB no disco, com um unins000.exe que nao
; aparece mais em "Aplicativos instalados", e com um atalho de menu Iniciar
; TAMBEM chamado "CaptionBand", num grupo chamado "Teams Live Translation".
;
; O operador abriu esse atalho por engano em 2026-09-18 e usou por um tempo
; uma versao de dois dias antes, digitando credenciais nela. Duas entradas de
; menu com o MESMO nome e nenhuma forma de distinguir qual e a boa.
;
; Nao da para simplesmente rodar o unins000.exe antigo: mesmo AppId significa
; que ele apagaria a entrada de desinstalacao da versao ATUAL. Logo, remocao
; direta da pasta e do grupo.
Type: filesandordirs; Name: "{autopf}\TeamsLiveTranslation"
Type: filesandordirs; Name: "{userprograms}\Teams Live Translation"
Type: filesandordirs; Name: "{commonprograms}\Teams Live Translation"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

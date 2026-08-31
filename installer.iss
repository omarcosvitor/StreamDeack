#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

#define MyAppName "StreamDeck de bolso"
#define MyAppExeName "StreamDeck.exe"

[Setup]
AppId={{B32318A9-D62C-4720-BE52-A5A0EF2ED08A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Marcos Costa
DefaultDirName={localappdata}\Programs\StreamDeck
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release
OutputBaseFilename=StreamDeck-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=build\StreamDeck.ico
UninstallDisplayName={#MyAppName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na area de trabalho"; GroupDescription: "Atalhos adicionais:"; Flags: unchecked

[Files]
Source: "dist\StreamDeck\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir o {#MyAppName}"; Flags: nowait postinstall skipifsilent
; O proprio deck chama este instalador em silencio com /REOPEN=1 quando o
; usuario manda atualizar: ele fechou pra ser substituido e volta sozinho aqui.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: Reabrir

[Code]
function Reabrir: Boolean;
begin
  Result := ExpandConstant('{param:REOPEN|0}') = '1';
end;

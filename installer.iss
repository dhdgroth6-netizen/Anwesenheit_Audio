; Installer für "Anwesenheit Audio" (Inno Setup 6)
#define AppName "Anwesenheit Audio"
#define AppExe "AnwesenheitAudio.exe"
#define AppVersion "1.0"

[Setup]
AppId={{6B1D5C3E-8F2A-4A7B-9C41-AA0D2E7F3B10}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=AnwesenheitAudio_Setup
SetupIconFile=icon.ico
LicenseFile=LICENSE
AppPublisher=D:Groth
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "autostart"; Description: "Beim Windows-Start automatisch im Hintergrund starten"; GroupDescription: "Optionen:"
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Optionen:"; Flags: unchecked

[Files]
Source: "dist\AnwesenheitAudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AnwesenheitAudio"; ValueData: """{app}\{#AppExe}"" --hidden"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{#AppName} jetzt starten"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\AnwesenheitAudio"

[Code]
// Laufendes Programm vor dem Installieren/Aktualisieren beenden
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
  Result := '';
end;

// Bei Neuinstallation ohne Autostart: alten Autostart-Eintrag entfernen
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardIsTaskSelected('autostart') then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AnwesenheitAudio');
end;

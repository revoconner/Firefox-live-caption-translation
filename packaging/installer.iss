; Inno Setup script for the backend installer. Run through packaging\build.ps1, which builds packaging\dist first.
; Per user install with no elevation: files under %LocalAppData%\LiveCaptionTranslate\backend, a logon task in Task Scheduler for this user, a Start menu shortcut. Uninstall reverses all of it and removes the whole %LocalAppData%\LiveCaptionTranslate folder including logs.

#define AppName "Live Caption Translate"
#define AppVersion "0.1.0"
#define AppExe "LiveCaptionTranslate.exe"
#define Dist "dist\LiveCaptionTranslate"

[Setup]
AppId={{B7F2C1E4-6D3A-4F0B-9C21-5E8A2D4F7C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Rev Oconner
AppPublisherURL=https://github.com/revoconner/ff-live-caption-translate
DefaultDirName={localappdata}\LiveCaptionTranslate\backend
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.20348
LicenseFile=..\LICENSE
SetupIconFile=..\backend\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=out
OutputBaseFilename=LiveCaptionTranslate-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
; A single Setup.exe cannot exceed 4.2 GB (Windows will not run larger executables), so the 5 GB payload is split into Setup.exe plus Setup-N.bin slices that must stay together. 2 GB slices also fit GitHub release assets.
DiskSpanning=yes
DiskSliceSize=2000000000
WizardStyle=modern
CloseApplications=no

[Files]
; Quantized weights do not compress, so the GGUFs are stored as is; everything else goes through LZMA.
Source: "{#Dist}\*"; DestDir: "{app}"; Excludes: "*.gguf"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Dist}\_internal\models\*.gguf"; DestDir: "{app}\_internal\models"; Flags: ignoreversion nocompression
Source: "logon_task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\logon_task.ps1"" -Exe ""{app}\{#AppExe}"""; StatusMsg: "Registering the logon task..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\logon_task.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveLogonTask"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\LiveCaptionTranslate"

[Code]
const
    MutexName = 'LiveCaptionTranslate';

// Ask a running instance to exit and wait for it, so an upgrade or uninstall never fights over locked files. The backend holds a named mutex while it runs and --quit asks it to stop over its WebSocket.
procedure QuitRunning();
var
    Exe: String;
    R, I: Integer;
begin
    if not CheckForMutexes(MutexName) then
        exit;
    Exe := ExpandConstant('{app}\{#AppExe}');
    if FileExists(Exe) then
        Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, R);
    for I := 1 to 40 do
    begin
        if not CheckForMutexes(MutexName) then
            exit;
        Sleep(250);
    end;
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, R);
    Sleep(1000);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
    QuitRunning();
    Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
    if CurUninstallStep = usUninstall then
        QuitRunning();
    if CurUninstallStep = usPostUninstall then
        DelTree(ExpandConstant('{localappdata}\LiveCaptionTranslate'), True, True, True);
end;

; OpenTranscribe Windows Installer Script
; Inno Setup Script for deploying OpenTranscribe with Docker Desktop on Windows
;
; IMPORTANT: Before compiling, update the BuildDir path below to match where you
;            extracted the build folder on your Windows system
;
; AUTOMATIC PREREQUISITE CHECKING:
;   The installer automatically runs check-prerequisites.ps1 before installation.
;   If prerequisites fail, installation stops with detailed error message.
;   This ensures Docker, WSL 2, RAM, disk space, etc. are ready before copying 50GB+ files.

#define MyAppName "OpenTranscribe"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "OpenTranscribe Project"
#define MyAppURL "https://github.com/davidamacey/opentranscribe"
#define MyAppExeName "run_opentranscribe.bat"
#define MyAppExeUninstallName "uninstall_opentranscribe.bat"
; CHANGE THIS PATH to where you extracted the build folder on Windows
#define BuildDir "C:\opentranscribe-windows-build"
#define MyAppIconName "ot-icon.ico"

; Note: All files are copied to BuildDir by build-windows-installer.sh on Linux
; Transfer the entire build folder to Windows, then update BuildDir path above

[Setup]
; App identification - this GUID uniquely identifies the application in Windows Registry
; This enables proper Add/Remove Programs functionality
AppId={{8F7A3D1E-9B2C-4E5F-A1D8-6C9E4B2F7A3D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; Installation directories
DefaultDirName={autopf}\{#MyAppName}
DisableDirPage=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; User interaction
LicenseFile={#BuildDir}\license.txt
InfoBeforeFile={#BuildDir}\preinstall.txt
InfoAfterFile={#BuildDir}\after-install.txt

; Privileges (admin required for Docker operations)
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Output configuration
OutputDir={#BuildDir}\output
OutputBaseFilename=OpenTranscribe-Setup-{#MyAppVersion}
SetupIconFile={#BuildDir}\{#MyAppIconName}
UninstallDisplayIcon={app}\{#MyAppIconName}

; Compression settings
; Use fast compression for installer itself, nocompression for large binary files
Compression=lzma2/fast
SolidCompression=yes

; Visual settings
WizardStyle=modern

; Disk spanning for large installers (50-80GB total)
DiskSpanning=yes
DiskSliceSize=max

; Minimum Windows version (1809 - October 2018 Update with WSL 2 support)
MinVersion=10.0.17763

; Uninstall configuration
UninstallDisplayName={#MyAppName}
UninstallFilesDir={app}\uninstall

; Add/Remove Programs customization
AppContact={#MyAppURL}/issues
AppComments=AI-powered transcription application with speaker diarization
AppReadmeFile={app}\README-WINDOWS.md

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Main application scripts
Source: "{#BuildDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\{#MyAppExeUninstallName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\{#MyAppIconName}"; DestDir: "{app}"; Flags: ignoreversion

; Configuration files
Source: "{#BuildDir}\.env"; DestDir: "{app}"; Flags: ignoreversion confirmoverwrite
Source: "{#BuildDir}\config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs

; Database initialization
Source: "{#BuildDir}\database\*"; DestDir: "{app}\database"; Flags: ignoreversion recursesubdirs createallsubdirs

; Documentation
Source: "{#BuildDir}\README-WINDOWS.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "{#BuildDir}\package-info.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\checksums.sha256"; DestDir: "{app}"; Flags: ignoreversion

; PowerShell scripts (if they exist)
Source: "{#BuildDir}\check-prerequisites.ps1"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; First-run secret generation - secrets are NOT baked into the package
Source: "{#BuildDir}\generate-secrets.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Docker images (largest files - 10-30GB total)
; Use nocompression to dramatically speed up installer build (tar files are already compressed)
Source: "{#BuildDir}\docker-images\*"; DestDir: "{app}\docker-images"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression

; AI Models (5-40GB depending on Whisper model size)
; Use nocompression to speed up build - these are already optimized binary formats
Source: "{#BuildDir}\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression

[Icons]
; Start Menu shortcuts
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppIconName}"; Comment: "Launch OpenTranscribe"
Name: "{group}\{#MyAppName} README"; Filename: "{app}\README-WINDOWS.md"; Comment: "View Documentation"
Name: "{group}\Open {#MyAppName} Frontend"; Filename: "http://localhost:5173"; IconFilename: "{app}\{#MyAppIconName}"; Comment: "Open in browser"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"; IconFilename: "{app}\{#MyAppIconName}"

; Desktop shortcut (optional, user can decline)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppIconName}"; Comment: "Launch OpenTranscribe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
; Option to launch after installation
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Run uninstall script to clean up Docker containers and volumes
Filename: "{app}\{#MyAppExeUninstallName}"; Parameters: "/SILENT"; Flags: runhidden waituntilterminated

[UninstallDelete]
; Clean up any generated files and directories
Type: filesandordirs; Name: "{app}\docker-images"
Type: filesandordirs; Name: "{app}\models"
Type: files; Name: "{app}\*.log"
Type: files; Name: "{app}\*.tmp"

[Code]
// Prerequisite checking and installation control

function InitializeSetup(): Boolean;
begin
  Result := True;
  // Initial setup - can add splash screen or early checks here if needed
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  PrereqScript: String;
begin
  Result := '';
  NeedsRestart := False;

  // Path to prerequisite checker script
  PrereqScript := ExpandConstant('{#BuildDir}\check-prerequisites.ps1');

  // Display status message
  Log('Running system prerequisites check...');

  // Run PowerShell prerequisite checker in silent mode
  // -Silent flag suppresses interactive output and returns exit code
  // Exit code 0 = all checks passed, 1 = checks failed
  if not Exec('powershell.exe',
              '-ExecutionPolicy Bypass -NoProfile -File "' + PrereqScript + '" -Silent',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    // Failed to execute the script
    Result := 'ERROR: Failed to run system prerequisites checker.' + #13#10 + #13#10 +
              'PowerShell may not be available or the script file is missing.' + #13#10 + #13#10 +
              'Please verify:' + #13#10 +
              '  1. PowerShell is installed (Windows 10/11 default)' + #13#10 +
              '  2. check-prerequisites.ps1 exists in build directory' + #13#10 + #13#10 +
              'Installation cannot continue.';
    Exit;
  end;

  // Check exit code from prerequisite script
  if ResultCode <> 0 then
  begin
    // Prerequisites check failed
    Result := 'SYSTEM PREREQUISITES CHECK FAILED!' + #13#10 + #13#10 +
              'Your system does not meet the requirements for OpenTranscribe.' + #13#10 + #13#10 +
              'Common issues:' + #13#10 +
              '  • Docker Desktop is not running' + #13#10 +
              '  • WSL 2 is not properly configured' + #13#10 +
              '  • Insufficient RAM (need 16GB+)' + #13#10 +
              '  • Insufficient disk space (need 100GB+)' + #13#10 +
              '  • Windows version too old (need 10/11 build 17763+)' + #13#10 + #13#10 +
              'TO DIAGNOSE:' + #13#10 +
              '  1. Open PowerShell as Administrator' + #13#10 +
              '  2. Navigate to: ' + ExpandConstant('{#BuildDir}') + #13#10 +
              '  3. Run: .\check-prerequisites.ps1' + #13#10 +
              '  4. Review detailed error messages' + #13#10 +
              '  5. Fix all issues and try installation again' + #13#10 + #13#10 +
              'Installation cannot continue until prerequisites are met.';
    Exit;
  end;

  // All checks passed
  Log('System prerequisites check passed successfully.');
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Post-installation tasks can be added here
    // For example: final configuration, service startup verification, etc.
    Log('Installation completed successfully.');
  end;
end;

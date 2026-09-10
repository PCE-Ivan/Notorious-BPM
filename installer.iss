; Notorious B.P.M. Windows installer (was Jukebox -- renamed; see the new
; AppId note below).
; Build with Inno Setup (https://jrsoftware.org/isinfo.php), AFTER running
; PyInstaller first (this script packages dist\NotoriousBPM\, which
; PyInstaller produces). See BUILD_WINDOWS.md for the full step-by-step.

#define MyAppName "Notorious B.P.M."
#define MyAppVersion "1.2"
#define MyAppExeName "NotoriousBPM.exe"

[Setup]
; Keep this GUID the same across future versions -- it's how Windows and
; Inno Setup recognize "this is an upgrade of the same app" rather than a
; separate install. Regenerated for this rename (was
; B9A4E7E2-6C3E-4A9B-9F0E-6D2C6E8F1A11 under the old Jukebox name) since
; there's no real prior Windows install base to preserve upgrade continuity
; for -- if you've actually shipped a Jukebox-named build to real users,
; keep the old GUID instead so their installer recognizes this as an
; upgrade rather than a separate, parallel install.
AppId={{0D38A2DA-D5BE-46EB-A6B7-9BEDA0930478}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Notorious B.P.M.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=installer_output
OutputBaseFilename=NotoriousBPMSetup
Compression=lzma2
SolidCompression=yes
; PyInstaller produces a native-architecture build (whatever CPU the
; machine running PyInstaller has) -- there's no cross-compiling to a
; different architecture. This installer.iss, as configured, targets the
; ARM64 build actually made and tested (see BUILD_WINDOWS.md); an x86/x64
; build needs its own separate build+install cycle on an x86/x64 machine,
; with this line changed to "x64compatible" for that build instead.
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
WizardStyle=modern
DisableProgramGroupPage=yes
; Per-user install by default -- no admin prompt needed, and the app's own
; data (config, library index, art cache, trash) already lives under the
; user's own AppData, not Program Files.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Everything PyInstaller produced -- the exe plus its bundled Python
; runtime, libraries, and the static/ web assets.
Source: "dist\NotoriousBPM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The uninstaller only removes what it installed (Program Files). It
; deliberately leaves your music library untouched, and leaves the app's own
; data (ratings, playlists, the library index, trash) under
; %AppData%\Jukebox (the storage folder name itself is unchanged by this
; rename -- see BUILD_WINDOWS.md) in case you reinstall later. Delete that
; folder by hand if you want a completely clean uninstall.

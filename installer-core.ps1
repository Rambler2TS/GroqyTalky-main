#Requires -Version 5.1
<#
.SYNOPSIS
    GroqyTalky Installer (worker — do not run directly; use the .bat file)
.DESCRIPTION
    Copies GroqyTalky.exe and readme.htm to a user-chosen folder, creates Desktop
    and Start Menu shortcuts, registers the app in Programs and Features, and drops
    an uninstaller.  Launched by "Run Me to Install GroqyTalky.bat".
#>

param()

$AppName    = "GroqyTalky"
$AppVersion = "0.43"
$ExeName    = "GroqyTalky.exe"
$ReadmeName = "readme.htm"

Add-Type -AssemblyName System.Windows.Forms

# ---------------------------------------------------------------------------
# Locate required files (must be next to this script)
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceExe = Join-Path $ScriptDir $ExeName

if (-not (Test-Path $SourceExe)) {
    [System.Windows.Forms.MessageBox]::Show(
        "$ExeName was not found next to the installer files.`n`nPlease make sure all installer files are in the same folder as $ExeName and try again.",
        "$AppName Installer",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}

$SourceReadme = Join-Path $ScriptDir $ReadmeName

# ---------------------------------------------------------------------------
# Detect existing installation via registry
# ---------------------------------------------------------------------------
$RegPath    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$InstallDir = $null

$existingLocation = $null
if (Test-Path $RegPath) {
    try {
        $existingLocation = (Get-ItemProperty -Path $RegPath -Name InstallLocation -ErrorAction Stop).InstallLocation
    } catch {}
}

if ($existingLocation -and (Test-Path (Join-Path $existingLocation $ExeName))) {
    $existingVersion = $null
    try {
        $existingVersion = (Get-ItemProperty -Path $RegPath -Name DisplayVersion -ErrorAction Stop).DisplayVersion
    } catch {}

    $verLine = if ($existingVersion) { "Installed version: $existingVersion`nNew version:       $AppVersion" } else { "New version: $AppVersion" }

    $choice = [System.Windows.Forms.MessageBox]::Show(
        "An existing installation of $AppName was found at:`n$existingLocation`n`n$verLine`n`nUpdate it now? Your settings and API key will be kept.`n`n(Choose No to pick a different install folder instead.)",
        "$AppName $AppVersion - Installer",
        [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )

    if ($choice -eq [System.Windows.Forms.DialogResult]::Cancel) {
        exit 0
    } elseif ($choice -eq [System.Windows.Forms.DialogResult]::Yes) {
        $InstallDir = $existingLocation
    }
    # No = fall through to folder picker below
}

# ---------------------------------------------------------------------------
# Folder picker — only shown for new installs or if user chose No above
# ---------------------------------------------------------------------------
if (-not $InstallDir) {
    $DefaultDir = if ($existingLocation) { $existingLocation } else { Join-Path $env:LOCALAPPDATA $AppName }
    $browser = New-Object System.Windows.Forms.FolderBrowserDialog
    $browser.Description         = "Choose where to install $AppName ${AppVersion}:"
    $browser.SelectedPath        = $DefaultDir
    $browser.ShowNewFolderButton = $true

    if ($browser.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 0   # user cancelled
    }

    $InstallDir = $browser.SelectedPath
}

# ---------------------------------------------------------------------------
# Copy files to install folder
# ---------------------------------------------------------------------------
try {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Path $SourceExe -Destination (Join-Path $InstallDir $ExeName) -Force
    if (Test-Path $SourceReadme) {
        Copy-Item -Path $SourceReadme -Destination (Join-Path $InstallDir $ReadmeName) -Force
    }
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "Failed to copy files to:`n$InstallDir`n`n$_",
        "$AppName Installer",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}

# ---------------------------------------------------------------------------
# Write uninstaller into the install folder
# ---------------------------------------------------------------------------
$UninstallScript = Join-Path $InstallDir "Uninstall-GroqyTalky.ps1"

# Single-quoted here-string: nothing is expanded here — variables expand when
# the uninstaller itself runs, which is exactly what we need.
$UninstallContent = @'
#Requires -Version 5.1
# GroqyTalky Uninstaller — created by installer-core.ps1

Add-Type -AssemblyName System.Windows.Forms

$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName    = "GroqyTalky"

$r = [System.Windows.Forms.MessageBox]::Show(
    "This will remove $AppName and all its data files (config, logs, saved recordings) from:`n`n$InstallDir`n`nContinue?",
    "Uninstall $AppName",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Warning
)
if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { exit 0 }

# App files created during normal use
$filesToRemove = @(
    "GroqyTalky.exe",
    "readme.htm",
    "config.json",
    ".env",
    "last_recording.wav",
    "ramblings_log.txt"
)
foreach ($f in $filesToRemove) {
    $p = Join-Path $InstallDir $f
    if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
}

$dataDir = Join-Path $InstallDir "data"
if (Test-Path $dataDir) { Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue }

# Shortcuts
$StartMenuLink = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\GroqyTalky.lnk"
if (Test-Path $StartMenuLink) { Remove-Item $StartMenuLink -Force -ErrorAction SilentlyContinue }

$DesktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "GroqyTalky.lnk"
if (Test-Path $DesktopLink) { Remove-Item $DesktopLink -Force -ErrorAction SilentlyContinue }

# Programs and Features registry entry
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GroqyTalky" `
    -Recurse -Force -ErrorAction SilentlyContinue

[System.Windows.Forms.MessageBox]::Show(
    "$AppName has been removed.",
    "Uninstall Complete",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null

# Self-delete this script and attempt to remove the (now-empty) folder
$me = $MyInvocation.MyCommand.Path
Start-Process cmd.exe -ArgumentList "/c timeout /t 2 >nul & del /f /q `"$me`" & rmdir /q `"$InstallDir`"" -WindowStyle Hidden
'@

Set-Content -Path $UninstallScript -Value $UninstallContent -Encoding UTF8

# ---------------------------------------------------------------------------
# Shortcuts  (Start Menu + Desktop)
# ---------------------------------------------------------------------------
$InstalledExe = Join-Path $InstallDir $ExeName
$WshShell     = New-Object -ComObject WScript.Shell

$StartMenuDir  = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$StartMenuLink = Join-Path $StartMenuDir "$AppName.lnk"
$sc1 = $WshShell.CreateShortcut($StartMenuLink)
$sc1.TargetPath       = $InstalledExe
$sc1.WorkingDirectory = $InstallDir
$sc1.Description      = "$AppName $AppVersion - voice transcription"
$sc1.Save()

$DesktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"
$sc2 = $WshShell.CreateShortcut($DesktopLink)
$sc2.TargetPath       = $InstalledExe
$sc2.WorkingDirectory = $InstallDir
$sc2.Description      = "$AppName $AppVersion - voice transcription"
$sc2.Save()

# ---------------------------------------------------------------------------
# Register in Programs and Features (HKCU — no elevation required)
# ---------------------------------------------------------------------------
$RegPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
New-Item -Path $RegPath -Force | Out-Null
$props = @{
    DisplayName      = "$AppName $AppVersion"
    DisplayVersion   = $AppVersion
    Publisher        = $AppName
    InstallLocation  = $InstallDir
    UninstallString  = "powershell.exe -ExecutionPolicy Bypass -File `"$UninstallScript`""
    NoModify         = 1
    NoRepair         = 1
}
foreach ($kv in $props.GetEnumerator()) {
    $type = if ($kv.Value -is [int]) { "DWord" } else { "String" }
    Set-ItemProperty -Path $RegPath -Name $kv.Key -Value $kv.Value -Type $type
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
[System.Windows.Forms.MessageBox]::Show(
    "$AppName $AppVersion installed successfully to:`n$InstallDir`n`nShortcuts added to Desktop and Start Menu.`n`nTo uninstall, use Programs and Features in Windows Settings, or run Uninstall-GroqyTalky.ps1 from the install folder.",
    "Installation Complete",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null

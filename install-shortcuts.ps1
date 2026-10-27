$ErrorActionPreference = 'Stop'
$root = 'E:\AI\voice'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$ctl = Join-Path $root 'voice-ctl.ps1'

if (-not (Test-Path $pythonw)) { throw "pythonw not found: $pythonw" }
if (-not (Test-Path $ctl)) { throw "control script not found: $ctl" }

$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$icon = Join-Path $root 'assets\voice.ico'
if (-not (Test-Path $icon)) { throw "icon not found: $icon (run make-icon.py)" }
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shell = New-Object -ComObject WScript.Shell

# One entry point: double-click starts it, double-click again stops it.
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ctl + '" toggle'

# Remove the earlier split shortcuts so there is exactly one thing.
$stale = @(
    (Join-Path $desktop 'Voice dictation.lnk'),
    (Join-Path $startMenu 'Voice dictation.lnk'),
    (Join-Path $desktop 'Voice toggle.lnk')
)
foreach ($old in $stale) {
    if (Test-Path $old) { Remove-Item $old -Force; "removed: $old" }
}

foreach ($folder in @($desktop, $startMenu)) {
    $path = Join-Path $folder 'Voice.lnk'
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = 'Local voice dictation: double-click to start, again to stop'
    $shortcut.Save()
    "created: $path"
}

"=== verify ==="
foreach ($folder in @($desktop, $startMenu)) {
    $path = Join-Path $folder 'Voice.lnk'
    $s = $shell.CreateShortcut($path)
    "{0}`n   -> {1} {2}" -f $path, $s.TargetPath, $s.Arguments
}

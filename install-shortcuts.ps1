$ErrorActionPreference = 'Stop'
$root = 'E:\AI\voice'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$vbs = Join-Path $root 'start-voice.vbs'
$ctl = Join-Path $root 'voice-ctl.ps1'

if (-not (Test-Path $pythonw)) { throw "pythonw not found: $pythonw" }
if (-not (Test-Path $vbs)) { throw "launcher not found: $vbs" }

$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

$shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param($Path, $Target, $Arguments, $Icon, $Description)

    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = $Icon
    $shortcut.Description = $Description
    $shortcut.Save()
    "created: $Path"
}

# Start (idempotent: a second copy exits on the single-instance mutex)
New-Shortcut -Path (Join-Path $desktop 'Voice dictation.lnk') -Target $wscript -Arguments ('"' + $vbs + '"') -Icon "$pythonw,0" -Description 'Start local voice dictation'
New-Shortcut -Path (Join-Path $startMenu 'Voice dictation.lnk') -Target $wscript -Arguments ('"' + $vbs + '"') -Icon "$pythonw,0" -Description 'Start local voice dictation'

# Toggle (start if stopped, stop if running) and status, no console window
$toggleArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ctl + '" toggle'
$statusArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ctl + '" status'
New-Shortcut -Path (Join-Path $desktop 'Voice toggle.lnk') -Target $powershell -Arguments $toggleArgs -Icon "$pythonw,0" -Description 'Start or stop voice dictation'

"=== verify ==="
foreach ($lnk in (Join-Path $desktop 'Voice dictation.lnk'), (Join-Path $startMenu 'Voice dictation.lnk'), (Join-Path $desktop 'Voice toggle.lnk')) {
    if (Test-Path $lnk) {
        $s = $shell.CreateShortcut($lnk)
        "{0}`n   -> {1} {2}" -f $lnk, $s.TargetPath, $s.Arguments
    } else { "MISSING: $lnk" }
}

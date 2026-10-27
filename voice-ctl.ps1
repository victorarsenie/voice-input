param(
    [ValidateSet('start', 'stop', 'restart', 'toggle', 'status', 'log', 'mode')]
    [string]$Action = 'status',
    [string]$Mode
)

$root = 'E:\AI\voice'
$logPath = Join-Path $root 'logs\voice.log'

function Get-Listener {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*voice.py*' }
}

function Stop-Listener {
    $procs = Get-Listener
    if (-not $procs) { return $false }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(6)
    while ((Get-Date) -lt $deadline -and (Get-Listener)) { Start-Sleep -Milliseconds 200 }
    return $true
}

function Start-Listener {
    if (Get-Listener) { return 'already running' }
    Start-Process wscript.exe -ArgumentList (Join-Path $root 'start-voice.vbs')

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        if ((Get-Listener) -and (Test-Path $logPath)) {
            $tail = Get-Content $logPath -Tail 3 -ErrorAction SilentlyContinue
            if ($tail -match 'ready: hold') { return 'started' }
        }
    }
    return 'started but no ready line yet (check the log)'
}

function Show-Status {
    $procs = Get-Listener
    if (-not $procs) { 'voice: stopped'; return }
    $tail = if (Test-Path $logPath) { Get-Content $logPath -Tail 1 } else { '(no log)' }
    "voice: running (pid $((($procs | Select-Object -ExpandProperty ProcessId) -join ', ')))"
    "last log: $tail"
}

switch ($Action) {
    'start' { "voice: " + (Start-Listener) }
    'stop' { if (Stop-Listener) { 'voice: stopped' } else { 'voice: already stopped' } }
    'restart' { $null = Stop-Listener; Start-Sleep -Milliseconds 500; "voice: " + (Start-Listener) }
    'toggle' {
        if (Get-Listener) { $null = Stop-Listener; 'voice: stopped' }
        else { "voice: " + (Start-Listener) }
    }
    'status' { Show-Status }
    'log' { if (Test-Path $logPath) { Get-Content $logPath -Tail 20 } else { 'no log yet' } }
    'mode' {
        $cfgPath = Join-Path $root 'voice.json'
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if (-not $Mode) { "voice: mode is $($cfg.mode)"; return }
        if ($Mode -notin @('submit', 'compose')) { "voice: mode must be submit or compose"; return }
        $cfg.mode = $Mode
        $json = $cfg | ConvertTo-Json -Depth 10
        [IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        $null = Stop-Listener; Start-Sleep -Milliseconds 500
        "voice: mode set to $Mode; " + (Start-Listener)
    }
}

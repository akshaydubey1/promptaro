# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
# Promptaro launcher for Windows - Author: Akshay Dubey
# Starts Promptaro quietly in the background and opens its window (Edge/Chrome app mode).
# First run installs what it needs (about a minute).
$ErrorActionPreference = "Continue"     # native tools (uv) write progress to stderr; check exit codes instead
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Here
$Port = if ($env:PROMPTARO_PORT) { $env:PROMPTARO_PORT } elseif ($env:TILLWRIGHT_PORT) { $env:TILLWRIGHT_PORT } else { "8770" }
$Url = "http://localhost:$Port"          # what the window opens
$Probe = "http://127.0.0.1:$Port"        # what the checks call: "localhost" can try IPv6 first on Windows and time out
$Data = Join-Path $env:USERPROFILE ".promptaro"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$Log = Join-Path $Data "promptaro.log"
$ServerLog = Join-Path $Data "server.log"
if (-not $env:SMART_ROUTER_ROOT) { $env:SMART_ROUTER_ROOT = (Resolve-Path (Join-Path $Here "..\smart-router")).Path }

function Say($m) { Write-Host $m; Add-Content -Path $Log -Value "$(Get-Date -Format s) $m" -Encoding UTF8 }
function Fail($m) {
  Write-Host ""
  Write-Host "Promptaro didn't start: $m" -ForegroundColor Red
  foreach ($f in @($ServerLog, $Log)) {
    if (Test-Path $f) { Write-Host "`n--- last lines of $f ---"; Get-Content $f -Tail 25 | ForEach-Object { Write-Host $_ } }
  }
  if (-not $env:CI) { Read-Host "`nPress Enter to close" }
  exit 1
}
function Open-Window {
  if ($env:PROMPTARO_NO_WINDOW -or $env:TILLWRIGHT_NO_WINDOW) { return }   # started by a command-line launcher
  $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
  $chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
  foreach ($b in $chrome + $edge) { if ($b -and (Test-Path $b)) { Start-Process $b "--app=$Url --new-window"; return } }
  Start-Process $Url
}
function Healthy { try { Invoke-RestMethod "$Probe/healthz" -TimeoutSec 3 } catch { $null } }

$h = Healthy
if ($h) {
  if ($h.update_ready) {
    try { Invoke-RestMethod -Method Post -Headers @{ "x-promptaro" = "1" } "$Probe/api/restart" | Out-Null } catch {}
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep -Milliseconds 500; $n = Healthy; if ($n -and -not $n.update_ready) { break } }
  }
  Open-Window; exit 0
}

$uvDir = Join-Path $env:USERPROFILE ".local\bin"
$env:Path = "$uvDir;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "First run: installing Python tools (about a minute)..."
  powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex" *>> $Log
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "couldn't install uv (needs internet the first time)." }
}
$py = Join-Path $Here ".venv\Scripts\python.exe"
$stamp = Join-Path $Here ".venv\.installed"
$stampTime = [datetime]::MinValue                              # a missing stamp always means "reinstall"
if (Test-Path $stamp) { $stampTime = (Get-Item $stamp).LastWriteTime }
if (-not (Test-Path $py) -or (Get-Item "requirements.txt").LastWriteTime -gt $stampTime) {
  Say "Setting up Promptaro (one time, about a minute)..."
  if (-not (Test-Path $py)) {
    & uv venv --quiet --python 3.12 .venv *>> $Log
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { Fail "couldn't create the Python environment." }
  }
  & uv pip install --quiet --python $py -r requirements.txt *>> $Log
  if ($LASTEXITCODE -ne 0) { Remove-Item $stamp -ErrorAction SilentlyContinue; Fail "couldn't install Promptaro's packages." }
  New-Item -ItemType File -Force -Path $stamp | Out-Null
}
Say "Starting Promptaro..."
$proc = Start-Process -PassThru -WindowStyle Hidden -FilePath $py `
  -ArgumentList "-m","uvicorn","desk.server:app","--host","127.0.0.1","--port",$Port,"--log-level","warning" `
  -RedirectStandardError $ServerLog -RedirectStandardOutput (Join-Path $Data "server.out.log")
for ($i = 0; $i -lt 360; $i++) {                               # up to 90 s: the first start on Windows is slow
  if (Healthy) { Open-Window; exit 0 }
  if ($proc.HasExited) { Fail "the server stopped (exit code $($proc.ExitCode))." }
  Start-Sleep -Milliseconds 250
}
Fail "it didn't answer within 90 seconds."

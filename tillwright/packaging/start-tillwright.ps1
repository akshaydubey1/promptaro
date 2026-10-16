# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Tillwright launcher for Windows — Author: Akshay Dubey
# Starts Tillwright quietly in the background and opens its window (Edge/Chrome app mode).
# First run installs what it needs (about a minute).
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Here
$Port = if ($env:TILLWRIGHT_PORT) { $env:TILLWRIGHT_PORT } else { "8770" }
$Url = "http://localhost:$Port"
$Data = Join-Path $env:USERPROFILE ".tillwright"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$Log = Join-Path $Data "tillwright.log"
if (-not $env:SMART_ROUTER_ROOT) { $env:SMART_ROUTER_ROOT = (Resolve-Path (Join-Path $Here "..\smart-router")).Path }

function Open-Window {
  $edge = @("$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
  $chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
  foreach ($b in $chrome + $edge) { if (Test-Path $b) { Start-Process $b "--app=$Url --new-window"; return } }
  Start-Process $Url
}
function Healthy { try { Invoke-RestMethod "$Url/healthz" -TimeoutSec 2 } catch { $null } }

$h = Healthy
if ($h) {
  if ($h.update_ready) {
    try { Invoke-RestMethod -Method Post -Headers @{ "x-tillwright" = "1" } "$Url/api/restart" | Out-Null } catch {}
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep -Milliseconds 500; $n = Healthy; if ($n -and -not $n.update_ready) { break } }
  }
  Open-Window; exit 0
}

$uvDir = Join-Path $env:USERPROFILE ".local\bin"
$env:Path = "$uvDir;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "First run: installing Python tools (about a minute)..."
  powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex" *>> $Log
}
$py = Join-Path $Here ".venv\Scripts\python.exe"
$stamp = Join-Path $Here ".venv\.installed"
if (-not (Test-Path $py) -or ((Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $stamp -ErrorAction SilentlyContinue).LastWriteTime)) {
  Write-Host "Setting up Tillwright (one time)..."
  uv venv --quiet --python 3.12 .venv *>> $Log
  uv pip install --quiet --python $py -r requirements.txt *>> $Log
  New-Item -ItemType File -Force -Path $stamp | Out-Null
}
Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList "-m","uvicorn","desk.server:app","--host","127.0.0.1","--port",$Port,"--log-level","warning" -RedirectStandardError (Join-Path $Data "server.log")
for ($i = 0; $i -lt 120; $i++) { if (Healthy) { Open-Window; exit 0 }; Start-Sleep -Milliseconds 250 }
Write-Host "Tillwright didn't start. Details: $Log"; Read-Host "Press Enter to close"

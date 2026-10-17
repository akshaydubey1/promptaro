# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Adds Promptaro to the Start menu and desktop — Author: Akshay Dubey
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  $lnk = $ws.CreateShortcut((Join-Path $dir "Promptaro.lnk"))
  $lnk.TargetPath = "powershell.exe"
  $lnk.Arguments = "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File `"$Here\packaging\start-promptaro.ps1`""
  $lnk.IconLocation = "$Here\packaging\icon.ico"
  $lnk.WorkingDirectory = $Here
  $lnk.Save()
  Remove-Item (Join-Path $dir "Tillwright.lnk") -ErrorAction SilentlyContinue   # old shortcut, if present
}
Write-Host "Promptaro is in your Start menu and on your desktop."

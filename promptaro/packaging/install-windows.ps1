# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Adds Promptaro to the Start menu and desktop - Author: Akshay Dubey
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

# Put the coding-tool launchers (promptaro-claude, promptaro-env) on the user's PATH.
# Read/write the raw registry value: GetEnvironmentVariable expands %VAR% entries,
# and saving the expanded form back destroys them (a well-known PATH corruption).
$bin = Join-Path $env:USERPROFILE ".promptaro\bin"
$envKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Environment")
$userPath = [string]$envKey.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
if (-not ($userPath -split ";" | Where-Object { $_ -ieq $bin })) {
  $newPath = if ($userPath) { $userPath.TrimEnd(";") + ";" + $bin } else { $bin }
  $envKey.SetValue("Path", $newPath, [Microsoft.Win32.RegistryValueKind]::ExpandString)
  # Tell Windows the environment changed, or only apps restarted after logout see it.
  try {
    $sig = '[DllImport("user32.dll")] public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.IntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.IntPtr lpdwResult);'
    $w = Add-Type -MemberDefinition $sig -Name PromptaroNative -Namespace Win32 -PassThru
    $res = [System.IntPtr]::Zero
    $w.SendMessageTimeout([System.IntPtr]0xffff, 0x1A, [System.IntPtr]::Zero, "Environment", 2, 1000, [ref]$res) | Out-Null
  } catch {}
  Write-Host "Added $bin to your PATH (new terminals pick it up - promptaro-claude etc.)."
}
$envKey.Close()

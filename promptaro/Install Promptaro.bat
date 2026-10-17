@echo off
rem Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
rem Installs the shortcuts, then starts Promptaro right away.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0packaging\install-windows.ps1"
start "" powershell -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File "%~dp0packaging\start-promptaro.ps1"
echo Promptaro is starting - it opens in your browser in a moment.
pause

@echo off
setlocal
chcp 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\local\restore.ps1"
set "EAM_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EAM_EXIT_CODE%

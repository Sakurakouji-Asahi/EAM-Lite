@echo off
setlocal

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_eam_lite_local.ps1"
set "EAM_EXIT_CODE=%ERRORLEVEL%"

if not "%EAM_EXIT_CODE%"=="0" (
    echo.
    pause
)

exit /b %EAM_EXIT_CODE%

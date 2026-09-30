@echo off
setlocal
chcp 65001 >nul
set "MANAGER=%~dp0scripts\Manage-CodexCrossProviderBridge.ps1"
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=restart"

if not exist "%MANAGER%" (
    echo [ERROR] Not found: %MANAGER%
    echo         Keep this launcher next to the scripts directory.
    pause
    exit /b 2
)

where pwsh.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pwsh.exe ^(PowerShell 7^) was not found on PATH.
    echo         Windows PowerShell 5.1 is not supported by these scripts.
    pause
    exit /b 2
)

echo Bridge action: %ACTION%
echo.

pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%MANAGER%" -Action %ACTION%
set "EXITCODE=%ERRORLEVEL%"
echo.

if not "%EXITCODE%"=="0" (
    echo [FAILED] %ACTION% exited with code %EXITCODE%
    echo          Press any key to close.
    pause >nul
    exit /b %EXITCODE%
)

echo [OK] Window closes in 5 seconds ...
"%SystemRoot%\System32\timeout.exe" /t 5 /nobreak >nul 2>nul
exit /b 0

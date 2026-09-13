@echo off
chcp 65001 >nul
title QQ Dance 3.0 One-click Build

echo.
echo ========================================
echo        QQ Dance 3.0 One-click Build
echo ========================================
echo.
echo This builds the current source into the dist package.
echo The current working package is kept until the new build succeeds.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_release.ps1"
set "BUILD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%BUILD_EXIT_CODE%"=="0" (
    echo Build failed. Review the error above.
) else (
    echo Build succeeded. You can close this window.
)
echo.
pause
exit /b %BUILD_EXIT_CODE%

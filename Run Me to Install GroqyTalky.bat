@echo off
echo ============================================
echo  GroqyTalky Installer
echo ============================================
echo.

REM --- Check all required files are present ---
if not exist "%~dp0GroqyTalky.exe" (
    echo ERROR: GroqyTalky.exe was not found in this folder.
    echo.
    echo Please make sure ALL files are in the same folder:
    echo   - GroqyTalky.exe
    echo   - Run Me to Install GroqyTalky.bat
    echo   - installer-core.ps1
    echo.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
)

if not exist "%~dp0installer-core.ps1" (
    echo ERROR: installer-core.ps1 was not found in this folder.
    echo.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
)

echo All files found. Starting installer...
echo.

PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-core.ps1"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ============================================
    echo  Installer finished with error code: %ERRORLEVEL%
    echo ============================================
    echo.
    echo If you did not see an error message above, your system may
    echo have blocked the script. Try right-clicking installer-core.ps1
    echo and choosing "Run with PowerShell".
    echo.
    echo Press any key to close this window.
    pause >nul
) else (
    echo.
    echo Installation complete. This window will close in 4 seconds.
    timeout /t 4 >nul
)

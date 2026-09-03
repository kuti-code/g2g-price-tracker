@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "G2G_EXE_PATH=%CD%\G2GPriceTracker.exe"
set "G2G_WORK_DIR=%CD%"

where py >nul 2>nul
if errorlevel 1 (
    echo Python Launcher was not found. Install Python 3.11 or 3.12 first.
    goto :error
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating the virtual environment...
    py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo [1/5] Existing virtual environment found.
)

echo [2/5] Updating pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/5] Installing project and build tools...
".venv\Scripts\python.exe" -m pip install --upgrade -e ".[dev]"
if errorlevel 1 goto :error

if exist "G2GPriceTracker.exe" del /q "G2GPriceTracker.exe"
if exist "G2GPriceTracker.exe" goto :close_running_app
if exist "dist\G2GPriceTracker.exe" del /q "dist\G2GPriceTracker.exe"
if exist "G2GPriceTracker.spec" del /q "G2GPriceTracker.spec"

echo [4/5] Building G2GPriceTracker.exe...
".venv\Scripts\python.exe" tools\build_exe.py
if errorlevel 1 goto :error

if not exist "G2GPriceTracker.exe" if exist "dist\G2GPriceTracker.exe" move /y "dist\G2GPriceTracker.exe" "G2GPriceTracker.exe" >nul
if not exist "G2GPriceTracker.exe" goto :missing_output
if exist "dist\G2GPriceTracker.exe" del /q "dist\G2GPriceTracker.exe"
if exist "G2GPriceTracker.spec" del /q "G2GPriceTracker.spec"

echo [5/5] Creating desktop shortcut...
powershell -NoProfile -Command "$shell = New-Object -ComObject WScript.Shell; $shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'G2G Price Tracker.lnk')); $shortcut.TargetPath = $env:G2G_EXE_PATH; $shortcut.WorkingDirectory = $env:G2G_WORK_DIR; $shortcut.IconLocation = $env:G2G_EXE_PATH + ',0'; $shortcut.Description = 'G2G Price Tracker'; $shortcut.Save()"
if errorlevel 1 goto :error

echo.
echo Setup complete: G2GPriceTracker.exe
echo Desktop shortcut created: G2G Price Tracker
start "" "%G2G_EXE_PATH%"
exit /b 0

:missing_output
echo.
echo Build finished but G2GPriceTracker.exe was not found.
goto :error

:close_running_app
echo.
echo Close the running G2GPriceTracker.exe and run this installer again.
goto :error

:error
echo.
echo Setup could not be completed. Check the message above.
pause
exit /b 1

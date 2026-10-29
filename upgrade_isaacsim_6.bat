@echo off
setlocal enabledelayedexpansion
title PlanLift — Isaac Sim 6.0 Upgrade

echo ============================================================
echo  PlanLift: Isaac Sim 5.1 → 6.0 Upgrade
echo ============================================================
echo.

:: ── Step 1: Check / Install Python 3.12 ─────────────────────────────
echo [1/4] Checking for Python 3.12...
set PY312=
for %%P in (
    "C:\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%ProgramFiles%\Python312\python.exe"
) do (
    if exist %%P (
        set PY312=%%~P
        echo   Found: !PY312!
        goto :found_py
    )
)

:: Try py launcher
py -3.12 --version >nul 2>&1
if %ERRORLEVEL%==0 (
    set PY312=py -3.12
    echo   Found via py launcher
    goto :found_py
)

echo.
echo   Python 3.12 NOT FOUND.
echo.
echo   Please install Python 3.12 from:
echo     https://www.python.org/downloads/release/python-3129/
echo.
echo   Download "Windows installer (64-bit)" and install with:
echo     [x] Add Python to PATH
echo     [x] Install for all users (optional)
echo.
echo   After installing, run this script again.
echo.
pause
exit /b 1

:found_py
echo.

:: ── Step 2: Create venv with Python 3.12 ────────────────────────────
set ISAAC6_DIR=D:\ISAACSIM\isaacsim6
set VENV_DIR=%ISAAC6_DIR%\venv

echo [2/4] Creating Python 3.12 venv at %VENV_DIR%...
if not exist "%ISAAC6_DIR%" mkdir "%ISAAC6_DIR%"

if "%PY312%"=="py -3.12" (
    py -3.12 -m venv "%VENV_DIR%"
) else (
    "%PY312%" -m venv "%VENV_DIR%"
)

if %ERRORLEVEL% NEQ 0 (
    echo   ERROR: Failed to create venv
    pause
    exit /b 1
)

:: Activate and upgrade pip
call "%VENV_DIR%\Scripts\activate.bat"
python -m pip install --upgrade pip >nul 2>&1
echo   venv created OK

echo.

:: ── Step 3: Install Isaac Sim 6.0 ───────────────────────────────────
echo [3/4] Installing Isaac Sim 6.0 via pip (this will download ~15 GB)...
echo   This may take 20-60 minutes depending on your internet speed.
echo.
pip install isaacsim==6.0.0

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   ERROR: pip install failed.
    echo   If you see a CUDA error, install CUDA Toolkit 12.x first.
    echo   If you see a network error, check your internet connection.
    pause
    exit /b 1
)

echo.
echo   Isaac Sim 6.0 installed OK

:: ── Step 4: Find kit.exe and update .env ────────────────────────────
echo.
echo [4/4] Updating PlanLift configuration...

:: Find kit.exe in the installed package
set KIT_EXE=
for /r "%VENV_DIR%\Lib\site-packages\isaacsim" %%F in (kit.exe) do (
    set KIT_EXE=%%F
    goto :found_kit
)

:: Fallback: check direct install
for /r "%ISAAC6_DIR%" %%F in (kit.exe) do (
    set KIT_EXE=%%F
    goto :found_kit
)

echo   WARNING: kit.exe not found in the install.
echo   You may need to set ISAAC_SIM_PATH manually in .env
goto :done

:found_kit
set ISAAC_DIR=%%~dpKIT_EXE
echo   kit.exe found at: %KIT_EXE%

:: Update .env
set ENV_FILE=D:\PlanLift\PlanLift\.env
if exist "%ENV_FILE%" (
    :: Backup
    copy "%ENV_FILE%" "%ENV_FILE%.bak.isaacsim5" >nul 2>&1
    echo   .env backed up
)

:: Write updated ISAAC_SIM_PATH
echo   Updating ISAAC_SIM_PATH in .env...
powershell -Command "(Get-Content '%ENV_FILE%') -replace 'ISAAC_SIM_PATH=.*', 'ISAAC_SIM_PATH=%KIT_EXE:\=\\%' | Set-Content '%ENV_FILE%' -Encoding utf8"

:done
echo.
echo ============================================================
echo  UPGRADE COMPLETE
echo ============================================================
echo.
echo  Next steps:
echo    1. Close and re-run your PlanLift API server
echo    2. Try "Open in Isaac Sim" from the web app
echo.
echo  If Isaac Sim 6.0 still crashes, check your GPU driver:
echo    - RTX 5080 needs driver 570+ for Isaac Sim 6.0
echo    - Current driver: run "nvidia-smi" to check
echo.
pause

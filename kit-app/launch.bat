@echo off
setlocal

set ISAAC_SIM=D:\ISAACSIM\isaacsim6
set KIT_EXE=%ISAAC_SIM%\kit\kit.exe
set KIT_FILE=%~dp0apps\planlift_timelapse.kit
set EXT_FOLDER=%~dp0exts
set STARTUP=%~dp0startup.py

echo ============================================================
echo  PlanLift Timelapse Viewer  (Isaac Sim 6.0)
echo ============================================================
echo.

"%KIT_EXE%" "%KIT_FILE%" ^
    --ext-folder "%EXT_FOLDER%" ^
    --enable planlift.ui_setup ^
    --exec "%STARTUP%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Exited with code %ERRORLEVEL%
    pause
)

endlocal

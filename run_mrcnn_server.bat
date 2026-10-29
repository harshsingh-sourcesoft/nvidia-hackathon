@echo off
setlocal

set "VENV=D:\PlanLift\Real_Estate\Real_Estate\FloorPlanTo3D-API\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "REQS=D:\PlanLift\Real_Estate\Real_Estate\FloorPlanTo3D-API\requirements_minimal.txt"
set "SERVER=%~dp0api_server_mrcnn.py"

if not exist "%PYTHON%" (
    echo [setup] Creating venv at %VENV%...
    python -m venv "%VENV%"
    if errorlevel 1 ( echo [ERROR] venv creation failed & pause & exit /b 1 )
    echo [setup] Installing requirements...
    "%PIP%" install -r "%REQS%"
    if errorlevel 1 ( echo [ERROR] pip install failed & pause & exit /b 1 )
    echo [setup] Done.
)

echo Starting Mask R-CNN server on http://localhost:8001 ...
echo.

REM ANTHROPIC_API_KEY is read from PlanLift/.env (see _read_env_var_mrcnn in
REM api_server_mrcnn.py) — same pattern as SKETCHFAB_TOKEN and NEBIUS_API_KEY.
REM Add it there, not here.

"%PYTHON%" "%SERVER%"

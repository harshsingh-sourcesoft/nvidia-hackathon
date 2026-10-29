@echo off
setlocal enabledelayedexpansion

REM ════════════════════════════════════════════════════════════
REM  FloorPlan-to-3D  |  End-to-End Pipeline Runner
REM  Usage:  run_pipeline.bat  <floor_plan_image>  [options]
REM
REM  Options:
REM    --size 0            Preprocessing resolution (0=native, 512, 1024)
REM    --scale 0.01        Metres per pixel (default 0.01 = 1cm/px)
REM    --wall-height 2.5   Wall extrusion height in metres
REM    --device cuda       Force GPU (auto by default)
REM    --no-viewer         Skip opening browser viewer
REM    --no-cutouts        Skip door/window boolean cutouts
REM    --ai-3d             Run Hunyuan3D-2.1 AI mesh generation (stage_3dgen.py)
REM    --texture           Enable PBR texture pass for Hunyuan3D-2.1 (requires --ai-3d)
REM ════════════════════════════════════════════════════════════

if "%~1"=="" (
    echo.
    echo  Usage: run_pipeline.bat ^<image^> [options]
    echo  Example: run_pipeline.bat input\my_floorplan.png --size 0
    echo.
    pause & exit /b 1
)

REM ── Parse arguments ──────────────────────────────────────────
set INPUT_FILE=%~1
set SIZE=0
set SCALE=0.01
set WALL_HEIGHT=2.5
set DEVICE=auto
set VIEWER=1
set NO_CUTOUTS=
set AI_3D=0
set TEXTURE=

shift
:parse_args
if "%~1"=="" goto done_parse
if "%~1"=="--size"        ( set SIZE=%~2        & shift & shift & goto parse_args )
if "%~1"=="--scale"       ( set SCALE=%~2       & shift & shift & goto parse_args )
if "%~1"=="--wall-height" ( set WALL_HEIGHT=%~2 & shift & shift & goto parse_args )
if "%~1"=="--device"      ( set DEVICE=%~2      & shift & shift & goto parse_args )
if "%~1"=="--no-viewer"   ( set VIEWER=0        & shift & goto parse_args )
if "%~1"=="--no-cutouts"  ( set NO_CUTOUTS=--no-cutouts & shift & goto parse_args )
if "%~1"=="--ai-3d"       ( set AI_3D=1         & shift & goto parse_args )
if "%~1"=="--texture"     ( set TEXTURE=--texture & shift & goto parse_args )
shift & goto parse_args
:done_parse

REM ── Validate input ───────────────────────────────────────────
if not exist "%INPUT_FILE%" (
    echo [ERROR] Input file not found: %INPUT_FILE%
    pause & exit /b 1
)

REM ── Activate venv ────────────────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause & exit /b 1
)
call venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8

REM ── Derive stem name ─────────────────────────────────────────
for %%F in ("%INPUT_FILE%") do set STEM=%%~nF

echo.
echo ════════════════════════════════════════════════════════════
echo  FloorPlan-to-3D Pipeline
echo  Input : %INPUT_FILE%
echo  Stem  : %STEM%
echo  Size  : %SIZE%   Scale: %SCALE% m/px   Wall: %WALL_HEIGHT% m
echo ════════════════════════════════════════════════════════════
echo.

REM ── Stage 1: Preprocess ──────────────────────────────────────
echo [1/5] Preprocessing image...
python stage1_preprocess.py "%INPUT_FILE%" --size %SIZE%
if errorlevel 1 ( echo [FAIL] Stage 1 failed & pause & exit /b 1 )
set PREPROCESSED=output\masks\%STEM%_preprocessed.png
echo.

REM ── Stage 2: Segment ─────────────────────────────────────────
echo [2/5] Running segmentation model...
python stage2_segment.py "%PREPROCESSED%" --original "%INPUT_FILE%" --device %DEVICE%
if errorlevel 1 ( echo [FAIL] Stage 2 failed & pause & exit /b 1 )
set MASKS=output\masks\%STEM%_masks.npz
echo.

REM ── Stage 3: Vectorize ───────────────────────────────────────
echo [3/6] Vectorizing masks to scene graph...
python stage3_vectorize.py "%MASKS%" --scale %SCALE%
if errorlevel 1 ( echo [FAIL] Stage 3 failed & pause & exit /b 1 )
set SCENE=output\vectors\%STEM%_scene.json
echo.

REM ── Stage LLM: GPT-4o enhancement (skipped if no API key) ────
echo [4/6] LLM floor plan understanding (GPT-4o)...
python stage_llm.py "%INPUT_FILE%" "%SCENE%"
echo.

REM ── Stage 4: Extrude ─────────────────────────────────────────
echo [5/6] Extruding 3D mesh...
python stage4_extrude.py "%SCENE%" --wall-height %WALL_HEIGHT% %NO_CUTOUTS%
if errorlevel 1 ( echo [FAIL] Stage 4 failed & pause & exit /b 1 )
set GLB=output\models\%STEM%.glb
echo.

REM ── Stage 3D-Gen: Hunyuan3D-2.1 AI mesh (optional) ──────────
if "%AI_3D%"=="1" (
    echo [5b] Running Hunyuan3D-2.1 AI 3D generation...
    python stage_3dgen.py "%INPUT_FILE%" --output-dir output\models --device %DEVICE% %TEXTURE%
    if errorlevel 1 ( echo [WARN] Stage 3D-Gen failed - continuing with procedural mesh )
    echo.
)

REM ── Stage 5: Viewer ──────────────────────────────────────────
if "%VIEWER%"=="1" (
    echo [6/6] Launching 3D viewer...
    python stage5_viewer.py "%GLB%"
) else (
    echo [6/6] Viewer skipped.
)

echo.
echo ════════════════════════════════════════════════════════════
echo  Pipeline complete!
echo  Outputs:
echo    Masks    : output\masks\%STEM%_*.png
echo    Vectors  : output\vectors\%STEM%_preview.png
echo    3D Model : output\models\%STEM%.glb
echo ════════════════════════════════════════════════════════════

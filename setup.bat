@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo  FloorPlan-to-3D  ^|  Windows Setup
echo ============================================================
echo.

REM ── 1. Check Python ──────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% found

REM ── 2. Create venv ───────────────────────────────────────────
if not exist "venv\" (
    echo [SETUP] Creating virtual environment...
    python -m venv venv
)
echo [OK] Virtual environment ready

REM ── 3. Activate venv ─────────────────────────────────────────
call venv\Scripts\activate.bat
echo [OK] venv activated

REM ── 4. Upgrade pip ───────────────────────────────────────────
python -m pip install --upgrade pip --quiet

REM ── 5. Install PyTorch (CUDA 12.8 nightly — required for RTX 5080 / sm_120) ──
echo [SETUP] Installing PyTorch nightly with CUDA 12.8 (RTX 50-series / Blackwell)...
pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu128 --quiet
echo [OK] PyTorch nightly cu128 installed

REM ── 6. Install pipeline deps ─────────────────────────────────
echo [SETUP] Installing pipeline dependencies...
pip install -r requirements.txt --quiet
echo [OK] All dependencies installed

REM ── 7. Create weights folder and remind user to download weights ────────────
if not exist "weights\" mkdir weights
if not exist "weights\cubicasa_model.pkl" (
    echo.
    echo [INFO] CubiCasa pretrained weights not found.
    echo        To get production-quality segmentation, download them manually:
    echo.
    echo        1. Go to: https://github.com/CubiCasa/CubiCasa5k
    echo        2. Follow the Dropbox link in their README to get:
    echo              model_best_val_loss_var.pkl
    echo        3. Place it at: weights\cubicasa_model.pkl
    echo.
    echo        The pipeline will run with a built-in fallback U-Net until
    echo        the weights are in place (outputs will be noise-like without them).
    echo.
) else (
    echo [OK] CubiCasa weights found at weights\cubicasa_model.pkl
)

REM ── 8. Create output directories ─────────────────────────────
if not exist "input\"   mkdir input
if not exist "output\"  mkdir output
if not exist "output\masks\"   mkdir output\masks
if not exist "output\vectors\" mkdir output\vectors
if not exist "output\models\"  mkdir output\models

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo    1. Drop your floor plan image into the  input\  folder
echo    2. Run:  run_pipeline.bat  your_image.png
echo ============================================================
pause

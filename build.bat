@echo off
REM ---------------------------------------------------------------------------
REM  Build a standalone BrightnessController.exe with PyInstaller (one file).
REM ---------------------------------------------------------------------------
setlocal

title Building Screen Brightness Controller

echo.
echo  [1/3] Installing dependencies...
call python -m pip install --upgrade pip
call pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: dependency installation failed.
    pause
    exit /b 1
)

echo.
echo  [2/3] Cleaning previous build artifacts...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "BrightnessController.spec" del /q "BrightnessController.spec"

echo.
echo  [3/3] Compiling standalone executable...
pyinstaller --noconfirm --onefile --windowed ^
    --name "BrightnessController" ^
    --collect-all customtkinter ^
    --collect-all screen_brightness_control ^
    brightness_controller.py

if errorlevel 1 (
    echo.
    echo  ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo  Build complete! Your executable is located at:
echo      dist\BrightnessController.exe
start "" explorer "dist"
pause

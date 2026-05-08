@echo off
chcp 936 >nul
cd /d "%~dp0"

REM check python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ and check "Add to PATH"
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo ==========================================
echo System Monitor starting...
python --version
echo Press Ctrl+C to stop
echo ==========================================
echo.

REM check PyQt6
python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo Installing PyQt6...
    pip install PyQt6
    if errorlevel 1 (
        echo [ERROR] PyQt6 install failed
        pause
        exit /b 1
    )
)

python src\main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Program exited with code %errorlevel%
    echo.
    pause
)

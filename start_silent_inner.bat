@echo off
chcp 936 >nul
cd /d "%~dp0"

REM check python
python --version >nul 2>&1
if errorlevel 1 (
    msg * "ERROR: Python not found. Install Python 3.8+ and check Add to PATH"
    exit /b 1
)

REM check PyQt6
python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    pip install PyQt6 >nul 2>&1
)

pythonw src\main.py
if errorlevel 1 (
    msg * "Start failed. Use start_console.bat for details"
)

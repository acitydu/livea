@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"
if not exist logs mkdir logs

REM check python
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python and add it to PATH. >logs\launcher.log
    exit /b 1
)

REM Use pythonw from the same interpreter; the helper bounds all child output.
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "MONITOR_PYTHON=%%P"
if not defined MONITOR_PYTHON exit /b 1
for %%P in ("%MONITOR_PYTHON%") do set "MONITOR_PYTHONW=%%~dpPpythonw.exe"
if not exist "%MONITOR_PYTHONW%" exit /b 1
"%MONITOR_PYTHONW%" "%~dp0src\launch_silent.py"
exit /b %errorlevel%

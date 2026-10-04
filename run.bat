@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Please run setup.ps1 first. See README.md for instructions.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "entry.py"

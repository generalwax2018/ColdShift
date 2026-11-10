@echo off
rem Starts the ColdShift GUI from source. Errors stay visible in this window.
cd /d "%~dp0"
echo Checking dependencies...
python -m pip install -q psutil customtkinter
echo Starting ColdShift...
python ColdShift-GUI.py
echo.
echo ColdShift has closed.
pause

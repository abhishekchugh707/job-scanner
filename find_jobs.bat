@echo off
rem Opens the Job Radar dashboard (starts the app in the background if it is not running).
cd /d "%~dp0"
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
if not exist "%PYW%" set "PYW=python"
start "" "%PYW%" app.py --open

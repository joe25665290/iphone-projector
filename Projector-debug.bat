@echo off
rem Start Projector with a console that shows the live log (for troubleshooting).
rem For everyday use, double-click Projector.pyw instead (no console window).
rem Close the Projector window (not this console) to stop cleanly.
cd /d "%~dp0"
".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause

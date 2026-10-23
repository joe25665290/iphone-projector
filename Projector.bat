@echo off
rem Start Projector. Close the Projector window (not this console) to stop cleanly.
cd /d "%~dp0"
".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause

@echo off
rem Starts the CTP500 bridge in a console window so you can watch what it does.
rem Close the window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
python ctp500_bridge.py serve --log-file bridge.log %*
pause

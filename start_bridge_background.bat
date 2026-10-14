@echo off
rem Starts the CTP500 bridge with no window. Put a shortcut to this file in
rem your Startup folder (Win+R, type shell:startup) to run it at sign-in.
rem The log is written to bridge.log in this folder.
cd /d "%~dp0"
start "" pythonw ctp500_bridge.py serve --log-file bridge.log %*

@echo off
rem Create Desktop and Start Menu shortcuts for Voice Router
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_shortcut.ps1"
pause

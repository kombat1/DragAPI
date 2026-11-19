@echo off
rem DragAPI for Windows: double-click to start. Flags: start.cmd --port 9000
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause

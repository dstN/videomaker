@echo off
:: Startet den Voxtral TTS Service.
:: Benoetigt Admin-Rechte fuer Windows-Firewall-Regel (Port 8700).

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Starte mit Admin-Rechten fuer Firewall-Regel...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-VoxtralService.ps1"
pause

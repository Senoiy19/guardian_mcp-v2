@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 guardian_telegram.py) || (python guardian_telegram.py)
pause

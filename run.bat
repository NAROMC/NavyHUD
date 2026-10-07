@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==== NavyHUD quick run (no build) ====

set PYCMD=
where py >nul 2>nul && set PYCMD=py -3
if "%PYCMD%"=="" (where python >nul 2>nul && set PYCMD=python)

if exist .venv\Scripts\python.exe goto :run
if "%PYCMD%"=="" goto :nopython
echo First time only: creating environment and installing PySide6 ...
%PYCMD% -m venv .venv
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install "PySide6>=6.6"
if errorlevel 1 goto :fail

:run
taskkill /F /IM NavyHUD.exe >nul 2>nul
taskkill /F /IM NavyHUD_PresentMon.exe >nul 2>nul
echo Starting NavyHUD (a UAC prompt will appear; choose Yes) ...
.venv\Scripts\python.exe navyhud.py
exit /b 0

:nopython
echo Python was not found. Run build.bat once first (it installs Python with winget).
pause
exit /b 1

:fail
echo.
echo !!!! Setup failed. See the messages above. !!!!
pause
exit /b 1

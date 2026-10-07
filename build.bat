@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==== NavyHUD build ====

set PYCMD=
where py >nul 2>nul && set PYCMD=py -3
if "%PYCMD%"=="" (where python >nul 2>nul && set PYCMD=python)
if "%PYCMD%"=="" goto :nopython

taskkill /F /IM NavyHUD.exe >nul 2>nul
taskkill /F /IM NavyHUD_PresentMon.exe >nul 2>nul

if not exist .venv\Scripts\python.exe (
  echo [1/5] Creating virtual environment...
  %PYCMD% -m venv .venv
  if errorlevel 1 goto :fail
)
call .venv\Scripts\activate.bat

echo [2/5] Installing packages (first time takes a few minutes)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] Downloading PresentMon (FPS engine)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_presentmon.ps1"
if not exist NavyHUD_PresentMon.exe goto :nopm

echo [4/5] Building exe...
pyinstaller --noconfirm --clean --onefile --noconsole --uac-admin --name NavyHUD --icon icon.ico --add-data "icon.ico;." --add-data "NavyHUD_PresentMon.exe;." navyhud.py
if errorlevel 1 goto :fail

echo [5/5] Copying exe...
copy /y dist\NavyHUD.exe NavyHUD.exe >nul
echo.
echo ==== DONE: NavyHUD.exe created in this folder ====
echo (It asks for administrator permission on launch - needed for FPS measurement.)
pause
exit /b 0

:nopython
echo Python was not found. Installing Python 3.12 with winget...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Python install finished. Close this window and run build.bat again.
pause
exit /b 1

:nopm
echo.
echo !!!! Could not download PresentMon. !!!!
echo Manual fix: download the latest PresentMon-x.x.x-x64.exe from
echo   https://github.com/GameTechDev/PresentMon/releases/latest
echo rename it to NavyHUD_PresentMon.exe, put it next to build.bat, then run build.bat again.
pause
exit /b 1

:fail
echo.
echo !!!! BUILD FAILED. See the messages above. !!!!
pause
exit /b 1

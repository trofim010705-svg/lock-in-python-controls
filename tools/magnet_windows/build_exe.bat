@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" goto install
py -3.11 --version >nul 2>&1
if errorlevel 1 goto default_python
py -3.11 -m venv venv
goto check_venv
:default_python
python -m venv venv
:check_venv
if not exist "venv\Scripts\python.exe" goto failed
:install
"venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements-build.txt
if errorlevel 1 goto failed
"venv\Scripts\python.exe" build.py
if errorlevel 1 goto failed
echo.
echo Done: MagnetCommandTester_v1_4_Windows_x64.zip
pause
exit /b 0
:failed
echo.
echo Build failed. See the error above. Windows x64 and Python 3.11 x64 are recommended.
pause
exit /b 1

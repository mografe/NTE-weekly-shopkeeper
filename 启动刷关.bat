@echo off
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if exist "%PY%" goto run
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do set "PY=%%i"
if exist "%PY%" goto run
echo.
echo pythonw.exe not found.
echo Please install Python 3 and add it to PATH.
echo.
pause
exit /b 1
:run
start "" "%PY%" "%~dp0nte_gui.py"
exit /b 0

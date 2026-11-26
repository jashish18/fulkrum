@echo off
rem Task Rework Dashboard: double-click to start it on Windows.
rem The first run installs what it needs (a few minutes). Later runs start in seconds.
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\TaskReworkDashboard\venv"

if exist "%VENV%\Scripts\python.exe" goto :install
echo Setting up for the first time...
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m venv "%VENV%"
) else (
    python -m venv "%VENV%"
)
if not exist "%VENV%\Scripts\python.exe" goto :nopython

:install
echo Checking the app's packages...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created a settings file called .env in this folder.
    echo Open it in Notepad to set APP_PASSWORD and LLM_API_KEY, then run this file again.
    echo.
    notepad ".env"
)

echo.
echo Starting the dashboard. Your browser opens at http://localhost:8501
echo Keep this window open while you use it. Close it to stop the app.
echo.
"%VENV%\Scripts\python.exe" -m streamlit run app.py --server.headless false
pause
exit /b 0

:nopython
echo.
echo Python wasn't found. Install Python 3.11 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" on the first screen of the installer. Then run this file again.
pause
exit /b 1

:fail
echo.
echo Installing the packages failed. Check your internet connection and run this file again.
pause
exit /b 1

@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Creating one...
    py -3.14 -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment with Python 3.14
        exit /b 1
    )
)

if exist requirements.txt (
    echo Installing packages from requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

set "QUIZ_WEBSITE_PORT="
for /f "delims=" %%P in ('.venv\Scripts\python.exe launch_website.py port') do set "QUIZ_WEBSITE_PORT=%%P"
if not defined QUIZ_WEBSITE_PORT (
    echo ERROR: Could not select a free port for the website.
    exit /b 1
)

echo Starting QuizMaster on port %QUIZ_WEBSITE_PORT%...
start "Django Server" cmd /k "cd /d ""%~dp0"" && "".venv\Scripts\python.exe"" launch_website.py serve %QUIZ_WEBSITE_PORT%"

".venv\Scripts\python.exe" launch_website.py wait %QUIZ_WEBSITE_PORT%
if errorlevel 1 exit /b 1
start "" "http://127.0.0.1:%QUIZ_WEBSITE_PORT%/admin-dashboard/"

endlocal

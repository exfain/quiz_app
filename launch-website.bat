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

start "Django Server" cmd /k "cd /d ""%~dp0"" && "".venv\Scripts\python.exe"" manage.py runserver 0.0.0.0:8000"

timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:8000/admin-dashboard/"

endlocal

@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Ambiente virtual nao encontrado. Execute instalar.bat.
    pause
    exit /b 1
)

start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py

echo.
echo O programa foi encerrado.
pause

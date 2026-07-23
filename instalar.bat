@echo off
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python nao foi encontrado.
    echo Instale o Python 3.11 ou superior e marque "Add Python to PATH".
    pause
    exit /b 1
)

py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
    copy .env.example .env
)

echo.
echo Instalacao concluida.
echo Agora edite o arquivo .env e coloque o certificado na pasta.
pause

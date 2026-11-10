@echo off
setlocal
cd /d "%~dp0"

if not exist "dados" mkdir "dados"

echo [%date% %time%] INICIO >> "dados\automatico.log"
".venv\Scripts\python.exe" app.py --automatico >> "dados\automatico.log" 2>&1
set RESULTADO=%ERRORLEVEL%
echo [%date% %time%] FIM - codigo %RESULTADO% >> "dados\automatico.log"

exit /b %RESULTADO%

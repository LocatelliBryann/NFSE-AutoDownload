@echo off
setlocal EnableExtensions DisableDelayedExpansion

cd /d "%~dp0"

echo ==========================================
echo Instalador - NFSe Downloader Final
echo ==========================================
echo Pasta do projeto:
echo %CD%
echo.

if not exist "requirements.txt" (
    echo ERRO: requirements.txt nao encontrado.
    echo O instalar.bat precisa estar na mesma pasta do projeto.
    pause
    exit /b 1
)

set "PYTHON_EXE="

echo Procurando uma instalacao valida do Python...
echo.

rem ---------------------------------------------------------
rem 1. Tenta Python diretamente pelo PATH
rem ---------------------------------------------------------
for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
    if exist "%%P" set "PYTHON_EXE=%%P"
)

rem ---------------------------------------------------------
rem 2. Tenta versões conhecidas pelo Python Launcher
rem    Só aceita se retornar um python.exe realmente existente.
rem ---------------------------------------------------------
if not defined PYTHON_EXE (
    for %%V in (3.13 3.12 3.11 3.10 3) do (
        for /f "usebackq delims=" %%P in (`py -%%V -c "import sys; print(sys.executable)" 2^>nul`) do (
            if exist "%%P" set "PYTHON_EXE=%%P"
        )

        if defined PYTHON_EXE goto python_encontrado
    )
)

:python_encontrado

if not defined PYTHON_EXE (
    echo ERRO: nenhuma instalacao funcional do Python foi encontrada.
    echo.
    echo O comando "py" existe, mas nenhuma versao do Python esta instalada.
    echo.
    echo Instale o Python 3.12 de 64 bits.
    echo Durante a instalacao:
    echo   1. Marque "Add python.exe to PATH".
    echo   2. Marque "Install launcher for all users", se aparecer.
    echo.
    echo Depois, execute este instalador novamente.
    pause
    exit /b 1
)

echo Python localizado:
echo %PYTHON_EXE%
"%PYTHON_EXE%" --version

if errorlevel 1 (
    echo.
    echo ERRO: o executavel localizado nao iniciou corretamente.
    pause
    exit /b 1
)

echo.

rem ---------------------------------------------------------
rem Remove ambiente antigo, se existir
rem ---------------------------------------------------------
if exist ".venv" (
    echo Removendo ambiente virtual incompleto ou antigo...
    rmdir /s /q ".venv"

    if exist ".venv" (
        echo.
        echo ERRO: nao foi possivel remover a pasta .venv.
        echo Feche o programa, o VS Code e terminais abertos nesta pasta.
        pause
        exit /b 1
    )
)

rem ---------------------------------------------------------
rem Cria ambiente virtual
rem ---------------------------------------------------------
echo Criando ambiente virtual...
"%PYTHON_EXE%" -m venv ".venv"

if errorlevel 1 (
    echo.
    echo ERRO: falha ao criar o ambiente virtual.
    echo.
    echo Tente executar no PowerShell:
    echo "%PYTHON_EXE%" -m venv ".venv"
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERRO: o ambiente virtual nao foi criado corretamente.
    echo Caminho esperado:
    echo %CD%\.venv\Scripts\python.exe
    pause
    exit /b 1
)

echo Ambiente virtual criado.
echo.

rem ---------------------------------------------------------
rem Atualiza ferramentas de instalação
rem ---------------------------------------------------------
echo Atualizando pip, setuptools e wheel...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel

if errorlevel 1 (
    echo.
    echo ERRO ao atualizar as ferramentas de instalacao.
    pause
    exit /b 1
)

rem ---------------------------------------------------------
rem Instala dependências
rem ---------------------------------------------------------
echo.
echo Instalando dependencias do projeto...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"

if errorlevel 1 (
    echo.
    echo ERRO durante a instalacao das dependencias.
    echo Analise a mensagem exibida imediatamente acima.
    pause
    exit /b 1
)

rem ---------------------------------------------------------
rem Prepara configuração e pastas
rem ---------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo.
        echo Arquivo .env criado a partir do .env.example.
    ) else (
        echo.
        echo AVISO: .env.example nao encontrado.
    )
) else (
    echo.
    echo O arquivo .env existente foi preservado.
)

if not exist "dados" mkdir "dados"
if not exist "dados\downloads" mkdir "dados\downloads"
if not exist "dados\jobs" mkdir "dados\jobs"
if not exist "dados\automaticos" mkdir "dados\automaticos"

rem ---------------------------------------------------------
rem Teste final
rem ---------------------------------------------------------
echo.
echo Verificando os modulos principais...
".venv\Scripts\python.exe" -c "import flask, dotenv, requests, requests_pkcs12, reportlab, qrcode, PIL; print('Dependencias verificadas com sucesso.')"

if errorlevel 1 (
    echo.
    echo ERRO: uma ou mais dependencias nao puderam ser importadas.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo Instalacao concluida com sucesso.
echo ==========================================
echo.
echo Proximos passos:
echo   1. Edite o arquivo .env.
echo   2. Coloque certificado.pfx nesta pasta.
echo   3. Execute iniciar.bat.
echo.
pause
exit /b 0

@echo off
setlocal
cd /d "%~dp0"

set "ERP_DOCKER_CONTAINER=ji-erp-v2-postgres-local"
set "DOCKER_DESKTOP=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"

docker info >nul 2>&1
if errorlevel 1 (
    echo Iniciando Docker Desktop...
    if not exist "%DOCKER_DESKTOP%" (
        echo Docker Desktop nao foi encontrado em "%DOCKER_DESKTOP%".
        pause
        exit /b 1
    )
    start "" /min "%DOCKER_DESKTOP%"
    for /l %%I in (1,1,45) do (
        docker info >nul 2>&1
        if not errorlevel 1 goto docker_ready
        timeout /t 2 /nobreak >nul
    )
    echo O Docker Desktop nao ficou pronto dentro do tempo esperado.
    pause
    exit /b 1
)

:docker_ready
docker inspect "%ERP_DOCKER_CONTAINER%" >nul 2>&1
if errorlevel 1 (
    echo O container "%ERP_DOCKER_CONTAINER%" nao foi encontrado.
    pause
    exit /b 1
)

docker start "%ERP_DOCKER_CONTAINER%" >nul 2>&1
if errorlevel 1 (
    echo Nao foi possivel iniciar o PostgreSQL local.
    pause
    exit /b 1
)

echo Aguardando PostgreSQL local...
for /l %%I in (1,1,30) do (
    docker exec "%ERP_DOCKER_CONTAINER%" pg_isready -q >nul 2>&1
    if not errorlevel 1 goto database_ready
    timeout /t 1 /nobreak >nul
)
echo O PostgreSQL local nao respondeu dentro do tempo esperado.
pause
exit /b 1

:database_ready
echo PostgreSQL pronto. Iniciando MES...
py -3.14 main.py
if errorlevel 1 pause

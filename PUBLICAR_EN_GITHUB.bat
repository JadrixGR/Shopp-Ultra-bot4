@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "DEFAULT_REPO_URL=https://github.com/JadrixGR/Shopp-Ultra-bot4.git"

echo ============================================================
echo   SHOP ULTRA - PUBLICAR SOLO EL CODIGO EN GITHUB
echo ============================================================
echo.
echo Este proceso mantiene la estructura completa de carpetas.
echo NO publica .env, shop.db, providers.json ni MIGRACION_RENDER.
echo El repositorio indicado es PUBLICO: nunca suba secretos manualmente.
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git no esta instalado.
    echo Instale Git for Windows o GitHub Desktop y vuelva a ejecutar este BAT.
    pause
    exit /b 1
)

if not exist .git git init

rem Verificar que las reglas privadas realmente sean ignoradas.
for %%F in (".env" "data\shop.db" "data\providers.json" "MIGRACION_RENDER\import_once.zip") do (
    if exist "%%~F" (
        git check-ignore -q "%%~F"
        if errorlevel 1 (
            echo ERROR CRITICO: %%~F existe pero Git no lo esta ignorando.
            echo No se publicara nada. Revise .gitignore.
            pause
            exit /b 1
        )
    )
)

rem Si alguno ya hubiera sido registrado por Git, detener el proceso.
for /f "delims=" %%F in ('git ls-files') do (
    set "TRACKED=%%F"
    if /I "!TRACKED!"==".env" goto :tracked_secret
    echo !TRACKED! | findstr /I /R /C:"^data/.*\.db$" /C:"^data/providers\.json$" /C:"^MIGRACION_RENDER/" >nul
    if not errorlevel 1 goto :tracked_secret
)

git add .
if errorlevel 1 (
    echo ERROR: git add fallo.
    pause
    exit /b 1
)

for /f "delims=" %%F in ('git diff --cached --name-only') do (
    set "STAGED=%%F"
    if /I "!STAGED!"==".env" goto :staged_secret
    echo !STAGED! | findstr /I /R /C:"^data/.*\.db$" /C:"^data/providers\.json$" /C:"^MIGRACION_RENDER/" /C:"^RESPALDO_LOCAL_" >nul
    if not errorlevel 1 goto :staged_secret
)

echo Archivos que se publicaran:
git status --short

echo.
set /p "REPO_URL=URL del repositorio [Enter = %DEFAULT_REPO_URL%]: "
if "%REPO_URL%"=="" set "REPO_URL=%DEFAULT_REPO_URL%"

echo %REPO_URL% | findstr /I /R /C:"^https://github.com/.*/.*\.git$" >nul
if errorlevel 1 (
    echo ERROR: la URL no tiene el formato HTTPS esperado de GitHub.
    git reset >nul 2>&1
    pause
    exit /b 1
)

for /f "delims=" %%A in ('git config user.name 2^>nul') do set "GIT_NAME=%%A"
if not defined GIT_NAME (
    set /p "GIT_NAME=Nombre para Git: "
    git config user.name "!GIT_NAME!"
)
for /f "delims=" %%A in ('git config user.email 2^>nul') do set "GIT_EMAIL=%%A"
if not defined GIT_EMAIL (
    set /p "GIT_EMAIL=Correo para Git: "
    git config user.email "!GIT_EMAIL!"
)

git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Preparar Shop Ultra Bot para Render"
    if errorlevel 1 (
        echo ERROR: no se pudo crear el commit.
        pause
        exit /b 1
    )
) else (
    echo No hay archivos nuevos para confirmar; se continuara con el push.
)

git branch -M main
git remote remove origin >nul 2>&1
git remote add origin "%REPO_URL%"
git push -u origin main
if errorlevel 1 (
    echo.
    echo ERROR: GitHub rechazo el push.
    echo Revise el inicio de sesion, la URL y que el repositorio este vacio.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo PUBLICACION COMPLETADA
echo ============================================================
echo El codigo y las carpetas fueron publicados.
echo .env y data\ permanecen solo en su computadora.
pause
exit /b 0

:tracked_secret
echo ERROR CRITICO: Git ya tiene registrado un archivo privado: !TRACKED!
echo No se publicara nada. Retirelo del indice antes de continuar.
pause
exit /b 1

:staged_secret
echo ERROR CRITICO: se intento preparar un archivo privado: !STAGED!
git reset >nul 2>&1
pause
exit /b 1

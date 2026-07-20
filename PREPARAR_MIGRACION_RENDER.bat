@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   SHOP ULTRA - PREPARAR DATA PRIVADA PARA RENDER
echo ============================================================
echo.
echo IMPORTANTE: el bot del que se extraeran los datos debe estar detenido.
echo Puede indicar la carpeta antigua o esta carpeta si primero ejecuto
echo COPIAR_DATOS_DEL_BOT_ANTERIOR.bat.
echo.
set /p "OLD_BOT=Ruta del bot con .env y data [Enter = esta carpeta]: "
if "%OLD_BOT%"=="" set "OLD_BOT=%~dp0"

set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    echo ERROR: Python no esta instalado o no esta en PATH.
    pause
    exit /b 1
)

%PYTHON_CMD% tools\prepare_render_migration.py "%OLD_BOT%"
if errorlevel 1 (
    echo.
    echo La migracion NO se genero. Revise el error anterior.
    pause
    exit /b 1
)

echo.
echo Se abrio MIGRACION_RENDER.
echo NO suba esta carpeta a GitHub.
start "" "%~dp0MIGRACION_RENDER"
pause

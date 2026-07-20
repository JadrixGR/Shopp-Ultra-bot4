@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   SHOP ULTRA - COPIAR DATOS DEL BOT ANTERIOR
echo ============================================================
echo.
echo IMPORTANTE:
echo 1. Detenga primero el bot anterior con Ctrl+C.
echo 2. Cierre su ventana.
echo 3. Compruebe que no quede python.exe o pythonw.exe ejecutandolo.
echo.
set /p "OLD_BOT=Pegue la ruta completa de la carpeta del bot anterior: "
if "%OLD_BOT%"=="" (
    echo ERROR: no ingreso una ruta.
    pause
    exit /b 1
)

set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    echo ERROR: Python no esta instalado o no esta en PATH.
    pause
    exit /b 1
)

%PYTHON_CMD% tools\copy_existing_bot_data.py "%OLD_BOT%"
if errorlevel 1 (
    echo.
    echo ERROR: no se copiaron los datos. Revise el mensaje anterior.
    pause
    exit /b 1
)

echo.
echo Los datos quedaron en esta carpeta para probar el bot localmente.
echo .env y data\ NO se publicaran usando PUBLICAR_EN_GITHUB.bat.
echo.
pause

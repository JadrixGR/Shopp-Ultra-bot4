@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title SHOP ULTRA BOT - En ejecucion

set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo ======================================================
echo              SHOP ULTRA BOT - INICIO
echo ======================================================
echo.

if not exist ".env" (
    echo No existe el archivo .env. Se abrira la configuracion inicial.
    call "configurar.bat"
    if errorlevel 1 goto startup_error
)

if not exist ".venv\Scripts\python.exe" (
    echo No existe el entorno virtual. Se instalaran los requerimientos sin modificar .env ni data.
    call "instalar_requerimientos.bat"
    if errorlevel 1 goto startup_error
)

if not exist "requirements.txt" (
    echo ERROR: no se encontro requirements.txt.
    goto startup_error
)

echo Verificando e instalando requerimientos...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: no se pudieron instalar o verificar los requerimientos.
    echo Revisa tu conexion a Internet.
    goto startup_error
)

echo.
echo Iniciando el bot. No cierres esta ventana mientras quieras mantenerlo activo.
echo Para detenerlo, pulsa Ctrl+C.
echo.
".venv\Scripts\python.exe" -m app.main
set "BOT_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%BOT_EXIT_CODE%"=="0" (
    echo El bot se detuvo.
) else (
    echo El bot termino con un error. Codigo: %BOT_EXIT_CODE%
    echo Revisa los mensajes mostrados arriba y el archivo .env.
)
echo.
pause
exit /b %BOT_EXIT_CODE%

:startup_error
echo.
pause
exit /b 1

@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title SHOP ULTRA BOT - Respaldo seguro

echo ======================================================
echo       RESPALDO SEGURO DE CONFIGURACION Y DATOS
echo ======================================================
echo.
echo Cierra primero la ventana donde esta ejecutandose el bot.
echo El respaldo incluira .env, data\shop.db y data\providers.json.
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\backup_bot_data.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "tools\backup_bot_data.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

where python >nul 2>&1
if not errorlevel 1 (
    python "tools\backup_bot_data.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

echo ERROR: no se encontro Python. Ejecuta configurar.bat primero.
set "EXIT_CODE=1"

:finish
echo.
if "%EXIT_CODE%"=="0" (
    echo Respaldo terminado correctamente.
) else (
    echo No se pudo completar el respaldo.
)
echo.
if /I "%~1"=="nopause" exit /b %EXIT_CODE%
pause
exit /b %EXIT_CODE%

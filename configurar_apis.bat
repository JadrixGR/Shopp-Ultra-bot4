@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title SHOP ULTRA BOT - Configurar varias APIs

echo ======================================================
echo        SHOP ULTRA BOT - PROVEEDORES API
echo ======================================================
echo.

if not exist ".env" (
    echo Primero se abrira la configuracion general.
    call "configurar.bat"
    if errorlevel 1 goto error
)

if not exist ".venv\Scripts\python.exe" (
    echo No existe el entorno virtual. Instalando requerimientos sin modificar .env ni data...
    call "instalar_requerimientos.bat"
    if errorlevel 1 goto error
)

if not exist "tools\configure_apis_windows.py" (
    echo ERROR: falta tools\configure_apis_windows.py.
    goto error
)

".venv\Scripts\python.exe" "tools\configure_apis_windows.py"
if errorlevel 1 goto error

echo.
echo APIs guardadas. Reinicia el bot con iniciar_bot.bat.
echo.
pause
exit /b 0

:error
echo.
echo No se pudo completar la configuracion de APIs.
echo.
pause
exit /b 1

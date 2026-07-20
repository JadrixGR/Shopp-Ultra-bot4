@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title SHOP ULTRA BOT - Verificar datos

echo Cerrando cualquier proceso del bot antes de revisar evita lecturas inconsistentes.
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\inspect_database.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "tools\inspect_database.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

where python >nul 2>&1
if not errorlevel 1 (
    python "tools\inspect_database.py"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

echo ERROR: no se encontro Python. Ejecuta configurar.bat primero.
set "EXIT_CODE=1"

:finish
echo.
pause
exit /b %EXIT_CODE%

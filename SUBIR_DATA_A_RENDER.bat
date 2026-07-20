@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where scp >nul 2>&1
if errorlevel 1 (
    echo ERROR: Windows OpenSSH no esta instalado. Instale "OpenSSH Client".
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0SUBIR_DATA_A_RENDER.ps1"
if errorlevel 1 (
    echo.
    echo La carga no se completo.
    pause
    exit /b 1
)
pause

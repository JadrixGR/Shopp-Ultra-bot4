@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title SHOP ULTRA BOT - Instalar requerimientos

set "PY_LAUNCHER="
set "PY_ARGS="
set "INSTALL_ATTEMPTED="
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo ======================================================
echo       SHOP ULTRA BOT - INSTALAR REQUERIMIENTOS
echo ======================================================
echo.

:detect_python
set "PY_LAUNCHER="
set "PY_ARGS="

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_LAUNCHER=py"
        set "PY_ARGS=-3"
    )
)

if not defined PY_LAUNCHER (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "PY_LAUNCHER=python"
            set "PY_ARGS="
        )
    )
)

if defined PY_LAUNCHER goto python_ready

if defined INSTALL_ATTEMPTED goto no_python
where winget >nul 2>&1
if errorlevel 1 goto no_python

echo No se encontro Python 3.12 o superior.
choice /C SN /N /M "Deseas instalar Python 3.12 con winget? [S/N]: "
if errorlevel 2 goto no_python
set "INSTALL_ATTEMPTED=1"
winget install --exact --id Python.Python.3.12 --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto install_error
goto detect_python

:python_ready
if not exist "requirements.txt" (
    echo ERROR: no se encontro requirements.txt.
    goto fatal_error
)

if not exist ".venv\Scripts\python.exe" (
    echo Creando el entorno virtual .venv...
    "%PY_LAUNCHER%" %PY_ARGS% -m venv ".venv"
    if errorlevel 1 goto venv_error
)

echo Actualizando pip, setuptools y wheel...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto requirements_error

echo Instalando los requerimientos del bot...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto requirements_error

echo.
echo ======================================================
echo Requerimientos instalados correctamente.
echo Tu archivo .env y la carpeta data no fueron modificados.
echo ======================================================
echo.
pause
exit /b 0

:no_python
echo.
echo ERROR: necesitas Python 3.12 o superior.
echo Instala Python desde https://www.python.org/downloads/windows/
echo Durante la instalacion activa la opcion Add Python to PATH.
goto fatal_error

:install_error
echo.
echo ERROR: winget no pudo instalar Python.
goto fatal_error

:venv_error
echo.
echo ERROR: no se pudo crear el entorno virtual .venv.
goto fatal_error

:requirements_error
echo.
echo ERROR: no se pudieron instalar los requerimientos.
echo Revisa tu conexion a Internet y vuelve a intentarlo.
goto fatal_error

:fatal_error
echo.
pause
exit /b 1

@echo off
setlocal
cd /d "%~dp0"
call "configurar_apis.bat"
exit /b %errorlevel%

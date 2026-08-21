@echo off
setlocal

rem Athena Desktop launcher. This file can be opened with a double click.
set "ATHENA_ROOT=%~dp0"
set "PYTHONPATH=%ATHENA_ROOT%src;%PYTHONPATH%"
pushd "%ATHENA_ROOT%" >nul

if exist "%ATHENA_ROOT%.venv\Scripts\pythonw.exe" (
    start "Athena Desktop" "%ATHENA_ROOT%.venv\Scripts\pythonw.exe" -m athena_desktop
    popd >nul
    exit /b 0
)

where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "Athena Desktop" pyw.exe -m athena_desktop
    popd >nul
    exit /b 0
)

where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "Athena Desktop" pythonw.exe -m athena_desktop
    popd >nul
    exit /b 0
)

popd >nul
echo No se ha encontrado Python con soporte para aplicaciones graficas.
echo.
echo Instala Python desde https://www.python.org/downloads/windows/
echo y activa la opcion "Tcl/Tk and IDLE" durante la instalacion.
echo.
pause
exit /b 1

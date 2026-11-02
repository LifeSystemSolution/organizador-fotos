@echo off
title Organizador de Fotos y Videos
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo No se encontro Python.
    echo Instalalo desde https://www.python.org/downloads/ marcando "Add Python to PATH".
    pause
    exit /b 1
)
%PY% "OrganizadorFotos.py"
if errorlevel 1 (
    echo.
    echo El programa se cerro con un error. Revisa el mensaje de arriba.
    pause
)

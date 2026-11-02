@echo off
title Instalar complementos - Organizador de Fotos
cd /d "%~dp0"
echo Instalando Pillow (lee la fecha EXIF de las fotos)
echo y pillow-heif (fotos HEIC del iPhone)...
echo.
python -m pip install --upgrade Pillow pillow-heif
echo.
echo Listo. Ya puedes abrir Ejecutar_OrganizadorFotos.bat
pause

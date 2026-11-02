ORGANIZADOR DE FOTOS Y VIDEOS v2 - WINDOWS

PRIMERA VEZ
1. Necesitas Python 3 (https://www.python.org/downloads/, marca "Add Python to PATH").
2. Ejecuta Instalar_Complementos.bat una vez (instala Pillow y el soporte HEIC del iPhone).

USO
1. Abre Ejecutar_OrganizadorFotos.bat
2. "Agregar carpeta": agrega todas las carpetas donde tengas fotos y videos
   (copias del celular, USB, discos...). Se revisan todas las subcarpetas.
3. Destino: elige dónde crear la carpeta FOTOS_ORDENADAS (mejor en un disco
   con espacio suficiente).
4. Pulsa ANALIZAR. No cambia nada; muestra cuántas fotos y videos hay por año
   y mes, duplicados y archivos sin fecha.
5. Pulsa ORGANIZAR.

CÓMO SE DECIDE LA FECHA (en este orden)
- EXIF de la cámara (fecha real de captura, DateTimeOriginal)
- Metadatos internos del video (MP4/MOV/3GP se leen sin instalar nada;
  para AVI/MKV/etc. hace falta ffprobe de FFmpeg en el PATH)
- Fecha escrita en el nombre (IMG_20210625_184231, IMG-20210625-WA0001,
  Screenshot_2021-06-25..., marcas de tiempo Unix)
- Fecha del archivo (la más antigua entre creación y modificación)

DENTRO DE FOTOS_ORDENADAS
  2024/07 Julio/...    (u otra estructura a elegir)
  _DUPLICADOS          copias exactas (o no se pasan, si eliges esa opción)
  _SIN_FECHA           archivos sin ninguna fecha válida
  _REPORTES            REPORTE_*.csv (qué pasó con cada archivo, de dónde
                       vino y a dónde fue) e IGNORADOS_*.csv

SEGURIDAD
- Por defecto COPIA: los originales no se tocan.
- "Mover" también es seguro: nada se borra, todo queda en el destino y el
  reporte dice de dónde salió cada archivo.
- Puedes ejecutarlo varias veces: lo que ya está en el destino no se repite.
- Se ignoran las carpetas ocultas o de miniaturas (.thumbnails, papelera...)
  y, si lo dejas marcado, las imágenes de menos de 15 KB (stickers).
- No borres tus originales hasta revisar el resultado.

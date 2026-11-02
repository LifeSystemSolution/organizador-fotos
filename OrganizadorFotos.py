"""
ORGANIZADOR DE FOTOS Y VIDEOS  -  v2

- Recorre varias carpetas de origen con todas sus subcarpetas.
- Fecha real de captura: EXIF (DateTimeOriginal), metadatos internos de
  MP4/MOV (sin instalar nada), ffprobe si existe, nombre del archivo y,
  como último recurso, la fecha del archivo.
- Duplicados por contenido (tamaño -> hash parcial -> SHA-256 completo).
- Copia (por defecto) o mueve. NUNCA borra nada.
- Se puede volver a ejecutar: lo que ya está en el destino no se repite.
- Reporte CSV de todo lo que hizo.
"""

import os
import sys
import csv
import json
import time
import shutil
import struct
import hashlib
import threading
import traceback
import queue
import re
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

HAS_HEIF = False
if HAS_PIL:
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        HAS_HEIF = True
    except Exception:
        pass

FFPROBE = shutil.which("ffprobe")

# ============================================================
# CONFIGURACIÓN
# ============================================================

APP_NAME = "Organizador de Fotos y Videos"
DEST_FOLDER_NAME = "FOTOS_ORDENADAS"
SMALL_IMAGE_BYTES = 15 * 1024          # imágenes más chicas = miniaturas / stickers
PARTIAL_CHUNK = 64 * 1024
CONFIG_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "OrganizadorFotos" / "config.json"

IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".dng", ".raw", ".cr2", ".cr3",
    ".nef", ".arw", ".orf", ".rw2",
}
VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".3g2", ".mts",
    ".m2ts", ".webm", ".wmv", ".flv",
}
MP4_FAMILY = {".mp4", ".mov", ".m4v", ".3gp", ".3g2"}
SUPPORTED_EXT = IMAGE_EXT | VIDEO_EXT

# Carpetas que nunca contienen fotos "reales" (cachés de miniaturas, papelera...)
SKIP_DIRS = {"$recycle.bin", "system volume information", "thumbnails", "thumbs", "cache"}

MONTHS = ["01 Enero", "02 Febrero", "03 Marzo", "04 Abril", "05 Mayo", "06 Junio",
          "07 Julio", "08 Agosto", "09 Septiembre", "10 Octubre", "11 Noviembre", "12 Diciembre"]

STRUCTURES = [
    ("year_month", "Año / Mes   (2024 \u203a 07 Julio)"),
    ("month", "Año-Mes   (2024-07)"),
    ("year_month_day", "Año / Mes / Día   (2024 \u203a 07 Julio \u203a 2024-07-15)"),
    ("year", "Solo año   (2024)"),
]

DATE_METHODS = {
    "EXIF": "EXIF de la cámara",
    "video": "Metadatos del video",
    "nombre": "Nombre del archivo",
    "fecha_archivo": "Fecha del archivo",
}
# Para elegir el "original" entre duplicados: mejor fecha primero
METHOD_RANK = {"EXIF": 0, "video": 0, "nombre": 1, "fecha_archivo": 2, None: 3}

MIN_DATE = datetime(1990, 1, 1)

DATE_PATTERNS = [re.compile(p) for p in (
    # IMG_20210625_184231 / VID-20210625-184231
    r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})(?!\d)",
    # 2021-06-25 18-42-31 / 2021-06-25_18.42.31
    r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})[ T_-](\d{2})[-_.:](\d{2})[-_.:](\d{2})(?!\d)",
    # IMG-20210625-WA0001
    r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)",
    # 2021-06-25
    r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})(?!\d)",
)]
# Marca de tiempo Unix en segundos o milisegundos (1625000000 / 1625000000123)
TIMESTAMP_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d{8})(\d{3})?(?!\d)")


# ============================================================
# UTILIDADES
# ============================================================

class Stopped(Exception):
    pass


class Item:
    __slots__ = ("path", "size", "kind", "date", "source", "duplicate_of", "dest", "status")

    def __init__(self, path, size, kind):
        self.path = path
        self.size = size
        self.kind = kind            # "foto" | "video"
        self.date = None
        self.source = None
        self.duplicate_of = None
        self.dest = None
        self.status = ""


def is_valid_date(dt):
    return dt is not None and MIN_DATE <= dt <= datetime.now() + timedelta(days=1)


def is_within(path, parent):
    a = os.path.normcase(os.path.abspath(str(path)))
    b = os.path.normcase(os.path.abspath(str(parent))).rstrip("\\/")
    return a == b or a.startswith(b + os.sep)


def safe_name(name):
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip() or "archivo"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(sec):
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def eta_text(t0, done, total):
    elapsed = time.time() - t0
    if done <= 0 or elapsed < 2 or done >= total:
        return ""
    return "Faltan ~" + fmt_duration(elapsed / done * (total - done))


# ============================================================
# FECHAS
# ============================================================

def _parse_exif_value(value):
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not isinstance(value, str):
        return None
    value = value.strip("\x00 ").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            dt = datetime.strptime(value[:19] if "%H" in fmt else value[:10], fmt)
            return dt if is_valid_date(dt) else None
        except ValueError:
            continue
    return None


def exif_date(path):
    if not HAS_PIL:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            # DateTimeOriginal y DateTimeDigitized viven en el sub-IFD Exif (0x8769);
            # DateTime (306) del IFD principal suele ser la fecha de edición.
            sub = exif.get_ifd(0x8769)
            for tag, ifd in ((36867, sub), (36868, sub), (306, exif)):
                dt = _parse_exif_value(ifd.get(tag))
                if dt:
                    return dt, "EXIF"
    except Exception:
        pass
    return None


def _find_box(f, start, end, name):
    """Busca una caja (atom) MP4/MOV entre start y end. Devuelve (inicio_datos, fin)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return None
        size, box_type = struct.unpack(">I4s", header)
        header_len = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return None
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            return None
        if box_type == name:
            return pos + header_len, min(pos + size, end)
        pos += size
    return None


MP4_EPOCH_OFFSET = 2082844800  # segundos entre 1904-01-01 y 1970-01-01


def mp4_date(path):
    """Lee la fecha de creación (moov/mvhd) de MP4/MOV/3GP sin librerías externas."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            moov = _find_box(f, 0, end, b"moov")
            if not moov:
                return None
            mvhd = _find_box(f, moov[0], moov[1], b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version = f.read(4)[0]
            if version == 1:
                ts = struct.unpack(">Q", f.read(8))[0]
            else:
                ts = struct.unpack(">I", f.read(4))[0]
            if ts <= MP4_EPOCH_OFFSET:
                return None
            # El valor está en UTC: fromtimestamp lo pasa a hora local.
            dt = datetime.fromtimestamp(ts - MP4_EPOCH_OFFSET)
            return (dt, "video") if is_valid_date(dt) else None
    except Exception:
        return None


def ffprobe_date(path):
    if not FFPROBE:
        return None
    cmd = [FFPROBE, "-v", "quiet", "-show_entries", "format_tags=creation_time",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""
        if not value:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return (dt, "video") if is_valid_date(dt) else None
    except Exception:
        return None


def parse_filename_date(path):
    name = path.stem
    for pattern in DATE_PATTERNS:
        for m in pattern.finditer(name):
            try:
                dt = datetime(*(int(x) for x in m.groups()))
            except ValueError:
                continue
            if is_valid_date(dt):
                return dt, "nombre"
    m = TIMESTAMP_PATTERN.search(name)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)))
            if is_valid_date(dt):
                return dt, "nombre"
        except (OverflowError, OSError, ValueError):
            pass
    return None


def file_date(path):
    try:
        st = path.stat()
        # En Windows st_ctime es la fecha de creación; al copiar archivos suele
        # conservarse la de modificación, así que se usa la más antigua.
        dt = datetime.fromtimestamp(min(st.st_mtime, st.st_ctime))
        return (dt, "fecha_archivo") if is_valid_date(dt) else None
    except Exception:
        return None


def get_date(path, use_meta=True):
    ext = path.suffix.lower()
    if use_meta:
        found = None
        if ext in IMAGE_EXT:
            found = exif_date(path)
        elif ext in MP4_FAMILY:
            found = mp4_date(path) or ffprobe_date(path)
        elif ext in VIDEO_EXT:
            found = ffprobe_date(path)
        if found:
            return found
    return parse_filename_date(path) or file_date(path) or (None, None)


# ============================================================
# DUPLICADOS
# ============================================================

_hash_cache = {}

COPY_NAME = re.compile(r"(?i)(copia|copy|\(\d+\)$|[ _-]\d{1,3}$|~\d+$)")


def looks_like_copy(path):
    """Para elegir el original entre duplicados: 'foto (1).jpg' o 'copia.jpg' van después."""
    return bool(COPY_NAME.search(path.stem))


def partial_hash(path, size):
    """Hash del inicio y del final. Si el archivo es chico, cubre el archivo entero."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        if size <= 2 * PARTIAL_CHUNK:
            h.update(f.read())
        else:
            h.update(f.read(PARTIAL_CHUNK))
            f.seek(-PARTIAL_CHUNK, 2)
            h.update(f.read(PARTIAL_CHUNK))
    return h.hexdigest()


def full_hash(path):
    key = os.path.normcase(str(path))
    if key in _hash_cache:
        return _hash_cache[key]
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def same_content(a, b):
    try:
        size = a.stat().st_size
        if size != b.stat().st_size:
            return False
        if size <= 2 * PARTIAL_CHUNK:
            return partial_hash(a, size) == partial_hash(b, size)
        return partial_hash(a, size) == partial_hash(b, size) and full_hash(a) == full_hash(b)
    except OSError:
        return False


def mark_duplicates(items, stop, emit):
    by_size = defaultdict(list)
    for it in items:
        by_size[it.size].append(it)
    groups = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in groups)
    done = 0
    t0 = time.time()

    for group in groups:
        by_partial = defaultdict(list)
        for it in group:
            if stop.is_set():
                raise Stopped
            try:
                by_partial[partial_hash(it.path, it.size)].append(it)
            except OSError:
                pass
            done += 1
            if done % 20 == 0 or done == total:
                emit(("progress", done, total, f"Buscando duplicados  {done:,} / {total:,}",
                      eta_text(t0, done, total)))

        for same in by_partial.values():
            if len(same) < 2:
                continue
            if same[0].size <= 2 * PARTIAL_CHUNK:
                clusters = [same]
            else:
                by_full = defaultdict(list)
                for it in same:
                    if stop.is_set():
                        raise Stopped
                    try:
                        by_full[full_hash(it.path)].append(it)
                    except OSError:
                        pass
                clusters = by_full.values()
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                cluster.sort(key=lambda x: (METHOD_RANK.get(x.source, 3), looks_like_copy(x.path),
                                            len(str(x.path)), str(x.path)))
                for dup in cluster[1:]:
                    dup.duplicate_of = cluster[0].path


# ============================================================
# ANÁLISIS
# ============================================================

def analyze(sources, root, opts, stop, emit):
    _hash_cache.clear()
    t_start = time.time()
    items, ignored = [], []
    min_image = SMALL_IMAGE_BYTES if opts["ignore_small"] else 1

    emit(("indeterminate", "Buscando archivos…"))
    last = 0
    for src in sources:
        for dirpath, dirnames, filenames in os.walk(src):
            if stop.is_set():
                raise Stopped
            dp = Path(dirpath)
            kept = []
            for d in dirnames:
                full = dp / d
                if d.startswith(".") or d.lower() in SKIP_DIRS:
                    ignored.append((full, "carpeta oculta, de miniaturas o del sistema"))
                elif is_within(full, root):
                    continue  # nunca escanear el propio destino
                else:
                    kept.append(d)
            dirnames[:] = kept

            for name in filenames:
                p = dp / name
                ext = p.suffix.lower()
                if ext not in SUPPORTED_EXT:
                    continue
                kind = "foto" if ext in IMAGE_EXT else "video"
                try:
                    size = p.stat().st_size
                except OSError as e:
                    ignored.append((p, f"no se pudo leer ({e})"))
                    continue
                if size == 0:
                    ignored.append((p, "archivo vacío (0 bytes)"))
                elif kind == "foto" and size < min_image:
                    ignored.append((p, "miniatura o imagen muy pequeña"))
                else:
                    items.append(Item(p, size, kind))
            if len(items) - last >= 250:
                last = len(items)
                emit(("status", f"Buscando archivos…  {len(items):,} encontrados"))

    total = len(items)
    emit(("log", f"Archivos encontrados: {total:,}  ({human_size(sum(i.size for i in items))})", "info"))

    t0 = time.time()
    for i, it in enumerate(items, 1):
        if stop.is_set():
            raise Stopped
        it.date, it.source = get_date(it.path, opts["use_meta"])
        if i % 25 == 0 or i == total:
            emit(("progress", i, total, f"Leyendo fechas  {i:,} / {total:,}", eta_text(t0, i, total)))

    mark_duplicates(items, stop, emit)

    return {
        "items": items,
        "ignored": ignored,
        "elapsed": time.time() - t_start,
    }


def summarize(items):
    st = {"total": len(items), "fotos": 0, "videos": 0, "dups": 0, "nodate": 0,
          "bytes": 0, "dup_bytes": 0, "methods": Counter(), "by_month": defaultdict(lambda: [0, 0])}
    for it in items:
        st["bytes"] += it.size
        st["fotos" if it.kind == "foto" else "videos"] += 1
        if it.duplicate_of:
            st["dups"] += 1
            st["dup_bytes"] += it.size
            continue
        st["methods"][it.source] += 1
        if not it.date:
            st["nodate"] += 1
        key = (it.date.year, it.date.month) if it.date else None
        st["by_month"][key][0 if it.kind == "foto" else 1] += 1
    return st


# ============================================================
# ORGANIZACIÓN
# ============================================================

def target_folder(root, it, opts):
    if it.duplicate_of:
        return root / "_DUPLICADOS"
    if not it.date:
        folder = root / "_SIN_FECHA"
    else:
        d = it.date
        s = opts["structure"]
        if s == "month":
            folder = root / f"{d:%Y-%m}"
        elif s == "year":
            folder = root / f"{d:%Y}"
        elif s == "year_month_day":
            folder = root / f"{d:%Y}" / MONTHS[d.month - 1] / f"{d:%Y-%m-%d}"
        else:
            folder = root / f"{d:%Y}" / MONTHS[d.month - 1]
    if opts["split_kind"]:
        folder = folder / ("Fotos" if it.kind == "foto" else "Videos")
    return folder


def target_name(it, opts):
    if opts["rename"] and it.date and not it.duplicate_of:
        return f"{it.date:%Y-%m-%d_%H-%M-%S}{it.path.suffix.lower()}"
    return safe_name(it.path.name)


def place(src, folder, name, move):
    """Copia/mueve sin pisar nada. Si ya existe un archivo idéntico, no lo repite."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(name)
    n = 0
    while True:
        cand = folder / (name if n == 0 else f"{stem}_{n:03d}{suffix}")
        if not cand.exists():
            break
        if same_content(src, cand):
            return cand, "ya estaba en el destino"
        n += 1

    if move:
        shutil.move(str(src), str(cand))
        return cand, "movido"

    # Copia a un temporal y renombra: si se corta, no queda un archivo a medias.
    tmp = cand.with_name(cand.name + ".partial")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, cand)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return cand, "copiado"


def organize(analysis, root, opts, stop, emit):
    items = sorted(analysis["items"], key=lambda x: (x.date is None, x.date or datetime.min))
    skip_dups = opts["dup_mode"] == "skip"
    total_bytes = sum(it.size for it in items if not (it.duplicate_of and skip_dups)) or 1
    done_bytes = 0
    counts = Counter()
    errors_logged = 0
    t0 = time.time()
    verb = "Moviendo" if opts["move"] else "Copiando"

    for i, it in enumerate(items, 1):
        if stop.is_set():
            break
        if it.duplicate_of and skip_dups:
            it.status = "omitido (duplicado)"
            counts[it.status] += 1
            continue
        try:
            it.dest, it.status = place(it.path, target_folder(root, it, opts), target_name(it, opts), opts["move"])
        except Exception as e:
            it.status = f"error: {e}"
            counts["error"] += 1
            if errors_logged < 100:
                emit(("log", f"Error con {it.path}: {e}", "err"))
                errors_logged += 1
        else:
            counts[it.status] += 1
        done_bytes += it.size
        if i % 10 == 0 or i == len(items):
            emit(("progress", done_bytes, total_bytes,
                  f"{verb}  {i:,} / {len(items):,}   ·   {human_size(done_bytes)} de {human_size(total_bytes)}",
                  eta_text(t0, done_bytes, total_bytes)))

    report = write_reports(root, items, analysis["ignored"])
    return {"counts": counts, "report": report, "stopped": stop.is_set(), "elapsed": time.time() - t0}


def write_reports(root, items, ignored):
    folder = root / "_REPORTES"
    folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report = folder / f"REPORTE_{ts}.csv"
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["estado", "origen", "destino", "fecha_detectada", "metodo_fecha",
                    "tipo", "tamano_MB", "duplicado_de"])
        for it in items:
            w.writerow([
                it.status or "pendiente",
                str(it.path),
                str(it.dest or ""),
                it.date.strftime("%Y-%m-%d %H:%M:%S") if it.date else "",
                DATE_METHODS.get(it.source, "sin fecha"),
                it.kind,
                f"{it.size / 1048576:.2f}",
                str(it.duplicate_of or ""),
            ])
    if ignored:
        with (folder / f"IGNORADOS_{ts}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["ruta", "motivo"])
            for p, why in ignored:
                w.writerow([str(p), why])
    return report


# ============================================================
# INTERFAZ
# ============================================================

C = {
    "bg": "#F3F5F9",
    "card": "#FFFFFF",
    "border": "#E3E8EF",
    "text": "#0F172A",
    "muted": "#64748B",
    "header": "#111827",
    "header_sub": "#9CA3AF",
    "primary": "#2563EB",
    "primary_h": "#1D4ED8",
    "success": "#16A34A",
    "success_h": "#15803D",
    "danger": "#DC2626",
    "danger_h": "#B91C1C",
    "warn": "#D97706",
    "violet": "#7C3AED",
    "field": "#F8FAFC",
    "soft": "#EEF2F7",
}
FONT = "Segoe UI"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.configure(bg=C["bg"])
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1220, sw - 40), min(870, sh - 80)
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 2 - 20, 0)}")
        self.root.minsize(1080, 760)

        self.q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.analysis = None
        self.analysis_key = None
        self.last_report = None
        self.closing = False

        self.dest_var = tk.StringVar()
        self.structure_var = tk.StringVar(value=STRUCTURES[0][1])
        self.action_var = tk.StringVar(value="copy")
        self.dup_var = tk.StringVar(value="separate")
        self.split_var = tk.BooleanVar(value=False)
        self.rename_var = tk.BooleanVar(value=False)
        self.meta_var = tk.BooleanVar(value=True)
        self.small_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Agrega tus carpetas de fotos y elige un destino.")
        self.eta_var = tk.StringVar()
        self.preview_var = tk.StringVar()

        self.setup_style()
        self.build_ui()
        self.load_config()
        for v in (self.structure_var, self.split_var, self.rename_var):
            v.trace_add("write", lambda *_: self.update_preview())
        self.update_preview()
        self.set_busy(False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_id = self.root.after(100, self.poll_queue)

        if not HAS_PIL:
            self.log("Pillow no está instalado: no se podrá leer el EXIF de las fotos. "
                     "Ejecuta Instalar_Complementos.bat para instalarlo.", "warn")

    # ---------- estilo ----------
    def setup_style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", font=(FONT, 10), background=C["card"], foreground=C["text"])
        for name, bg, hover in (("Primary", C["primary"], C["primary_h"]),
                                ("Success", C["success"], C["success_h"]),
                                ("Danger", C["danger"], C["danger_h"])):
            st.configure(f"{name}.TButton", background=bg, foreground="white", borderwidth=0,
                         focusthickness=0, padding=(18, 9), font=(FONT, 10, "bold"))
            st.map(f"{name}.TButton",
                   background=[("disabled", "#CBD5E1"), ("pressed", hover), ("active", hover)],
                   foreground=[("disabled", "#F8FAFC")])
        st.configure("TButton", background=C["soft"], foreground=C["text"], borderwidth=0,
                     focusthickness=0, padding=(12, 7))
        st.map("TButton", background=[("disabled", C["soft"]), ("active", "#E2E8F0")],
               foreground=[("disabled", "#A0AEC0")])
        st.configure("TCheckbutton", background=C["card"], padding=(0, 3))
        st.map("TCheckbutton", background=[("active", C["card"])])
        st.configure("TRadiobutton", background=C["card"], padding=(0, 3))
        st.map("TRadiobutton", background=[("active", C["card"])])
        st.configure("TEntry", fieldbackground=C["field"], bordercolor=C["border"],
                     lightcolor=C["border"], darkcolor=C["border"], padding=6)
        st.configure("TCombobox", fieldbackground=C["field"], background=C["field"],
                     bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"],
                     arrowcolor=C["muted"], padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", C["field"])])
        st.configure("Horizontal.TProgressbar", troughcolor=C["soft"], background=C["primary"],
                     bordercolor=C["soft"], lightcolor=C["primary"], darkcolor=C["primary"], thickness=10)
        st.configure("Treeview", rowheight=28, background=C["card"], fieldbackground=C["card"],
                     borderwidth=0, font=(FONT, 10))
        st.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", C["text"])])
        st.configure("Treeview.Heading", background=C["field"], foreground=C["muted"],
                     font=(FONT, 9, "bold"), borderwidth=0, relief="flat", padding=6)
        st.map("Treeview.Heading", background=[("active", C["soft"])])
        st.configure("TNotebook", background=C["card"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=C["card"], foreground=C["muted"],
                     padding=(16, 8), borderwidth=0, font=(FONT, 10))
        st.map("TNotebook.Tab", background=[("selected", C["card"])],
               foreground=[("selected", C["primary"])], font=[("selected", (FONT, 10, "bold"))])
        st.configure("Vertical.TScrollbar", background=C["soft"], troughcolor=C["card"],
                     bordercolor=C["card"], arrowcolor=C["muted"], gripcount=0)

    # ---------- construcción ----------
    def card(self, parent, title, subtitle=None, step=None):
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
        head = tk.Frame(outer, bg=C["card"])
        head.pack(fill="x", padx=18, pady=(16, 8))
        if step:
            tk.Label(head, text=step, font=(FONT, 10, "bold"), bg=C["primary"], fg="white",
                     width=2).pack(side="left", padx=(0, 10))
        titles = tk.Frame(head, bg=C["card"])
        titles.pack(side="left", fill="x")
        tk.Label(titles, text=title, font=(FONT, 12, "bold"), bg=C["card"], fg=C["text"]).pack(anchor="w")
        if subtitle:
            tk.Label(titles, text=subtitle, font=(FONT, 9), bg=C["card"], fg=C["muted"]).pack(anchor="w")
        body = tk.Frame(outer, bg=C["card"])
        body.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        return outer, body

    def label(self, parent, text, muted=False, bold=False, size=10):
        return tk.Label(parent, text=text, bg=C["card"], fg=C["muted"] if muted else C["text"],
                        font=(FONT, size, "bold" if bold else "normal"), anchor="w", justify="left")

    def badge(self, parent, text, ok):
        color = "#34D399" if ok else "#F59E0B"
        tk.Label(parent, text=("\u2713 " if ok else "\u2715 ") + text, font=(FONT, 9, "bold"),
                 bg="#1F2937", fg=color, padx=10, pady=3).pack(side="left", padx=(8, 0))

    def build_ui(self):
        # Encabezado
        header = tk.Frame(self.root, bg=C["header"])
        header.pack(fill="x")
        hl = tk.Frame(header, bg=C["header"])
        hl.pack(side="left", padx=24, pady=16)
        tk.Label(hl, text="\U0001F4F7  " + APP_NAME, font=(FONT, 17, "bold"),
                 bg=C["header"], fg="white").pack(anchor="w")
        tk.Label(hl, text="Ordena por la fecha real de captura  ·  detecta duplicados  ·  nunca borra nada",
                 font=(FONT, 10), bg=C["header"], fg=C["header_sub"]).pack(anchor="w")
        hr = tk.Frame(header, bg=C["header"])
        hr.pack(side="right", padx=24)
        self.badge(hr, "EXIF", HAS_PIL)
        self.badge(hr, "HEIC", HAS_HEIF)
        self.badge(hr, "MP4 / MOV", True)
        self.badge(hr, "ffprobe", bool(FFPROBE))

        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=20, pady=18)
        main.columnconfigure(0, minsize=430, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left = tk.Frame(main, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right = tk.Frame(main, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")

        self.build_folders(left)
        self.build_options(left)
        self.build_actions(right)
        self.build_stats(right)
        self.build_results(right)

    def build_folders(self, parent):
        outer, body = self.card(parent, "Carpetas", "Puedes agregar varias: celular, USB, discos…", "1")
        outer.pack(fill="x")

        self.label(body, "Origen", bold=True, size=9).pack(anchor="w")
        lb_frame = tk.Frame(body, bg=C["field"], highlightthickness=1, highlightbackground=C["border"])
        lb_frame.pack(fill="x", pady=(4, 6))
        self.src_list = tk.Listbox(lb_frame, height=3, bg=C["field"], fg=C["text"], relief="flat",
                                   borderwidth=0, highlightthickness=0, font=(FONT, 9),
                                   selectbackground="#DBEAFE", selectforeground=C["text"],
                                   activestyle="none")
        self.src_list.pack(side="left", fill="both", expand=True, padx=6, pady=4)
        sb = ttk.Scrollbar(lb_frame, orient="vertical", command=self.src_list.yview)
        sb.pack(side="right", fill="y")
        self.src_list.configure(yscrollcommand=sb.set)

        row = tk.Frame(body, bg=C["card"])
        row.pack(fill="x")
        self.add_btn = ttk.Button(row, text="+  Agregar carpeta", command=self.add_source)
        self.add_btn.pack(side="left")
        self.remove_btn = ttk.Button(row, text="Quitar", command=self.remove_source)
        self.remove_btn.pack(side="left", padx=6)

        self.label(body, "Destino", bold=True, size=9).pack(anchor="w", pady=(14, 0))
        row2 = tk.Frame(body, bg=C["card"])
        row2.pack(fill="x", pady=(4, 0))
        self.dest_entry = ttk.Entry(row2, textvariable=self.dest_var)
        self.dest_entry.pack(side="left", fill="x", expand=True)
        self.dest_btn = ttk.Button(row2, text="Examinar…", command=self.select_dest)
        self.dest_btn.pack(side="left", padx=(6, 0))
        self.label(body, f"Se creará la carpeta {DEST_FOLDER_NAME} dentro.", muted=True, size=9).pack(anchor="w", pady=(4, 0))

    def build_options(self, parent):
        outer, body = self.card(parent, "Opciones", "Los valores por defecto sirven para la mayoría", "2")
        outer.pack(fill="both", expand=True, pady=(14, 0))
        body.columnconfigure(1, weight=1)

        self.label(body, "Carpetas", bold=True, size=9).grid(row=0, column=0, sticky="w", pady=4)
        self.structure_cb = ttk.Combobox(body, textvariable=self.structure_var, state="readonly",
                                         values=[s[1] for s in STRUCTURES], font=(FONT, 9))
        self.structure_cb.grid(row=0, column=1, sticky="ew", pady=4, padx=(12, 0))

        self.label(body, "Acción", bold=True, size=9).grid(row=1, column=0, sticky="nw", pady=(8, 0))
        f1 = tk.Frame(body, bg=C["card"])
        f1.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        self.copy_rb = ttk.Radiobutton(f1, text="Copiar  (recomendado)", value="copy", variable=self.action_var)
        self.copy_rb.pack(side="left")
        self.move_rb = ttk.Radiobutton(f1, text="Mover", value="move", variable=self.action_var)
        self.move_rb.pack(side="left", padx=(14, 0))

        self.label(body, "Duplicados", bold=True, size=9).grid(row=2, column=0, sticky="nw", pady=(4, 0))
        f2 = tk.Frame(body, bg=C["card"])
        f2.grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        self.dsep_rb = ttk.Radiobutton(f2, text="Separar en _DUPLICADOS", value="separate", variable=self.dup_var)
        self.dsep_rb.pack(side="left")
        self.dskip_rb = ttk.Radiobutton(f2, text="No pasarlos", value="skip", variable=self.dup_var)
        self.dskip_rb.pack(side="left", padx=(14, 0))

        checks = tk.Frame(body, bg=C["card"])
        checks.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.checks = [
            ttk.Checkbutton(checks, text="Separar Fotos y Videos dentro de cada carpeta", variable=self.split_var),
            ttk.Checkbutton(checks, text="Renombrar con la fecha  (2024-07-15_18-42-31.jpg)", variable=self.rename_var),
            ttk.Checkbutton(checks, text="Usar la fecha interna (EXIF / metadatos del video)", variable=self.meta_var),
            ttk.Checkbutton(checks, text="Ignorar miniaturas y stickers (imágenes de menos de 15 KB)", variable=self.small_var),
        ]
        for c in self.checks:
            c.pack(anchor="w")

        prev = tk.Frame(body, bg=C["field"], highlightthickness=1, highlightbackground=C["border"])
        prev.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        tk.Label(prev, text="VISTA PREVIA", font=(FONT, 8, "bold"), bg=C["field"],
                 fg=C["muted"]).pack(anchor="w", padx=10, pady=(8, 0))
        tk.Label(prev, textvariable=self.preview_var, font=("Consolas", 9), bg=C["field"],
                 fg=C["primary"], wraplength=370, justify="left").pack(anchor="w", padx=10, pady=(2, 8))

    def build_actions(self, parent):
        outer, body = self.card(parent, "Ordenar", "Primero analiza (no cambia nada), revisa y luego organiza", "3")
        outer.pack(fill="x")
        row = tk.Frame(body, bg=C["card"])
        row.pack(fill="x")
        self.analyze_btn = ttk.Button(row, text="\U0001F50D  Analizar", style="Primary.TButton", command=self.start_analyze)
        self.analyze_btn.pack(side="left")
        self.organize_btn = ttk.Button(row, text="\u2714  Organizar", style="Success.TButton", command=self.start_organize)
        self.organize_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(row, text="\u25A0  Detener", style="Danger.TButton", command=self.stop)
        self.stop_btn.pack(side="left")
        self.report_btn = ttk.Button(row, text="Abrir reporte", command=self.open_report)
        self.report_btn.pack(side="right")
        self.open_btn = ttk.Button(row, text="Abrir destino", command=self.open_dest)
        self.open_btn.pack(side="right", padx=6)

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(16, 6))
        srow = tk.Frame(body, bg=C["card"])
        srow.pack(fill="x")
        tk.Label(srow, textvariable=self.status_var, bg=C["card"], fg=C["text"], font=(FONT, 9),
                 anchor="w").pack(side="left")
        tk.Label(srow, textvariable=self.eta_var, bg=C["card"], fg=C["muted"], font=(FONT, 9),
                 anchor="e").pack(side="right")

    def build_stats(self, parent):
        grid = tk.Frame(parent, bg=C["bg"])
        grid.pack(fill="x", pady=14)
        self.tiles = {}
        specs = [("total", "Archivos", C["primary"]), ("fotos", "Fotos", "#0EA5E9"),
                 ("videos", "Videos", C["violet"]), ("dups", "Duplicados", C["warn"]),
                 ("nodate", "Sin fecha", C["danger"]), ("bytes", "Tamaño total", C["success"])]
        for i, (key, text, color) in enumerate(specs):
            grid.columnconfigure(i, weight=1, uniform="tiles")
            f = tk.Frame(grid, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            tk.Frame(f, bg=color, height=3).pack(fill="x")
            v = tk.StringVar(value="—")
            tk.Label(f, textvariable=v, font=(FONT, 15, "bold"), bg=C["card"], fg=C["text"]).pack(anchor="w", padx=14, pady=(10, 0))
            tk.Label(f, text=text, font=(FONT, 9), bg=C["card"], fg=C["muted"]).pack(anchor="w", padx=14, pady=(0, 10))
            self.tiles[key] = v

    def build_results(self, parent):
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
        outer.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, padx=8, pady=6)

        tab1 = tk.Frame(self.nb, bg=C["card"])
        self.tree = ttk.Treeview(tab1, columns=("fotos", "videos", "total"), show="tree headings")
        self.tree.heading("#0", text="FECHA", anchor="w")
        for col, txt in (("fotos", "FOTOS"), ("videos", "VIDEOS"), ("total", "TOTAL")):
            self.tree.heading(col, text=txt, anchor="e")
            self.tree.column(col, width=110, anchor="e", stretch=False)
        self.tree.column("#0", width=260)
        self.tree.tag_configure("year", font=(FONT, 10, "bold"))
        self.tree.tag_configure("special", foreground=C["warn"], font=(FONT, 10, "bold"))
        tsb = ttk.Scrollbar(tab1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=6)
        tsb.pack(side="right", fill="y", pady=6)
        self.empty_lbl = tk.Label(tab1, text="Pulsa  Analizar  para ver aquí cuántas fotos y videos hay por año y mes.",
                                  bg=C["card"], fg=C["muted"], font=(FONT, 10))
        self.empty_lbl.place(relx=0.5, rely=0.45, anchor="center")
        self.nb.add(tab1, text="Resumen por fecha")

        tab2 = tk.Frame(self.nb, bg=C["card"])
        self.text = tk.Text(tab2, font=("Consolas", 9), bg=C["field"], fg=C["text"], relief="flat",
                            borderwidth=0, highlightthickness=0, padx=10, pady=8, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(tab2, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=lsb.set)
        self.text.pack(side="left", fill="both", expand=True, pady=6)
        lsb.pack(side="right", fill="y", pady=6)
        self.text.tag_configure("title", font=("Consolas", 10, "bold"), foreground=C["primary"])
        self.text.tag_configure("ok", foreground=C["success"])
        self.text.tag_configure("warn", foreground=C["warn"])
        self.text.tag_configure("err", foreground=C["danger"])
        self.text.tag_configure("info", foreground=C["text"])
        self.nb.add(tab2, text="Registro")

    # ---------- utilidades de UI ----------
    def log(self, s, tag="info"):
        self.text.configure(state="normal")
        self.text.insert("end", s + "\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        idle = "disabled" if busy else "!disabled"
        for w in (self.analyze_btn, self.add_btn, self.remove_btn, self.dest_btn, self.dest_entry,
                  self.copy_rb, self.move_rb, self.dsep_rb, self.dskip_rb, *self.checks):
            w.state([idle])
        self.structure_cb.state(["disabled"] if busy else ["!disabled", "readonly"])
        self.organize_btn.state(["!disabled"] if (not busy and self.analysis) else ["disabled"])
        self.stop_btn.state(["!disabled"] if busy else ["disabled"])
        self.report_btn.state(["!disabled"] if (not busy and self.last_report) else ["disabled"])

    def collect_opts(self):
        label = self.structure_var.get()
        structure = next((k for k, v in STRUCTURES if v == label), "year_month")
        return {
            "structure": structure,
            "move": self.action_var.get() == "move",
            "dup_mode": self.dup_var.get(),
            "split_kind": self.split_var.get(),
            "rename": self.rename_var.get(),
            "use_meta": self.meta_var.get(),
            "ignore_small": self.small_var.get(),
        }

    def update_preview(self):
        opts = self.collect_opts()
        it = Item(Path("IMG_20240715_184231.jpg"), 0, "foto")
        it.date = datetime(2024, 7, 15, 18, 42, 31)
        p = target_folder(Path(DEST_FOLDER_NAME), it, opts) / target_name(it, opts)
        self.preview_var.set("  \u203a  ".join(p.parts))

    def sources(self):
        return list(self.src_list.get(0, "end"))

    def add_source(self):
        p = filedialog.askdirectory(title="Selecciona una carpeta con fotos y videos")
        if not p:
            return
        p = os.path.normpath(p)
        if p in self.sources():
            return
        self.src_list.insert("end", p)
        self.invalidate_analysis()

    def remove_source(self):
        for i in reversed(self.src_list.curselection()):
            self.src_list.delete(i)
        self.invalidate_analysis()

    def select_dest(self):
        p = filedialog.askdirectory(title=f"Selecciona dónde crear la carpeta {DEST_FOLDER_NAME}")
        if p:
            self.dest_var.set(os.path.normpath(p))
            self.update_preview()

    def dest_root(self):
        base = Path(self.dest_var.get().strip())
        return base if base.name.upper() == DEST_FOLDER_NAME else base / DEST_FOLDER_NAME

    def open_dest(self):
        if not self.dest_var.get().strip():
            messagebox.showinfo(APP_NAME, "Primero elige una carpeta de destino.")
            return
        root = self.dest_root()
        target = root if root.is_dir() else root.parent
        if target.is_dir():
            os.startfile(target)
        else:
            messagebox.showinfo(APP_NAME, "La carpeta de destino todavía no existe.")

    def open_report(self):
        if self.last_report and Path(self.last_report).exists():
            os.startfile(self.last_report)

    def invalidate_analysis(self):
        if self.analysis and not self.busy:
            self.analysis = None
            self.organize_btn.state(["disabled"])
            self.status_var.set("Cambiaste las carpetas u opciones: vuelve a analizar.")

    def validate(self):
        srcs = self.sources()
        if not srcs:
            messagebox.showerror(APP_NAME, "Agrega al menos una carpeta de origen.")
            return None
        missing = [s for s in srcs if not os.path.isdir(s)]
        if missing:
            messagebox.showerror(APP_NAME, "Estas carpetas no existen:\n\n" + "\n".join(missing))
            return None
        if not self.dest_var.get().strip():
            messagebox.showerror(APP_NAME, "Elige una carpeta de destino.")
            return None
        base = Path(self.dest_var.get().strip())
        if not base.is_absolute() or not Path(base.anchor).exists():
            messagebox.showerror(APP_NAME, "La carpeta de destino no es válida.")
            return None
        root = Path(os.path.abspath(self.dest_root()))
        resolved = list({os.path.normcase(os.path.abspath(s)): Path(os.path.abspath(s)) for s in srcs}.values())
        for s in resolved:
            if is_within(s, root):
                messagebox.showerror(APP_NAME, f"El origen\n{s}\nestá dentro del destino. Elige otro destino.")
                return None
        # Si una carpeta está dentro de otra de la lista, basta con la de arriba.
        unique = [s for s in resolved if not any(o != s and is_within(s, o) for o in resolved)]
        return unique, root

    def save_config(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {"sources": self.sources(), "dest": self.dest_var.get(), **self.collect_opts()}
            CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_config(self):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for s in data.get("sources", []):
            self.src_list.insert("end", s)
        self.dest_var.set(data.get("dest", ""))
        self.structure_var.set(dict(STRUCTURES).get(data.get("structure"), STRUCTURES[0][1]))
        self.action_var.set("move" if data.get("move") else "copy")
        self.dup_var.set(data.get("dup_mode", "separate"))
        self.split_var.set(bool(data.get("split_kind", False)))
        self.rename_var.set(bool(data.get("rename", False)))
        self.meta_var.set(bool(data.get("use_meta", True)))
        self.small_var.set(bool(data.get("ignore_small", True)))

    # ---------- trabajo en segundo plano ----------
    def run_worker(self, fn):
        self.stop_event.clear()
        self.set_busy(True)

        def target():
            try:
                fn()
            except Stopped:
                self.q.put(("stopped",))
            except Exception:
                self.q.put(("fatal", traceback.format_exc()))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def start_analyze(self):
        vd = self.validate()
        if not vd:
            return
        sources, root = vd
        opts = self.collect_opts()
        self.save_config()
        self.analysis = None
        self.analysis_key = (tuple(map(str, sources)), str(root), opts["use_meta"], opts["ignore_small"])
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        for v in self.tiles.values():
            v.set("—")
        self.log("ANÁLISIS", "title")
        for s in sources:
            self.log(f"Origen:  {s}")
        self.log(f"Destino: {root}")

        def job():
            res = analyze(sources, root, opts, self.stop_event, self.q.put)
            self.q.put(("analysis_done", res))
        self.run_worker(job)

    def start_organize(self):
        if not self.analysis:
            return
        vd = self.validate()
        if not vd:
            return
        sources, root = vd
        opts = self.collect_opts()
        key = (tuple(map(str, sources)), str(root), opts["use_meta"], opts["ignore_small"])
        if key != self.analysis_key:
            messagebox.showwarning(APP_NAME, "Cambiaste las carpetas o las opciones de lectura.\nVuelve a pulsar Analizar.")
            return

        items = self.analysis["items"]
        skip = opts["dup_mode"] == "skip"
        to_do = [it for it in items if not (it.duplicate_of and skip)]
        need = sum(it.size for it in to_do)

        # Espacio libre (al copiar, o al mover a otro disco)
        same_drive = all(os.path.splitdrive(str(s))[0].lower() == os.path.splitdrive(str(root))[0].lower() for s in sources)
        if not opts["move"] or not same_drive:
            probe = root
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            try:
                free = shutil.disk_usage(probe).free
                if need > free * 0.98:
                    messagebox.showerror(APP_NAME, f"No hay espacio suficiente en el destino.\n\n"
                                                   f"Se necesitan: {human_size(need)}\nLibres: {human_size(free)}")
                    return
            except OSError:
                pass

        verb = "MOVER" if opts["move"] else "COPIAR"
        msg = (f"Se van a {verb} {len(to_do):,} archivos ({human_size(need)}) a:\n\n{root}\n\n")
        if opts["move"]:
            msg += ("Los archivos dejarán de estar en sus carpetas actuales.\n"
                    "No se borra nada: todo queda en el destino y en el reporte.\n\n")
        else:
            msg += "Tus originales no se tocan.\n\n"
        if not messagebox.askyesno(APP_NAME, msg + "¿Continuar?", icon="warning" if opts["move"] else "question"):
            return

        self.save_config()
        self.log("")
        self.log("ORGANIZACIÓN", "title")
        analysis = self.analysis

        def job():
            res = organize(analysis, root, opts, self.stop_event, self.q.put)
            res["move"] = opts["move"]
            res["root"] = root
            self.q.put(("organize_done", res))
        self.run_worker(job)

    def stop(self):
        self.stop_event.set()
        self.status_var.set("Deteniendo… (termina el archivo actual)")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "Hay un proceso en curso. ¿Detenerlo y salir?"):
                return
            self.closing = True
            self.stop()
            self.root.after(200, self._close_when_done)
            return
        self._destroy()

    def _close_when_done(self):
        if self.worker and self.worker.is_alive():
            self.root.after(200, self._close_when_done)
        else:
            self._destroy()

    def _destroy(self):
        self.save_config()
        self.root.after_cancel(self._poll_id)
        self.root.destroy()

    # ---------- mensajes del hilo ----------
    def poll_queue(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if not self.closing or (self.worker and self.worker.is_alive()):
            self._poll_id = self.root.after(80, self.poll_queue)

    def set_progress(self, value, maximum):
        if str(self.progress["mode"]) == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress["value"] = 1000 * value / max(maximum, 1)

    def handle(self, msg):
        kind = msg[0]
        if kind == "status":
            self.status_var.set(msg[1])
        elif kind == "indeterminate":
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.status_var.set(msg[1])
            self.eta_var.set("")
        elif kind == "progress":
            _, value, maximum, text, eta = msg
            self.set_progress(value, maximum)
            self.status_var.set(text)
            self.eta_var.set(eta)
        elif kind == "log":
            self.log(msg[1], msg[2] if len(msg) > 2 else "info")
        elif kind == "analysis_done":
            self.on_analysis_done(msg[1])
        elif kind == "organize_done":
            self.on_organize_done(msg[1])
        elif kind == "stopped":
            self.set_progress(0, 1)
            self.eta_var.set("")
            self.status_var.set("Detenido. No se cambió nada más.")
            self.log("Proceso detenido por el usuario.", "warn")
            self.set_busy(False)
        elif kind == "fatal":
            self.set_progress(0, 1)
            self.status_var.set("Ocurrió un error inesperado.")
            self.log(msg[1], "err")
            self.set_busy(False)
            self.nb.select(1)
            messagebox.showerror(APP_NAME, "Ocurrió un error inesperado. Revisa la pestaña Registro.")

    def on_analysis_done(self, res):
        self.analysis = res
        st = summarize(res["items"])
        self.set_progress(1, 1)
        self.eta_var.set(f"en {fmt_duration(res['elapsed'])}")

        self.tiles["total"].set(f"{st['total']:,}")
        self.tiles["fotos"].set(f"{st['fotos']:,}")
        self.tiles["videos"].set(f"{st['videos']:,}")
        self.tiles["dups"].set(f"{st['dups']:,}")
        self.tiles["nodate"].set(f"{st['nodate']:,}")
        self.tiles["bytes"].set(human_size(st["bytes"]))

        self.tree.delete(*self.tree.get_children())
        by_year = defaultdict(list)
        for key, (f, v) in st["by_month"].items():
            if key:
                by_year[key[0]].append((key[1], f, v))
        years = sorted(by_year)
        for y in years:
            months = sorted(by_year[y])
            tf, tv = sum(m[1] for m in months), sum(m[2] for m in months)
            node = self.tree.insert("", "end", text=f"  {y}", values=(f"{tf:,}", f"{tv:,}", f"{tf + tv:,}"),
                                    tags=("year",), open=(y == years[-1]))
            for m, f, v in months:
                self.tree.insert(node, "end", text=f"      {MONTHS[m - 1]}", values=(f"{f:,}", f"{v:,}", f"{f + v:,}"))
        if None in st["by_month"]:
            f, v = st["by_month"][None]
            self.tree.insert("", "end", text="  Sin fecha  (_SIN_FECHA)", values=(f"{f:,}", f"{v:,}", f"{f + v:,}"),
                             tags=("special",))
        if st["dups"]:
            self.tree.insert("", "end", text="  Duplicados  (_DUPLICADOS)", values=("", "", f"{st['dups']:,}"),
                             tags=("special",))
        if st["total"]:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.configure(text="No se encontraron fotos ni videos en las carpetas elegidas.")
            self.empty_lbl.place(relx=0.5, rely=0.45, anchor="center")

        self.log("")
        self.log(f"Fotos: {st['fotos']:,}    Videos: {st['videos']:,}    Total: {human_size(st['bytes'])}")
        self.log("Cómo se obtuvo la fecha:")
        for method, label in DATE_METHODS.items():
            n = st["methods"].get(method, 0)
            if n:
                self.log(f"   {label:<22} {n:>8,}", "warn" if method == "fecha_archivo" else "info")
        if st["nodate"]:
            self.log(f"   {'Sin fecha':<22} {st['nodate']:>8,}", "warn")
        if st["dups"]:
            self.log(f"Duplicados: {st['dups']:,}  ({human_size(st['dup_bytes'])} repetidos)", "warn")
        if res["ignored"]:
            self.log(f"Ignorados (miniaturas, vacíos, carpetas de sistema): {len(res['ignored']):,}", "info")
        self.log("Revisa el resumen y pulsa Organizar.", "ok")

        if st["total"]:
            self.status_var.set(f"Análisis listo: {st['total']:,} archivos. Revisa el resumen y pulsa Organizar.")
        else:
            self.analysis = None
            self.status_var.set("No se encontraron fotos ni videos.")
        self.set_busy(False)

    def on_organize_done(self, res):
        self.last_report = res["report"]
        self.set_progress(1, 1)
        self.eta_var.set(f"en {fmt_duration(res['elapsed'])}")
        c = res["counts"]
        lines = [(k, v) for k, v in c.items() if v]
        for k, v in sorted(lines):
            self.log(f"   {k.capitalize():<26} {v:>8,}", "err" if k == "error" else "ok")
        self.log(f"Reporte: {res['report']}")
        if res["stopped"]:
            self.log("Se detuvo antes de terminar. Puedes volver a analizar y organizar: "
                     "lo que ya está en el destino no se repite.", "warn")

        # Tras mover (o tras terminar) las rutas analizadas ya no sirven: hay que analizar de nuevo.
        self.analysis = None
        self.set_busy(False)
        done = c.get("copiado", 0) + c.get("movido", 0)
        self.status_var.set(f"Listo: {done:,} archivos organizados en {res['root']}")
        msg = (f"{'Proceso detenido' if res['stopped'] else 'Organización terminada'}.\n\n"
               f"{'Movidos' if res['move'] else 'Copiados'}: {done:,}\n"
               f"Ya estaban en el destino: {c.get('ya estaba en el destino', 0):,}\n"
               f"Duplicados omitidos: {c.get('omitido (duplicado)', 0):,}\n"
               f"Errores: {c.get('error', 0):,}\n\n"
               f"Destino:\n{res['root']}\n\n¿Abrir la carpeta ahora?")
        if messagebox.askyesno(APP_NAME, msg):
            self.open_dest()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

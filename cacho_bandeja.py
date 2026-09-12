# -*- coding: utf-8 -*-
"""cacho_bandeja.py — la BANDEJA DE ARCHIVOS de cada sesión de Cacho (11-set-2026).

Qué es. Una tira de miniaturas al pie de la sesión con todo archivo que pasó por la
charla: lo que el usuario subió (＋ / arrastrar / pegar) y lo que Claude produjo o mostró
(un informe en ~/Desktop, una captura, un PDF por SendUserFile). Se toca y se abre
grande, en cualquier dispositivo, sin salir de Cacho.

RAÍZ (pedido del usuario, 11-set-2026). Mostrarle algo era «un chino»: la herramienta Read
renderiza la imagen sólo para Claude; «está en el Escritorio» no sirve cuando el usuario está
en la MacBook y el archivo lo escribió la Studio; la galería `/static/mostrar.html` había
que armarla a mano y romperla al final; y SendUserFile depende de que Claude se acuerde
de llamarlo. Cuatro caminos, ninguno automático. Este módulo hace que la máquina lo vea
sola: no hay que «mostrar» nada, alcanza con que el archivo haya pasado por la charla.

DE DÓNDE SALE LA LISTA. Del TRANSCRIPT de la sesión (`~/.claude/projects/*/<sid>.jsonl`),
que es la única fuente que ya tiene TODO: la ruta que /api/subir pegó en la terminal
(mensaje del usuario), el `file_path` de un Write/Read, los `files` de un SendUserFile, un
`cp … ~/Desktop/` en un Bash, o la ruta que Claude nombra en su texto. No hay un registro
aparte que mantener: si está en la charla, está en la bandeja. Se lee INCREMENTAL (el
.jsonl sólo crece: se guarda hasta qué byte se leyó) y se filtra por extensión VISIBLE —
código, JSON y llaves nunca entran, aunque Claude los haya leído.

QUIÉN SIRVE EL ARCHIVO. `serve_sesiones.py` (`/api/archivo?sid&ruta[&mini=1]`), y sólo
si la ruta está en la lista de ESA sesión: no es un lector de disco con PIN, es la
bandeja de una charla. Las miniaturas se hacen acá (PIL para imágenes; `qlmanage` del
sistema para PDF, video, HEIC y Office) y se guardan en ~/Library/Caches/Cacho/minis/.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

MINIS = os.path.expanduser("~/Library/Caches/Cacho/minis")
SUBIDAS = os.path.expanduser("~/Library/Caches/Cacho/subidas")
LADO_MINI = 320          # px del lado mayor de la miniatura
TOPE_ITEMS = 80          # lo último que pasó por la charla; más que eso nadie lo mira

# Extensión → tipo para el visor. Lo que no está acá NO es de la bandeja (código,
# JSON, llaves, bases): la lista es cerrada a propósito.
TIPOS = {
    ".png": "imagen", ".jpg": "imagen", ".jpeg": "imagen", ".webp": "imagen",
    ".gif": "imagen", ".heic": "imagen", ".svg": "imagen",
    ".pdf": "pdf",
    ".html": "html", ".htm": "html",
    ".md": "texto", ".txt": "texto", ".csv": "texto",
    ".xlsx": "doc", ".xls": "doc", ".docx": "doc", ".doc": "doc", ".pptx": "doc",
    ".numbers": "doc", ".pages": "doc",
    ".mp4": "video", ".mov": "video", ".m4v": "video", ".webm": "video",
    ".mp3": "audio", ".m4a": "audio", ".opus": "audio", ".wav": "audio", ".ogg": "audio",
}
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".html": "text/html", ".htm": "text/html", ".md": "text/plain",
    ".txt": "text/plain", ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".webm": "video/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".ogg": "audio/ogg",
}
_EXTS = "|".join(sorted(e[1:] for e in TIPOS))
# ¿Vale la pena parsear esta línea? Un .jsonl de 20 MB se recorre entero la primera
# vez; con esto el 95% de las líneas se descartan sin json.loads.
_RE_RAPIDA = re.compile(r"\.(?:%s)\b" % _EXTS, re.I)
# Rutas en texto libre / comandos. Primero las entrecomilladas (", ' o `), que es la
# única forma de que una ruta con espacios llegue entera (una carpeta «📈 Mi proyecto» los tiene);
# después las sueltas sin espacios.
_RE_RUTA = re.compile(
    r'["\'`]((?:~|/)[^"\'`\n]{2,500}?\.(?:%(e)s))["\'`]'
    r'|((?:~|/(?:Users|private|tmp|Volumes))[^\s"\'`<>|;&()\[\]{}]{1,500}?\.(?:%(e)s))'
    r'(?=[\s"\'`<>|;&()\[\]{}.,:!?]|$)' % {"e": _EXTS}, re.I)
# El base64 de una imagen leída: una sola línea pesa 1,3 MB y no trae ninguna ruta.
_RE_B64 = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=]{2000,}"')
# Lo que NUNCA se sirve, esté donde esté: llaves, tokens, la memoria del CLI.
_RE_PROHIBIDA = re.compile(r"/\.claude/|/\.ssh/|token|clave|secret|password|credencial", re.I)

_lock = threading.Lock()
_estado = {}   # path del transcript -> {"off", "size", "mtime", "items": {ruta: item}}


def _hora_local(ts):
    """HH:MM en la hora de la máquina. El transcript viene en UTC («…Z»): crudo se
    lee tres horas adelantado."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except Exception:
        return ts[11:16]


def _normalizar(ruta):
    ruta = os.path.normpath(os.path.expanduser(ruta.strip()))
    if not os.path.isabs(ruta):
        return None
    ext = os.path.splitext(ruta)[1].lower()
    if ext not in TIPOS or _RE_PROHIBIDA.search(ruta):
        return None
    return ruta


def _rutas_en_texto(txt):
    if not txt or not isinstance(txt, str):
        return []
    out = []
    for m in _RE_RUTA.finditer(txt):
        r = _normalizar(m.group(1) or m.group(2) or "")
        if r:
            out.append(r)
    return out


def _rutas_en_tool(b):
    """Rutas de un tool_use. Los campos con nombre de ruta se toman enteros (así una
    ruta con espacios no depende de comillas); el resto del input se lee como texto."""
    inp = b.get("input") or {}
    if not isinstance(inp, dict):
        return [], ""
    out = []
    for k, v in inp.items():
        if isinstance(v, str):
            if k in ("file_path", "path", "notebook_path", "ruta"):
                r = _normalizar(v)
                if r:
                    out.append(r)
            elif k not in ("content", "new_string", "old_string"):
                # el contenido de un Write puede nombrar rutas que no son de la charla
                out += _rutas_en_texto(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    r = _normalizar(x)
                    if r:
                        out.append(r)
    nota = inp.get("caption") if isinstance(inp.get("caption"), str) else ""
    return out, (nota or "")[:200]


def _procesar(line, items):
    if len(line) > 100_000:
        line = _RE_B64.sub('"data":""', line)
    try:
        d = json.loads(line)
    except ValueError:
        # Sólo llegan líneas completas (archivos() corta en el último salto de línea): una que
        # no es JSON es un transcript roto de verdad. Se cuenta y se dice, no se calla ni se
        # revienta la bandeja entera por un renglón (policía 11-set-2026).
        items["__rotas__"] = items.get("__rotas__", 0) + 1
        return
    if not isinstance(d, dict) or d.get("type") not in ("user", "assistant"):
        return
    if d.get("isSidechain") or d.get("isMeta"):
        return
    quien = "vos" if d["type"] == "user" else "claude"
    ts = d.get("timestamp") or ""
    content = (d.get("message") or {}).get("content")
    hallazgos = []   # (ruta, nota)
    if isinstance(content, str):
        hallazgos += [(r, "") for r in _rutas_en_texto(content)]
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                hallazgos += [(r, "") for r in _rutas_en_texto(b.get("text"))]
            elif t == "tool_use":
                rutas, nota = _rutas_en_tool(b)
                hallazgos += [(r, nota) for r in rutas]
            # tool_result: es lo que VOLVIÓ de una herramienta (contenidos, listados),
            # no algo que alguien puso en la charla
    for ruta, nota in hallazgos:
        it = items.get(ruta)
        if it is None:
            items[ruta] = {"ruta": ruta, "quien": quien, "ts": ts, "nota": nota}
        else:
            # la primera mención manda (quién lo trajo y cuándo); la nota, la primera que haya
            if nota and not it["nota"]:
                it["nota"] = nota
        if ruta.startswith(SUBIDAS + os.sep):
            items[ruta]["quien"] = "vos"   # lo subió el usuario, aunque Claude lo lea después


def archivos(path):
    """Los archivos que pasaron por la charla de `path` (transcript), en orden de
    aparición, sólo los que EXISTEN hoy. Incremental: lee lo que creció desde la
    última vez. Si el archivo se achicó (otro transcript con el mismo nombre) arranca
    de cero."""
    st = os.stat(path)
    with _lock:
        e = _estado.get(path)
        if e is None or st.st_size < e["off"]:
            e = _estado[path] = {"off": 0, "size": -1, "mtime": 0, "items": {}}
        if st.st_size != e["size"] or st.st_mtime != e["mtime"]:
            with open(path, "rb") as fh:
                fh.seek(e["off"])
                data = fh.read()
            fin = data.rfind(b"\n")          # sólo líneas completas
            data = data[:fin + 1] if fin >= 0 else b""
            e["off"] += len(data)
            for line in data.decode("utf-8", "replace").splitlines():
                if _RE_RAPIDA.search(line):
                    _procesar(line, e["items"])
            e["size"], e["mtime"] = st.st_size, st.st_mtime
        rotas = e["items"].get("__rotas__", 0)
        vivos = [v for k, v in e["items"].items() if k != "__rotas__"]
    if rotas:
        print(f"⚠️ bandeja de {os.path.basename(path)[:8]}: {rotas} línea(s) del transcript "
              "que no son JSON, salteadas", file=sys.stderr)
    salida = []
    for it in vivos:
        try:
            s = os.stat(it["ruta"])
        except OSError:
            continue   # ya no está (scratchpad borrado, «mostrar-*» limpiado): no se lista
        ext = os.path.splitext(it["ruta"])[1].lower()
        salida.append({
            "ruta": it["ruta"], "nombre": os.path.basename(it["ruta"]),
            "ext": ext, "tipo": TIPOS[ext], "quien": it["quien"],
            "ts": it["ts"], "hora": _hora_local(it["ts"]), "nota": it["nota"],
            "bytes": s.st_size, "mtime": int(s.st_mtime),
        })
    return salida[-TOPE_ITEMS:]


def permitida(path, ruta):
    """¿`ruta` está en la bandeja de la sesión `path`? Es la ÚNICA puerta por la que
    /api/archivo sirve algo: sin esto el endpoint sería «leer cualquier archivo con PIN»."""
    r = _normalizar(ruta)
    if not r:
        return None
    return r if any(it["ruta"] == r for it in archivos(path)) else None


# ---------------------------------------------------------------------------
# Miniaturas
# ---------------------------------------------------------------------------

def _mini_pil(ruta, salida):
    from PIL import Image
    im = Image.open(ruta)
    im.thumbnail((LADO_MINI, LADO_MINI), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "P"):
        fondo = Image.new("RGB", im.size, (255, 255, 255))
        fondo.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = fondo
    else:
        im = im.convert("RGB")
    tmp = salida + ".tmp"
    im.save(tmp, "JPEG", quality=82, optimize=True)
    os.replace(tmp, salida)                  # atómico: nadie sirve un JPEG a medio escribir


def _mini_quicklook(ruta, salida):
    """PDF, video, HEIC, Office, SVG: lo dibuja Quick Look del sistema (lo mismo que
    ves al apretar espacio en el Finder). Escribe <nombre>.png en la carpeta que se
    le da; de ahí se pasa a JPEG con PIL."""
    tmp = tempfile.mkdtemp(prefix="cacho-mini-")
    try:
        r = subprocess.run(["/usr/bin/qlmanage", "-t", "-s", str(LADO_MINI), "-o", tmp, ruta],
                           capture_output=True, text=True, timeout=20)
        png = os.path.join(tmp, os.path.basename(ruta) + ".png")
        if not os.path.isfile(png):
            raise RuntimeError(f"qlmanage no dibujó {os.path.basename(ruta)!r}: "
                               f"{(r.stderr or r.stdout).strip()[:200]}")
        _mini_pil(png, salida)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def miniatura(ruta):
    """Ruta a la miniatura JPEG de `ruta` (la hace si no está), o None si este tipo no
    tiene dibujo o no se pudo. El visor pone un ícono en ese caso."""
    try:
        s = os.stat(ruta)
    except OSError:
        return None
    ext = os.path.splitext(ruta)[1].lower()
    tipo = TIPOS.get(ext)
    if tipo in (None, "audio", "texto"):
        return None
    os.makedirs(MINIS, exist_ok=True)
    clave = hashlib.sha1(f"{ruta}|{int(s.st_mtime)}|{s.st_size}".encode()).hexdigest()
    salida = os.path.join(MINIS, clave + ".jpg")
    if os.path.isfile(salida):
        return salida
    try:
        if tipo == "imagen" and ext not in (".heic", ".svg"):
            _mini_pil(ruta, salida)
        else:
            _mini_quicklook(ruta, salida)
        return salida
    except Exception as e:
        print(f"⚠️ bandeja: sin miniatura para {ruta}: {e!r}", file=sys.stderr)
        return None


if __name__ == "__main__":
    # python3 cacho_bandeja.py <sid|transcript.jsonl>  → lista lo que vería la bandeja
    import glob
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg.endswith(".jsonl"):
        p = arg
    else:
        hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{arg}*.jsonl"))
        if not hits:
            sys.exit(f"no encontré el transcript {arg!r}")
        p = hits[0]
    for it in archivos(p):
        m = miniatura(it["ruta"])
        print(f"{it['hora']:>5}  {it['quien']:<6} {it['tipo']:<6} "
              f"{'🖼' if m else '  '} {it['nombre']}  ({it['bytes']:,} b)".replace(",", "."))

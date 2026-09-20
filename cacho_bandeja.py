# -*- coding: utf-8 -*-
"""cacho_bandeja.py — la BANDEJA DE ARCHIVOS de cada sesión de Cacho (11-set-2026).

Qué es. Una tira de miniaturas al pie de la sesión con lo que el usuario subió (＋ / arrastrar /
pegar) y lo que ESA sesión produjo (un informe en ~/Desktop, una pieza en publicidad/assets,
un PDF por SendUserFile). Se toca y se abre grande, en cualquier dispositivo, sin salir de
Cacho.

RAÍZ (pedido del usuario, 11-set-2026). Mostrarle algo era «un chino»: la herramienta Read
renderiza la imagen sólo para Claude; «está en el Escritorio» no sirve cuando el usuario está
en la MacBook y el archivo lo escribió la Studio; la galería `/static/mostrar.html` había
que armarla a mano y romperla al final; y SendUserFile depende de que Claude se acuerde
de llamarlo. Cuatro caminos, ninguno automático. Este módulo hace que la máquina lo vea
sola: no hay que «mostrar» nada, alcanza con que el archivo haya pasado por la charla.

DE DÓNDE SALE LA LISTA. Del TRANSCRIPT de la sesión, que es la única fuente que ya tiene
TODO: la ruta que /api/subir pegó en la terminal (mensaje del usuario), el `file_path` de un
Write/Read, los `files` de un SendUserFile, un `cp … ~/Desktop/` en un Bash, o la ruta que
el agente nombra en su texto. No hay un registro aparte que mantener. Se lee INCREMENTAL
(el .jsonl sólo crece: se guarda hasta qué byte se leyó) y se filtra por extensión VISIBLE —
código, JSON y llaves nunca entran, aunque el agente los haya leído. Dos formatos:
  · Claude Code   ~/.claude/projects/*/<sid>.jsonl   (registros user/assistant con `cwd`)
  · Codex CLI     ~/.codex/sessions/AAAA/MM/DD/rollout-*-<thread>.jsonl  (Gpto y el Policía;
                  `response_item` + `event_msg`, con el cwd en `session_meta`/`turn_context`)

SÓLO LO TUYO Y LO QUE ESTA SESIÓN CREÓ (regla del usuario, 12-set-2026: «que ese recuadro sólo
me muestre lo que subí yo o lo que creó la sesión en la que estoy parado»). Hasta ese día
entraba todo lo que la charla NOMBRABA, y una sesión de Jaime que abre veinte creativos
viejos para mirarlos ponía los veinte en la tira, al lado de los dos que hizo. La regla:
  · lo que dijo EL USUARIO (una ruta en su mensaje, o lo que vive en ~/Library/Caches/Cacho/subidas)
    entra siempre — es lo que subió;
  · lo que nombra el AGENTE entra sólo si el archivo NACIÓ mientras la sesión estaba
    TRABAJANDO: el mtime (o el birthtime) cae dentro de una ventana de herramientas de
    ESTE transcript (del primer tool_use al último tool_result de esa corrida). Un archivo
    que existía de antes y sólo se leyó queda afuera; uno que escribió otra sesión, también.
    Es la forma exacta que pidió el usuario: no «lo que pasó por la charla», sino «lo que creó».
  · lo que va por SendUserFile o por `tools/mostrar.py` entra siempre: es el agente diciendo
    «mirá esto», y ahí sí puede ser algo viejo. Son LAS puertas para mostrarle a el usuario un
    archivo que ya existía. `mostrar.py` además le pide al server que la página ABRA el visor
    (`presentar()` / `presentacion_reciente()`): la tira es la memoria, el visor es el «ahora».
  · el scratchpad de la sesión (/tmp/claude-*/…/scratchpad/) queda afuera salvo por
    SendUserFile: son recortes y pruebas intermedias, no entregables (una sesión de
    creativos dejaba doce recortes de la misma imagen en la tira).

QUIÉN SIRVE EL ARCHIVO. `serve_sesiones.py` (`/api/archivo?sid&ruta[&mini=1]`), y sólo
si la ruta está en la lista de ESA sesión: no es un lector de disco con PIN, es la
bandeja de una charla. Las miniaturas se hacen acá (PIL para imágenes; `qlmanage` del
sistema para PDF, video, HEIC y Office) y se guardan en ~/Library/Caches/Cacho/minis/.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
# Rutas RELATIVAS al cwd de la sesión («publicidad/assets/x/pieza-v1.png»): es como las
# escribe Codex en sus comandos y en su texto (Gpto: «Pieza guardada (publicidad/assets/…)»),
# y también un `cp` de Claude. Piden al menos una carpeta adelante para no leer «foto.png»
# suelto como ruta; se resuelven contra el cwd y sólo cuentan si el archivo existe.
_RE_RUTA_REL = re.compile(
    r'(?<![\w/.~\-])((?:[\w.\-]+/)+[\w.\-]+\.(?:%s))(?=[\s"\'`<>|;&()\[\]{}.,:!?]|$)' % _EXTS, re.I)
# El base64 de una imagen: una sola línea pesa 1,3 MB y no trae ninguna ruta. Claude lo
# guarda en `"data": "…"`; Codex en `"image_url": "data:image/png;base64,…"`.
_RE_B64 = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=]{2000,}"|data:image/[a-z]+;base64,[A-Za-z0-9+/=]{2000,}')
# Lo que NUNCA se sirve, esté donde esté: llaves, tokens, la memoria del CLI.
_RE_PROHIBIDA = re.compile(r"/\.claude/|/\.ssh/|token|clave|secret|password|credencial", re.I)
# El scratchpad de una sesión de Claude Code: recortes y pruebas, no entregables.
_RE_SCRATCH = re.compile(r"/claude-\d+/[^/]+/[^/]+/scratchpad/")
# Herramientas que son «mirá esto»: lo que pasa por ahí entra a la bandeja aunque sea viejo.
_MUESTRAN = {"SendUserFile"}
# Lo mismo dicho por un comando: `python3 tools/mostrar.py pieza.png` (Bash de Claude o shell
# de Codex). RAÍZ (policía 13-set-2026): la puerta nueva mostraba una placa de ayer y la
# bandeja la descartaba por vieja — reconocía SendUserFile y no a mostrar.py. Se lee del
# transcript, así sobrevive a un reinicio del server (el registro de abajo no).
_RE_MOSTRAR = re.compile(r"(?<![\w/.-])(?:tools/)?mostrar\.py\b")
# Presentaciones explícitas que el server recibió por /api/mostrar (tools/mostrar.py):
# transcript -> [item]. Es lo que hace que la página abra el visor; la tira ya lo tiene por
# el transcript. Vive en memoria: un reinicio la pierde y no pasa nada (el «ahora» ya pasó).
_presentadas = {}
_n_presentacion = 0
# La página abre sola lo presentado hace menos de esto (segundos): después queda en la tira.
VENTANA_ABRIR = 1800
# Holgura entre el reloj del transcript y el del disco (segundos).
_HOLGURA = 3.0

_lock = threading.Lock()
# path del transcript -> {"off", "size", "mtime", "formato", "cwd", "items": {ruta: item},
#                         "ventanas": [(desde, hasta)], "abierta": desde|None, "ultimo_res"}
_estado = {}


def _epoch(ts):
    """Segundos epoch de un timestamp ISO del transcript («…Z»); None si no se lee."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


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


def _rutas_en_texto(txt, cwd=""):
    if not txt or not isinstance(txt, str):
        return []
    out = []
    # Markdown de Codex: [pieza](</ruta con espacios/pieza.png>) y links con :línea.
    for match in re.finditer(r'\]\(<?((?:/|~)[^\n]*?)>?\)', txt):
        r = _normalizar(re.sub(r':\d+$', '', match.group(1)))
        if r:
            out.append(r)
    for m in _RE_RUTA.finditer(txt):
        r = _normalizar(m.group(1) or m.group(2) or "")
        if r:
            out.append(r)
    if cwd:
        for m in _RE_RUTA_REL.finditer(txt):
            r = _normalizar(os.path.join(cwd, m.group(1)))
            if r:
                out.append(r)
    return out


def _rutas_en_tool(b, cwd=""):
    """Rutas de un tool_use. Los campos con nombre de ruta se toman enteros (así una
    ruta con espacios no depende de comillas); el resto del input se lee como texto."""
    inp = b.get("input") or {}
    if not isinstance(inp, dict):
        return [], "", False        # el caller desempaca TRES (rutas, nota, muestra)
    out = []
    for k, v in inp.items():
        if isinstance(v, str):
            if k in ("file_path", "path", "notebook_path", "ruta"):
                r = _normalizar(v)
                if r:
                    out.append(r)
            elif k not in ("content", "new_string", "old_string"):
                # el contenido de un Write puede nombrar rutas que no son de la charla
                out += _rutas_en_texto(v, cwd)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    r = _normalizar(x)
                    if r:
                        out.append(r)
    nota = inp.get("caption") if isinstance(inp.get("caption"), str) else ""
    cmd = inp.get("command")
    cmd = " ".join(cmd) if isinstance(cmd, list) else (cmd if isinstance(cmd, str) else "")
    muestra = b.get("name") in _MUESTRAN or bool(_RE_MOSTRAR.search(cmd))
    return out, (nota or "")[:200], muestra


def _procesar(line, items, estado=None):
    if len(line) > 100_000:
        # Una data URL vive DENTRO de un string: insertar comillas rompe el JSON.
        line = _RE_B64.sub(lambda m: '"data":""' if m.group().startswith('"data"')
                           else 'data:image/png;base64,', line)
    try:
        d = json.loads(line)
    except ValueError:
        # Sólo llegan líneas completas (archivos() corta en el último salto de línea): una que
        # no es JSON es un transcript roto de verdad. Se cuenta y se dice, no se calla ni se
        # revienta la bandeja entera por un renglón (policía 11-set-2026).
        items["__rotas__"] = items.get("__rotas__", 0) + 1
        return
    if not isinstance(d, dict):
        return
    estado = estado if estado is not None else {}
    tipo = d.get('type')
    if tipo in ('session_meta', 'turn_context'):
        estado['formato'] = 'codex'
        estado['cwd'] = (d.get('payload') or {}).get('cwd') or estado.get('cwd', '')
        return
    if tipo == 'response_item':
        p = d.get('payload') or {}
        ts = d.get('timestamp', '')
        pt = p.get('type')
        if pt in ('function_call', 'custom_tool_call'):
            estado.setdefault('llamadas', {})[p.get('call_id')] = _epoch(ts)
            inp = p.get('input') if pt == 'custom_tool_call' else p.get('arguments')
            if pt == 'function_call' and isinstance(inp, str):
                # `arguments` del shell de Codex es JSON en texto: {"command": ["bash","-lc",
                # "python3 tools/mostrar.py \"/ruta con espacios/x.mp4\""]}. Se decodifica y
                # queda el comando real. RAÍZ (13-set-2026): leer el JSON crudo con el regex de
                # rutas perdía toda ruta entre comillas dobles (llegan como \") — y el repo se
                # llama «📈 Mi proyecto»: un `mostrar.py <mp4>` de Gpto no entraba a la tira.
                try:
                    dec = json.loads(inp)
                except ValueError:
                    dec = None
                if isinstance(dec, dict):
                    cmd = dec.get('command')
                    if isinstance(cmd, list):
                        inp = ' '.join(str(x) for x in cmd)
                    elif isinstance(cmd, str):
                        inp = cmd
            d = {'type': 'assistant', 'timestamp': ts, 'message': {'content': [
                {'type': 'tool_use', 'name': p.get('name'), 'input': {'command': inp}}]}}
        elif pt in ('function_call_output', 'custom_tool_call_output'):
            start = estado.setdefault('llamadas', {}).pop(p.get('call_id'), None)
            end = _epoch(ts)
            if start is not None and end is not None:
                estado.setdefault('ventanas', []).append((start, end))
            return
        elif pt == 'message' and p.get('role') in ('user', 'assistant'):
            content = p.get('content') or []
            texts = [b.get('text', '') for b in content if isinstance(b, dict)
                     and b.get('type') in ('input_text', 'output_text', 'text')]
            # AGENTS/contexto de arranque no son archivos subidos por el usuario.
            texts = [s for s in texts if not s.startswith('# AGENTS.md instructions for ')]
            d = {'type': p['role'], 'timestamp': ts,
                 'message': {'content': '\n'.join(texts)}}
        else:
            return
    if d.get("type") not in ("user", "assistant"):
        return
    _ventanas_claude(d, estado)
    if d.get("isSidechain") or d.get("isMeta"):
        return
    quien = "vos" if d["type"] == "user" else (
        "gpto" if estado.get('formato') == 'codex' else "claude")
    cwd = d.get('cwd') or estado.get('cwd', '')
    if d.get('cwd'):
        estado['cwd'] = d['cwd']    # Claude lo trae en cada registro; Codex en session_meta
    ts = d.get("timestamp") or ""
    content = (d.get("message") or {}).get("content")
    hallazgos = []   # (ruta, nota)
    if isinstance(content, str):
        hallazgos += [(r, "") for r in _rutas_en_texto(content, cwd)]
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                hallazgos += [(r, "") for r in _rutas_en_texto(b.get("text"), cwd)]
            elif t == "tool_use":
                rutas, nota, muestra = _rutas_en_tool(b, cwd)
                hallazgos += [(r, nota, muestra) for r in rutas]
            # tool_result: es lo que VOLVIÓ de una herramienta (contenidos, listados),
            # no algo que alguien puso en la charla
    for h in hallazgos:
        ruta, nota = h[0], h[1]
        muestra = h[2] if len(h) > 2 else False
        it = items.get(ruta)
        if it is None:
            it = items[ruta] = {"ruta": ruta, "quien": quien, "ts": ts, "nota": nota,
                                "mostrado": False}
        else:
            # la primera mención manda (quién lo trajo y cuándo); la nota, la primera que haya
            if nota and not it["nota"]:
                it["nota"] = nota
        if muestra:
            it["mostrado"] = True   # pasó por SendUserFile: «mirá esto», entra aunque sea viejo
        if ruta.startswith(SUBIDAS + os.sep):
            it["quien"] = "vos"   # lo subió el usuario, aunque Claude lo lea después


def _ventanas_claude(d, estado):
    """Las ventanas de TRABAJO de un transcript de Claude Code: cada tool_use (assistant) abre
    por su `id` y el tool_result (user) que lo cierra da el fin. Es el mismo par que Gpto
    junta para Codex por `call_id`. Lo que esta sesión CREÓ nació dentro de una de estas
    ventanas; lo que sólo leyó, no. Se juntan también las de los subagentes (isSidechain):
    escriben archivos de la misma charla."""
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    t0 = _epoch(d.get("timestamp") or "")
    if t0 is None:
        return
    llamadas = estado.setdefault("llamadas", {})
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("id"):
            llamadas[b["id"]] = t0
        elif b.get("type") == "tool_result" and b.get("tool_use_id"):
            ini = llamadas.pop(b["tool_use_id"], None)
            if ini is not None:
                estado.setdefault("ventanas", []).append((ini, t0))


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
            # Por b"\n" y NO por splitlines(): un JSON válido puede llevar U+2028/U+2029,
            # NEL, \x0b o \x0c crudos dentro de un string, y splitlines() corta también ahí:
            # el registro quedaba en 2-3 pedazos que no son JSON, contados como «rotas», y
            # SUS rutas y su ventana se perdían (el Policía, 20-set-2026: 352 avisos
            # «3 línea(s)… que no son JSON» de un solo rollout de Codex con dos U+2028).
            for raw in data.split(b"\n"):
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if (_RE_RAPIDA.search(line) or '"response_item"' in line
                        or '"session_meta"' in line or '"turn_context"' in line
                        or '"tool_use"' in line or '"tool_use_id"' in line):
                    _procesar(line, e["items"], e)
            e["size"], e["mtime"] = st.st_size, st.st_mtime
        rotas = e["items"].get("__rotas__", 0)
        vivos = [v for k, v in e["items"].items() if k != "__rotas__"]
        ventanas = list(e.get('ventanas', []))
        # una herramienta que todavía no volvió: la sesión está trabajando AHORA, y lo que
        # nazca desde ese momento es suyo (si no, el archivo recién escrito tarda en aparecer)
        abiertas = [v for v in e.get("llamadas", {}).values() if v is not None]
        repo = os.path.realpath(e.get("cwd") or "") if e.get("cwd") else ""
        avisadas = e.get("rotas_avisadas", 0)
        if rotas > avisadas:
            e["rotas_avisadas"] = rotas
    if rotas > avisadas:
        # Una vez por cambio del contador, no en cada llamada (la página pide la bandeja cada
        # 4 s: eran 21.600 líneas/día por transcript). Y por la COLA del nombre: la cabeza
        # de un rollout de Codex es «rollout-» y no identifica nada.
        print(f"⚠️ bandeja de …{os.path.basename(path)[-18:-6]}: {rotas} línea(s) del transcript "
              "que no son JSON, salteadas", file=sys.stderr)
    # Lo presentado por /api/mostrar: entra aunque el transcript no lo haya nombrado todavía
    # (Codex escribe su rollout con retraso) y trae el título que el transcript no tiene.
    with _lock:
        presentadas = list(_presentadas.get(path, []))
    for pr in presentadas:
        it = next((v for v in vivos if v["ruta"] == pr["ruta"]), None)
        if it is None:
            it = {"ruta": pr["ruta"], "quien": pr["quien"], "ts": pr["ts_iso"], "nota": ""}
            vivos.append(it)
        it["mostrado"] = True
        if pr["titulo"]:
            it["nota"] = pr["titulo"]
    salida = []
    for it in vivos:
        try:
            s = os.stat(it["ruta"])
        except OSError:
            continue   # ya no está (scratchpad borrado, «mostrar-*» limpiado): no se lista
        if not os.path.isfile(it['ruta']):
            continue
        # «Lo que subí yo» es lo de subidas/ o una ruta de AFUERA del repo (Escritorio, Drive…).
        # Un archivo DEL repo nombrado en un mensaje de usuario casi nunca lo tipeó el usuario: es el
        # prompt con el que cacho_lanzar abrió la pestaña («leé publicidad/CLAUDE.md…»), y ése
        # se juzga como lo que nombra el agente — entra si la sesión lo escribió.
        tuyo = it['quien'] == 'vos' and (
            it['ruta'].startswith(SUBIDAS + os.sep)
            or not repo or not os.path.realpath(it['ruta']).startswith(repo + os.sep))
        if not tuyo and not it.get("mostrado"):
            # SÓLO lo que ESTA sesión creó (regla del usuario, 12-set-2026): el archivo tiene que
            # haber nacido mientras la sesión trabajaba. Lo viejo que sólo se leyó, y lo que
            # escribió otra sesión, quedan afuera. El scratchpad son pruebas intermedias.
            if _RE_SCRATCH.search(it["ruta"]):
                continue
            nacio = max(s.st_mtime, getattr(s, "st_birthtime", 0))
            if not (any(a - _HOLGURA <= nacio <= b + _HOLGURA for a, b in ventanas)
                    or any(nacio >= a - _HOLGURA for a in abiertas)):
                continue
        ext = os.path.splitext(it["ruta"])[1].lower()
        salida.append({
            "ruta": it["ruta"], "nombre": os.path.basename(it["ruta"]),
            "ext": ext, "tipo": TIPOS[ext], "quien": it["quien"],
            "ts": it["ts"], "hora": _hora_local(it["ts"]), "nota": it["nota"],
            "bytes": s.st_size, "mtime": int(s.st_mtime),
        })
    return salida[-TOPE_ITEMS:]


def presentar(path, ruta, titulo="", quien="claude"):
    """Registra que la sesión del transcript `path` le MUESTRA `ruta` a el usuario (viene de
    tools/mostrar.py por /api/mostrar). Devuelve el ítem de la bandeja tal como lo va a ver
    la página. Tira si el archivo no existe o no es de un tipo que la bandeja muestre —
    ruidoso a propósito: «mostrada» sin que se pueda ver es la mentira que destapó el
    policía el 13-set-2026."""
    global _n_presentacion
    r = _normalizar(ruta)
    if not r:
        raise ValueError(f"{ruta!r} no es un archivo que la bandeja muestre "
                         f"(tipos: {', '.join(sorted(TIPOS))}; ni llaves ni .claude/)")
    if not os.path.isfile(r):
        raise FileNotFoundError(r)
    ahora = time.time()
    with _lock:
        _n_presentacion += 1
        pr = {"ruta": r, "titulo": (titulo or "")[:200], "quien": quien, "ts": ahora,
              "ts_iso": datetime.utcfromtimestamp(ahora).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
              "n": _n_presentacion}
        _presentadas.setdefault(path, []).append(pr)
    it = next((x for x in archivos(path) if x["ruta"] == r), None)
    if it is None:
        raise RuntimeError(f"registré {r} pero la bandeja no lo lista (¿se borró recién?)")
    return it


def presentacion_reciente(path):
    """La última presentación de la sesión si fue hace menos de VENTANA_ABRIR segundos:
    {"ruta", "titulo", "n"} — la página abre el visor una vez por `n`. None si no hay."""
    with _lock:
        lista = _presentadas.get(path) or []
        pr = lista[-1] if lista else None
    if not pr or time.time() - pr["ts"] > VENTANA_ABRIR:
        return None
    return {"ruta": pr["ruta"], "titulo": pr["titulo"], "n": pr["n"]}


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

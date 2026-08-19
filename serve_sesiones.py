#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cacho — gestor de sesiones de terminal con estética de la app de Claude.

Una app local: barra lateral con todas las sesiones (qué hace cada una, cuál
trabaja, cuál espera) y pestañas con terminales REALES corriendo `claude`
adentro. Se abre con la app "Cacho" del Dock (ventana propia, sin navegador
alrededor).

Escucha SOLO en 127.0.0.1. OJO: si usás Tailscale, eso NO alcanza para
aislarlo del tailnet — tailscaled en modo userspace reenvía a localhost las
conexiones entrantes de tus otros dispositivos. O sea: desde el teléfono o la
laptop por Tailscale este server SÍ se ve. Una terminal alcanzable por red =
control total de esa máquina, así que la puerta no es decorativa. Cómo está
cerrada:

  · Se entra UNA vez con el PIN de ~/.cacho_pin (se genera solo, por máquina; el
    lanzador de Cacho.app lo pasa en la URL y el server redirige para sacarlo de
    la barra). Acertarlo entrega un TOKEN de sesión de 256 bits, que es lo que
    viaja después: el PIN no queda guardado en el navegador.
  · El login está frenado: 8 PIN equivocados y esa IP queda afuera 1 min, 5, 15,
    1 h. Se contesta al instante, sin dormir la request (dormirla no frena nada:
    el atacante las lanza en paralelo). Los bloqueos quedan escritos en
    ~/Library/Logs/cacho-seguridad.log.
  · Estar adentro NO pasa por el freno: el token no se adivina. Por eso alguien
    golpeando la puerta no deja afuera al dueño — que es lo que pasaba cuando la
    cookie guardaba el PIN, porque por Tailscale todos los dispositivos llegan
    con la misma IP de origen que localhost.
  · Sin token no se ve NADA salvo /api/ping y el logo de la pantalla de login.
  · Los .html de static/ se sirven en sandbox: son para mirar, no son código de
    Cacho, y no pueden llamar a las APIs.

Para echar a todos los dispositivos: borrar
~/Library/Application Support/Cacho/sesiones_web.json (y cambiar el PIN si hace
falta, que se relee en caliente: `echo 1234 > ~/.cacho_pin`).

Uso:  python3 serve_sesiones.py            # http://127.0.0.1:8811
      CACHO_PORT=9000 python3 serve_sesiones.py   # otro puerto (tests)
"""
import base64
import fcntl
import hmac
import ipaddress
import json
import os
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from datetime import datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlencode, urlparse

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CARPETA_PROYECTOS = os.path.expanduser("~/Claude/Projects")
AQUI = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CACHO_PORT", "8811"))
BIND = "127.0.0.1"   # NO cambiar a 0.0.0.0: expondría terminales por red
CHUNK = 256 * 1024   # bytes que se leen de cabeza/cola de cada transcript
TOPE_BUFFER = 2_000_000  # scrollback por terminal; es lo que se re-manda al reconectar
COALESCE_MAX = 64 * 1024  # tope al juntar trozos del PTY en un solo evento SSE (ver _stream)

# Identidad de ESTE arranque del server. La página lo compara contra /api/ping
# y se recarga sola si cambió: reiniciar el server NO recarga las ventanas de
# Chrome ya abiertas (open -a solo las trae al frente), y sin esto quedaban
# corriendo el JS viejo para siempre (reiniciabas el server y seguías viendo
# la UI anterior).
BOOT_ID = uuid.uuid4().hex

# PIN de acceso (ver docstring). Un PIN por máquina, persistido en ~/.cacho_pin.
PIN_PATH = os.path.expanduser("~/.cacho_pin")


def _cargar_pin():
    try:
        with open(PIN_PATH) as fh:
            pin = fh.read().strip()
        if pin:
            return pin
    except OSError:
        pass
    import secrets
    pin = "".join(secrets.choice("0123456789") for _ in range(6))
    # 0600 desde el os.open, NO con un chmod después: entre el open y el chmod el
    # archivo existe con los permisos del umask (habitualmente legible por todos) y
    # ahí está la llave de la máquina. Es una ventana chica, pero es evitable.
    fd = os.open(PIN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(pin + "\n")
    return pin


PIN = _cargar_pin()

# El PIN se RELEE del archivo cuando cambia. Antes se leía una sola vez al arrancar:
# cambiarlo obligaba a reiniciar el server, y reiniciar el server mata las pestañas
# vivas. Ahora alcanza con `echo 1234 > ~/.cacho_pin` y rige en la request siguiente.
# Cacheado por mtime: no es un open() por request.
_pin_cache = {"mtime": 0.0, "pin": PIN}
_PIN_LOCK = threading.Lock()


def _pin_actual():
    try:
        m = os.path.getmtime(PIN_PATH)
    except OSError:
        return _pin_cache["pin"]
    with _PIN_LOCK:
        if m != _pin_cache["mtime"]:
            try:
                with open(PIN_PATH) as fh:
                    nuevo = fh.read().strip()
                if nuevo:
                    _pin_cache.update(mtime=m, pin=nuevo)
            except OSError:
                pass
        return _pin_cache["pin"]


# ─── Freno anti fuerza bruta ───────────────────────────────────────────────────
# Acertar el PIN no es "ver la app": es una terminal con control total de esta
# máquina. Y el PIN es corto a propósito (se tipea en el celular), así que el
# espacio de búsqueda es chico y lo único que lo defiende es este freno.
#
# La primera versión dormía la request que fallaba. NO SIRVE, y está medido
# (16-ago-2026): el atacante abre 60 conexiones a la vez y las esperas corren en
# paralelo → 2 intentos/s sostenidos, o sea los 10.000 PIN de 4 dígitos en 1,4 h.
# Encima cada request dormida ocupa un thread del server: el "freno" era también
# la forma más barata de tumbarlo.
#
# Ahora: pasados unos pocos fallos se le CIERRA LA PUERTA a esa IP por un rato y
# se contesta 429 al instante, sin dormir ni ocupar nada. El castigo escala con la
# reincidencia, así que insistir sale cada vez más caro: 8 intentos por hora contra
# 10.000 combinaciones son ~52 días de ataque continuo — y ruidoso, porque queda
# escrito (ver _log_seguridad).
BLOQUEO_TRAS = 8                       # fallos seguidos antes de cerrar la puerta
ESCALADA = (60, 300, 900, 3600)        # cuánto dura el bloqueo, según reincidencia
LOG_SEGURIDAD = os.path.expanduser("~/Library/Logs/cacho-seguridad.log")

_FALLOS = {}          # ip -> {"n": fallos, "hasta": epoch, "castigos": int}
_FALLOS_LOCK = threading.Lock()


def _pin_igual(recibido, esperado=None):
    """Comparación de tiempo constante: `==` corta en el primer carácter distinto
    y ese tiempo delata cuántos dígitos acertaste."""
    try:
        return hmac.compare_digest(str(recibido).encode("utf-8", "replace"),
                                   (esperado or _pin_actual()).encode())
    except Exception as e:
        print(f"⚠️ comparando el PIN se rompió algo (lo doy por incorrecto): {e}", file=sys.stderr)
        return False


def _log_seguridad(texto):
    """Deja rastro de los intentos. El server no loguea NADA (log_message está
    anulado a propósito, ensuciaba la consola), así que sin esto un ataque de
    días sería completamente invisible."""
    try:
        os.makedirs(os.path.dirname(LOG_SEGURIDAD), exist_ok=True)
        with open(LOG_SEGURIDAD, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {texto}\n")
    except OSError:
        pass


def _bloqueado(ip):
    """Segundos de bloqueo que le quedan a esa IP (0 = puede intentar)."""
    with _FALLOS_LOCK:
        d = _FALLOS.get(ip)
        return max(0.0, d["hasta"] - time.time()) if d else 0.0


def _fallo_pin(ip):
    """Un PIN equivocado. Al octavo, la puerta se cierra por un rato."""
    with _FALLOS_LOCK:
        if len(_FALLOS) > 1000:        # purga de vencidos, que no crezca sin techo
            ahora = time.time()
            for k in [k for k, v in _FALLOS.items() if v["hasta"] < ahora]:
                del _FALLOS[k]
        d = _FALLOS.setdefault(ip, {"n": 0, "hasta": 0.0, "castigos": 0})
        d["n"] += 1
        if d["n"] < BLOQUEO_TRAS:
            return
        dur = ESCALADA[min(d["castigos"], len(ESCALADA) - 1)]
        d.update(n=0, hasta=time.time() + dur, castigos=d["castigos"] + 1)
    _log_seguridad(f"BLOQUEO {ip}: {BLOQUEO_TRAS} PIN equivocados seguidos "
                   f"-> {dur}s de bloqueo (castigo #{d['castigos']})")


def _acierto_pin(ip):
    with _FALLOS_LOCK:
        _FALLOS.pop(ip, None)


# ─── Sesiones web: el PIN se tipea UNA vez, después manda un token ─────────────
# Antes la cookie GUARDABA EL PIN: cada request lo traía, así que "estar adentro"
# y "adivinar la llave" eran el mismo acto. Eso rompía el freno de arriba en el
# caso que importa: por Tailscale (proxy userspace) TODOS los dispositivos llegan
# con la misma IP de origen que localhost, así que un atacante bloqueando esa IP
# dejaba afuera al dueño (verificado: con el PIN correcto contestaba 429).
#
# Ahora acertar el PIN entrega un TOKEN aleatorio de 256 bits. El token no se
# adivina —no necesita freno y nunca frena a nadie— y el PIN, que es corto porque
# se tipea en el celular, solo se usa en el login, que sí está frenado duro.
# De regalo: el PIN deja de viajar en cada request y de quedar guardado en el
# navegador del teléfono.
SESION_DIR = os.path.expanduser("~/Library/Application Support/Cacho")
SESIONES_FILE = os.path.join(SESION_DIR, "sesiones_web.json")
SESION_MAX = 20                  # dispositivos recordados a la vez
SESION_VIDA = 365 * 24 * 3600    # un año sin usarse y se cae solo

_sesiones = {}        # token -> último uso (epoch)
_SES_LOCK = threading.Lock()


def _sesiones_guardar():
    """Persistidas para que reiniciar el server NO obligue a re-tipear el PIN en
    cada dispositivo (el server se reinicia seguido). 0600 desde el os.open."""
    try:
        os.makedirs(SESION_DIR, exist_ok=True)
        tmp = SESIONES_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_sesiones, fh)
        os.replace(tmp, SESIONES_FILE)
    except OSError as e:
        # Sin persistir, el login vale hasta el próximo reinicio del server —
        # que es seguido. Se ve como "Cacho me pide el PIN de nuevo" y no hay
        # forma de adivinar por qué si no queda escrito.
        _log_seguridad(f"no pude guardar sesiones_web.json ({e!r}): los "
                       "dispositivos van a tener que re-tipear el PIN al reiniciar")


def _sesiones_cargar():
    global _sesiones
    try:
        with open(SESIONES_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            ahora = time.time()
            _sesiones = {k: float(v) for k, v in d.items()
                         if isinstance(k, str) and ahora - float(v) < SESION_VIDA}
    except FileNotFoundError:
        _sesiones = {}          # primera vez en esta máquina: normal
    except Exception as e:
        # Arrancar de cero acá ECHA A TODOS los dispositivos: hay que volver a
        # tipear el PIN en el teléfono y en la laptop sin saber por qué. Que al
        # menos quede escrito, que es lo único que lo explica después.
        _sesiones = {}
        _log_seguridad(f"sesiones_web.json ilegible ({e!r}): se cerró la sesión "
                       "de TODOS los dispositivos; hay que re-tipear el PIN")


def _sesion_nueva():
    import secrets
    tok = secrets.token_urlsafe(32)
    with _SES_LOCK:
        _sesiones[tok] = time.time()
        sobran = len(_sesiones) - SESION_MAX
        if sobran > 0:               # se van las más viejas
            for k in sorted(_sesiones, key=_sesiones.get)[:sobran]:
                del _sesiones[k]
        _sesiones_guardar()
    return tok


def _sesion_valida(tok):
    if not tok:
        return False
    with _SES_LOCK:
        for k in _sesiones:
            if hmac.compare_digest(k, tok):
                _sesiones[k] = time.time()
                return True
    return False


# ─── Tickets de un solo uso (para el lanzador de la app) ──────────────────────
# El lanzador abría Chrome con `--app=http://…/?pin=1234`. Eso deja el PIN en la
# LÍNEA DE COMANDOS de Chrome, que cualquier proceso de la máquina lee con `ps`
# mientras la ventana viva — horas. Ahora el lanzador cambia el PIN (que le pasa
# al server por stdin, no por argumento) por un ticket que vale 60 segundos y una
# sola vez: lo que queda a la vista en `ps` es un papel ya quemado.
TICKET_VIDA = 60
_tickets = {}         # ticket -> vencimiento
_TICKET_LOCK = threading.Lock()


def _ticket_nuevo():
    import secrets
    tok = secrets.token_urlsafe(24)
    ahora = time.time()
    with _TICKET_LOCK:
        for k in [k for k, v in _tickets.items() if v < ahora]:
            del _tickets[k]
        _tickets[tok] = ahora + TICKET_VIDA
    return tok


def _ticket_usar(tok):
    """True si el ticket valía. Se quema en el acto: un solo uso."""
    if not tok:
        return False
    ahora = time.time()
    with _TICKET_LOCK:
        for k in list(_tickets):
            if _tickets[k] < ahora:
                del _tickets[k]
            elif hmac.compare_digest(k, tok):
                del _tickets[k]
                return True
    return False


def _sesiones_borrar_todas():
    """Echa a todos los dispositivos (para cuando algo huele mal)."""
    with _SES_LOCK:
        _sesiones.clear()
        _sesiones_guardar()


_sesiones_cargar()


# ---------------------------------------------------------------------------
# Parseo de transcripts (~/.claude/projects/*/<uuid>.jsonl)
# ---------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()

_RE_TAGS = re.compile(
    r"<(local-command-caveat|local-command-stdout|command-name|command-message|"
    r"command-args|system-reminder)>.*?</\1>", re.S)


def _limpiar(texto, tope=170):
    texto = _RE_TAGS.sub(" ", texto)
    texto = re.sub(r"</?[a-z][a-z-]*(\s[^>]*)?>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:tope] + ("…" if len(texto) > tope else "")


def _texto_de(content):
    if isinstance(content, str):
        return content, None
    if isinstance(content, list):
        textos, tools = [], []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                textos.append(b["text"])
            elif b.get("type") == "tool_use":
                tools.append(b.get("name", "?"))
            elif b.get("type") == "tool_result":
                return None, None
        if textos:
            return " ".join(textos), None
        if tools:
            return None, tools
    return None, None


def _parse_linea(line):
    try:
        return json.loads(line)
    except Exception as e:
        print(f"⚠️ línea ilegible en un .jsonl de sesión (la salteo): {e}", file=sys.stderr)
        return None


def _leer_sesion(path):
    st = os.stat(path)
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]

    with open(path, "rb") as fh:
        head = fh.read(CHUNK)
        tail = b""
        if st.st_size > CHUNK:
            fh.seek(max(CHUNK, st.st_size - CHUNK))
            tail = fh.read()

    head_lines = head.decode("utf-8", "replace").splitlines()
    if st.st_size > CHUNK:
        head_lines = head_lines[:-1]
    tail_lines = tail.decode("utf-8", "replace").splitlines()[1:] if tail else []

    s = {
        "id": os.path.basename(path)[:-6],
        "cwd": "", "branch": "", "titulo": "", "primer_msg": "",
        "primer_ts": "", "ultimo_quien": "", "ultimo_txt": "",
        "mtime": st.st_mtime, "auto": False,
    }

    for line in head_lines:
        d = _parse_linea(line)
        if not d:
            continue
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            s["titulo"] = d["aiTitle"]
        if d.get("cwd") and not s["cwd"]:
            s["cwd"] = d["cwd"]
        if d.get("gitBranch") and not s["branch"]:
            s["branch"] = d["gitBranch"]
        if d.get("timestamp") and not s["primer_ts"]:
            s["primer_ts"] = d["timestamp"]
        if t == "user" and not s["primer_msg"] and not d.get("isMeta") and not d.get("isSidechain"):
            txt, _ = _texto_de((d.get("message") or {}).get("content"))
            if txt:
                if "<scheduled-task" in txt:
                    s["auto"] = True
                limpio = _limpiar(txt)
                if limpio:
                    s["primer_msg"] = limpio

    for line in reversed(tail_lines or head_lines):
        d = _parse_linea(line)
        if not d:
            continue
        if d.get("type") == "ai-title" and d.get("aiTitle") and not s["titulo"]:
            s["titulo"] = d["aiTitle"]
        # El cwd puede NO estar en el head: los transcripts largos arrancan con una
        # línea `file-history-snapshot` de varios MB y el primer "cwd" cae fuera de
        # los 256 KB de cabeza (visto 16-ago-2026 en uno de 5,5 MB: el primer cwd
        # estaba en el byte 3.376.892). Sin cwd, ⟳ Retomar contestaba "la carpeta de
        # esa sesión ya no existe" con la carpeta ahí, y la sesión no podía
        # emparejarse con su proceso vivo. La cola sí lo trae, en cada mensaje.
        if d.get("cwd") and not s["cwd"]:
            s["cwd"] = d["cwd"]
        if d.get("gitBranch") and not s["branch"]:
            s["branch"] = d["gitBranch"]
        if s["ultimo_quien"]:
            continue
        if d.get("type") in ("user", "assistant") and not d.get("isMeta") and not d.get("isSidechain"):
            txt, tools = _texto_de((d.get("message") or {}).get("content"))
            if txt:
                limpio = _limpiar(txt)
                if limpio:
                    s["ultimo_quien"] = "Claude" if d["type"] == "assistant" else "Vos"
                    s["ultimo_txt"] = limpio
            elif tools:
                s["ultimo_quien"] = "Claude"
                s["ultimo_txt"] = "⚙ " + ", ".join(dict.fromkeys(tools))

    if not (s["primer_msg"] or s["titulo"]):
        resultado = None
    else:
        if not s["titulo"]:
            s["titulo"] = s["primer_msg"]
        resultado = s

    with _cache_lock:
        _cache[path] = (st.st_mtime, st.st_size, resultado)
    return resultado


def _sesiones_parseadas():
    """Sesiones de hoy según los transcripts (copias mutables)."""
    medianoche = datetime.now().replace(hour=0, minute=0, second=0,
                                        microsecond=0).timestamp()
    out = []
    for carpeta in os.listdir(PROJECTS_DIR):
        d = os.path.join(PROJECTS_DIR, carpeta)
        if not os.path.isdir(d):
            continue
        try:
            nombres = os.listdir(d)
        except PermissionError:
            continue
        for n in nombres:
            if not n.endswith(".jsonl") or n.startswith("agent-"):
                continue
            p = os.path.join(d, n)
            try:
                if os.stat(p).st_mtime < medianoche:
                    continue
            except OSError:
                continue
            s = _leer_sesion(p)
            if s:
                out.append(dict(s))
    return out


def _buscar_transcript(sid):
    """Path del .jsonl de una sesión por id, o None. sid ya viene validado."""
    for carpeta in os.listdir(PROJECTS_DIR):
        p = os.path.join(PROJECTS_DIR, carpeta, sid + ".jsonl")
        if os.path.isfile(p):
            return p
    return None


def _limpiar_multilinea(texto, tope=6000):
    """Como _limpiar pero conservando los saltos de línea (para el visor)."""
    texto = _RE_TAGS.sub(" ", texto)
    texto = re.sub(r"</?[a-z][a-z-]*(\s[^>]*)?>", " ", texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    return texto[:tope] + ("…" if len(texto) > tope else "")


_cache_conv = {}
_conv_lock = threading.Lock()


def _leer_conversacion(path, max_msgs=150):
    """Conversación de un transcript para el visor de solo-lectura.

    Lee la COLA del archivo. Cuánta cola NO puede ser fija: los transcripts largos
    traen líneas sueltas de varios MB (los `file-history-snapshot`), así que con el
    1 MB de antes una sesión de 40 MB mostraba TRES mensajes y parecía vacía
    (medido 16-ago-2026: 44 MB → 3 mensajes). Ahora la ventana se agranda hasta
    juntar conversación de verdad. El resultado se cachea por (mtime, tamaño):
    el visor se repinta cada 4 s y una sesión terminada ya no cambia nunca."""
    st = os.stat(path)
    clave = (path, st.st_mtime, st.st_size)
    with _conv_lock:
        hit = _cache_conv.get(path)
        if hit and hit[0] == clave:
            return hit[1]

    titulo, items, recortado = "", [], False
    for tope in (1024 * 1024, 8 * 1024 * 1024, 32 * 1024 * 1024):
        titulo, items = _parsear_cola(path, st, tope)
        recortado = st.st_size > tope
        if len(items) >= 12 or not recortado:
            break
    if len(items) > max_msgs:
        items = items[-max_msgs:]
    salida = {"titulo": titulo, "items": items, "mtime": st.st_mtime,
              "recortado": recortado}
    with _conv_lock:
        if len(_cache_conv) > 40:
            _cache_conv.clear()
        _cache_conv[path] = (clave, salida)
    return salida


def _parsear_cola(path, st, TOPE):
    with open(path, "rb") as fh:
        if st.st_size > TOPE:
            fh.seek(st.st_size - TOPE)
        data = fh.read()
    lines = data.decode("utf-8", "replace").splitlines()
    if st.st_size > TOPE:
        lines = lines[1:]  # la primera puede venir cortada

    titulo, items = "", []
    for line in lines:
        d = _parse_linea(line)
        if not d:
            continue
        if d.get("type") == "ai-title" and d.get("aiTitle"):
            titulo = d["aiTitle"]
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        txt, tools = _texto_de((d.get("message") or {}).get("content"))
        ts = (d.get("timestamp") or "")[11:16]  # HH:MM
        if txt:
            limpio = _limpiar_multilinea(txt)
            if limpio:
                items.append({"q": "Claude" if d["type"] == "assistant" else "Vos",
                              "t": limpio, "ts": ts, "tool": False})
        elif tools and d["type"] == "assistant":
            nombres = ", ".join(dict.fromkeys(tools))
            # herramientas seguidas se colapsan en una sola línea ⚙
            if items and items[-1]["tool"]:
                previos = items[-1]["t"].split(", ")
                items[-1]["t"] = ", ".join(dict.fromkeys(previos + nombres.split(", ")))
                items[-1]["ts"] = ts
            else:
                items.append({"q": "Claude", "t": nombres, "ts": ts, "tool": True})
    return titulo, items


def _proyecto_lindo(cwd):
    if not cwd:
        return "—"
    home = os.path.expanduser("~")
    if cwd == home:
        return "Home"
    if cwd.startswith("/private/var") or cwd.startswith("/var/folders"):
        return "Temporal"
    return os.path.basename(cwd.rstrip("/")) or cwd


def _iso_a_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception as e:
        print(f"⚠️ timestamp raro en sesión ({ts!r}), lo tomo como 0: {e}", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# Procesos `claude` vivos fuera de la app
# ---------------------------------------------------------------------------

_proc_cache = {}  # pid -> {tty, cwd, start}; tty/cwd/start no cambian en vida del proceso


def procesos_claude(ttys_excluidos):
    try:
        # timeout en los tres: sin él, un `lsof` colgado (pasa con volúmenes de red
        # dormidos) se lleva puesto el thread que atiende /api/estado y la barra
        # lateral queda muda sin decir por qué
        pids = {int(p) for p in subprocess.run(
            ["pgrep", "-x", "claude"], capture_output=True, text=True,
            timeout=5).stdout.split()}
    except Exception as e:
        print(f"⚠️ pgrep de procesos claude falló (la barra lateral queda sin externos): {e}", file=sys.stderr)
        return []
    for pid in list(_proc_cache):
        # pop y no `del`: /api/estado la piden varios clientes a la vez (cada
        # pestaña refresca cada 4 s) y dos threads acá adentro con el mismo pid
        # muerto hacían que el segundo se comiera un KeyError -> 500 y la barra
        # lateral muda ese ciclo, sin explicación.
        if pid not in pids:
            _proc_cache.pop(pid, None)
    procs = []
    for pid in pids:
        info = _proc_cache.get(pid)
        if not info:
            try:
                salida = subprocess.run(
                    ["ps", "-o", "tty=,lstart=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                if not salida:
                    continue
                tty, lstart = salida.split(None, 1)
                start = time.mktime(time.strptime(lstart.strip(),
                                                  "%a %b %d %H:%M:%S %Y"))
                lsof = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                      capture_output=True, text=True, timeout=5).stdout
                cwd = ""
                for line in lsof.splitlines():
                    if line.startswith("n"):
                        cwd = line[1:]
                        break
                info = {"tty": tty, "cwd": cwd, "start": start}
                _proc_cache[pid] = info
            except Exception as e:
                print(f"⚠️ no pude leer ps/lsof del proceso claude {pid} (lo salteo): {e}", file=sys.stderr)
                continue
        if info["tty"] in ttys_excluidos:
            continue  # es una pestaña nuestra
        procs.append({"pid": pid, **info})
    return procs


# ---------------------------------------------------------------------------
# Terminales de la app (PTY + zsh + claude)
# ---------------------------------------------------------------------------

MODELO_PATH = os.path.expanduser("~/.cacho_modelo")
_MODELO_OK = re.compile(r"^[A-Za-z0-9._\[\]-]{1,64}$")


def _modelo_preferido():
    """Modelo con el que arrancan las pestañas nuevas, o "" para el default.

    Lo escribe `tools/vigia_fable.py`. Se relee en cada pestaña a propósito: el vigía
    puede cambiarlo con el server ya andando. Si el contenido tiene cualquier cosa rara
    se ignora — este texto termina en una línea de comando.
    """
    try:
        with open(MODELO_PATH) as f:
            modelo = f.read().strip()
    except Exception as e:
        print(f"⚠️ no pude leer el modelo preferido ({MODELO_PATH}), uso el default: {e}", file=sys.stderr)
        return ""
    return modelo if _MODELO_OK.match(modelo) else ""


class TermSession:
    def __init__(self, cwd, resume_id=""):
        self.id = uuid.uuid4().hex[:8]
        self.cwd = cwd
        self.creado = time.time()
        self.last_out = time.time()
        self.buf = bytearray()
        self.subs = []
        self.lock = threading.Lock()
        # retomar una charla terminada: claude --resume sigue en el MISMO
        # transcript, así que se vincula de entrada (título y estado al toque)
        self.resume_id = resume_id
        self.transcript_id = resume_id
        self.titulo = ""

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE")}  # sin herencia de Claude Code:
        # si el server se levantó desde una sesión de Claude, esas variables
        # harían que las pestañas arranquen como "sesión hija" sin transcript
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        master, slave = os.openpty()
        self.master = master
        self.tty = os.ttyname(slave)  # /dev/ttysNNN

        TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x20007461)

        def preexec():
            os.setsid()
            fcntl.ioctl(slave, TIOCSCTTY, 0)

        # shell interactiva de login: mismas alias/entorno que su Terminal
        self.proc = subprocess.Popen(
            ["/bin/zsh", "-il"], cwd=cwd, env=env,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=preexec, close_fds=True)
        os.close(slave)
        threading.Thread(target=self._leer, daemon=True).start()
        threading.Thread(target=self._auto_claude, daemon=True).start()

    def _auto_claude(self):
        time.sleep(0.9)  # que la shell levante el prompt
        # `claude` puede no estar en el PATH de la shell nueva (el instalador
        # oficial lo deja en ~/.local/bin y el de npm en ~/.npm-global/bin, como
        # en la MacBook). Si tampoco está ahí, decirlo en pantalla en vez de
        # dejar la pestaña negra.
        # resume_id ya viene validado ([0-9a-f-]): seguro para la línea de comando
        claude = ("claude --resume " + self.resume_id) if self.resume_id else "claude"
        # Si existe ~/.cacho_modelo, las pestañas nuevas arrancan con ese modelo. Sirve
        # para que un vigía externo cambie de modelo solo (p. ej. bajar a uno más barato
        # cuando se agota el cupo del más caro, y volver cuando se renueva) sin tocar el
        # server. Sin archivo, el comportamiento es el de siempre: el default de la máquina.
        modelo = _modelo_preferido()
        if modelo:
            # entre comillas SIEMPRE: el sufijo de contexto va entre corchetes
            # (`claude-opus-5[1m]`) y zsh lo toma como glob → "no matches found".
            claude += " --model '" + modelo + "'"
        cmd = (
            'PATH="$HOME/.local/bin:$HOME/.claude/local:$HOME/.npm-global/bin:'
            '/opt/homebrew/bin:/usr/local/bin:$PATH"; '
            f"if command -v claude >/dev/null; then {claude}; "
            "else echo '>> Falta Claude Code en esta maquina. Instalalo con:'; "
            "echo '>>   curl -fsSL https://claude.ai/install.sh | bash'; fi"
        )
        try:
            os.write(self.master, cmd.encode() + b"\r")
        except OSError as e:
            msg = (f"\r\n>> No pude arrancar claude en esta pestaña ({e})."
                   "\r\n>> Cerrala y abri otra.\r\n").encode()
            with self.lock:
                self.buf += msg
                for q in list(self.subs):
                    q.put(msg)

    def _leer(self):
        while True:
            try:
                d = os.read(self.master, 65536)
            except OSError:
                d = b""
            if not d:
                break
            with self.lock:
                self.buf += d
                if len(self.buf) > TOPE_BUFFER:
                    del self.buf[: len(self.buf) - TOPE_BUFFER]
                self.last_out = time.time()
                for q in list(self.subs):
                    q.put(d)
        with self.lock:
            for q in list(self.subs):
                q.put(None)

    @property
    def viva(self):
        return self.proc.poll() is None

    def escribir(self, data):
        os.write(self.master, data)

    def resize(self, cols, rows):
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def matar(self):
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=1)   # cosecharlo acá: si no queda de zombi
        except Exception:               # hasta que se cree otra pestaña
            pass
        try:
            os.close(self.master)
        except OSError:
            pass

    def soltar(self):
        """Suelta lo que ocupa una pestaña YA muerta: el scrollback (hasta 2 MB),
        el fd del pty y el proceso sin cosechar. No mata nada — se llama justamente
        cuando la shell ya murió sola (ver _cosechar_tabs)."""
        try:
            self.proc.wait(timeout=0)
        except Exception:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        with self.lock:
            self.buf = bytearray()
            for q in list(self.subs):
                q.put(None)
            self.subs = []


TABS = {}          # id -> TermSession
TABS_LOCK = threading.Lock()
MAX_TABS = 30      # techo de pestañas simultáneas (ver /api/term/new)
_mapeo_pid = {}    # transcript_id -> pid del proceso claude que le corresponde

# ─── Metadatos por sesión: FIJADA / ORDEN / NOMBRE PROPIO ───
# Primer estado persistente de Cacho: hasta ahora TODO era memoria y se perdía al
# reiniciar el server. Va FUERA del repo (es preferencia de máquina, no dato del negocio).
#
# La clave es el transcript_id (el UUID del .jsonl), NO el id de pestaña: ese es
# uuid4()[:8] nuevo en cada arranque del server, así que el pineo no sobreviviría a un
# reinicio. Ojo: una pestaña recién creada no tiene transcript_id hasta que
# `_vincular_transcripts` la empareja (~1-5 s); hasta entonces simplemente no tiene meta.
META_DIR = os.path.expanduser("~/Library/Application Support/Cacho")
META_FILE = os.path.join(META_DIR, "sesiones.json")
_meta_cache = {"mtime": 0, "datos": {}}
META_LOCK = threading.Lock()


def _meta_leer():
    """Metadatos {sid: {"fija": bool, "orden": int, "nombre": str}}. Cacheado por mtime:
    /api/estado se pide cada 4 s por cliente, no se relee el disco cada vez."""
    with META_LOCK:
        try:
            m = os.path.getmtime(META_FILE)
        except OSError:
            _meta_cache.update(mtime=0, datos={})
            return {}
        if m != _meta_cache["mtime"]:
            try:
                with open(META_FILE, encoding="utf-8") as fh:
                    d = json.load(fh)
                _meta_cache.update(mtime=m, datos=d if isinstance(d, dict) else {})
            except Exception as e:
                # archivo corrupto: no tumba Cacho, pero se pierden las fijadas y
                # los nombres propios. Antes desaparecían en silencio y parecía un
                # bug de la barra; ahora queda escrito. (Solo se loguea cuando el
                # mtime cambia, así que no puede spamear cada 4 s.)
                _meta_cache.update(mtime=m, datos={})
                _log_seguridad(f"sesiones.json ilegible ({e!r}): se perdieron las "
                               "sesiones fijadas y los nombres propios")
        return dict(_meta_cache["datos"])


def _meta_set(sid, campos):
    """Aplica cambios de UNA sesión y persiste. Escritura atómica (tmp + replace) bajo
    lock: el server es multi-thread y dos pestañas pueden guardar a la vez."""
    with META_LOCK:
        datos = {}
        try:
            with open(META_FILE, encoding="utf-8") as fh:
                datos = json.load(fh)
            if not isinstance(datos, dict):
                datos = {}
        except FileNotFoundError:
            pass                 # todavía no hay nada guardado: normal
        except Exception as e:
            # Acá NO alcanza con seguir de largo: arrancar de {} y guardar BORRA
            # las fijadas y los nombres propios de todas las demás sesiones, en
            # silencio y para siempre. Se aparta el archivo ilegible antes de
            # pisarlo (así se puede recuperar a mano) y queda escrito.
            datos = {}
            try:
                os.replace(META_FILE, META_FILE + ".corrupto")
            except OSError:
                pass
            _log_seguridad(f"sesiones.json ilegible al guardar ({e!r}): se apartó "
                           f"como {os.path.basename(META_FILE)}.corrupto y se "
                           "empezó de cero (se pierden fijadas y nombres)")
        actual = dict(datos.get(sid) or {})
        for k, v in campos.items():
            if v is None:
                actual.pop(k, None)          # None = volver al valor por defecto
            else:
                actual[k] = v
        if actual:
            datos[sid] = actual
        else:
            datos.pop(sid, None)             # sin nada que recordar, no dejar basura
        os.makedirs(META_DIR, exist_ok=True)
        tmp = META_FILE + ".tmp"
        # 0600 desde el os.open, igual que el resto del estado de Cacho: acá
        # quedan los nombres que se le ponen a las sesiones, y eso dice en qué
        # anda uno. Antes salía con el umask (legible por cualquier usuario).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, META_FILE)
        _meta_cache.update(mtime=os.path.getmtime(META_FILE), datos=datos)
        return actual


def _aplicar_meta(item, sid, meta):
    """Agrega a un item de la lista sus campos de meta. El NOMBRE PROPIO gana sobre el
    título del transcript — que si no lo pisaría en la próxima lectura, cada 4 s."""
    m = meta.get(sid) or {}
    item["sid"] = sid or ""
    item["fija"] = bool(m.get("fija"))
    item["orden"] = m.get("orden")
    if m.get("nombre"):
        item["titulo"] = m["nombre"]
        item["renombrada"] = True
    return item


def _vincular_transcripts(tabs, parseadas):
    """Asocia cada pestaña de la app con su transcript (título y estado).

    El transcript nace ~1 s después de que la pestaña lanza `claude`, así que
    la señal firme es primer_ts ≈ t.creado. Antes se elegía max(mtime) entre
    los candidatos y, con varias pestañas del mismo proyecto, la pestaña más
    vieja sin vincular le robaba el transcript a una más nueva (títulos
    entreverados, y el cruce quedaba pegado). Ahora el emparejado es global:
    gana el par (pestaña, transcript) con menor |primer_ts − creado|.
    """
    por_id = {p["id"]: p for p in parseadas}
    usadas = set()
    pendientes = []
    for t in tabs:
        if t.transcript_id and t.transcript_id in por_id:
            usadas.add(t.transcript_id)
        elif not t.transcript_id:
            pendientes.append(t)
        # transcript_id seteado pero sin parsear hoy (p. ej. resume de una
        # charla vieja sin actividad aún): se respeta, no se re-adivina

    pares = []
    for t in pendientes:
        for p in parseadas:
            if p["cwd"] != t.cwd or p["id"] in usadas:
                continue
            delta = _iso_a_epoch(p["primer_ts"]) - t.creado
            if delta >= -60:
                pares.append((abs(delta), t.creado, t, p))
    pares.sort(key=lambda x: (x[0], x[1]))
    vinculadas = set()
    for _, _, t, p in pares:
        if id(t) in vinculadas or p["id"] in usadas:
            continue
        t.transcript_id = p["id"]
        usadas.add(p["id"])
        vinculadas.add(id(t))

    for t in tabs:
        p = por_id.get(t.transcript_id)
        if p:
            t.titulo = p["titulo"]
    return usadas


def _cosechar_tabs(ahora, gracia=900):
    """Saca de TABS las pestañas cuya shell murió hace rato.

    Antes NADA las sacaba: la única salida era la ✕ (que llama /kill). Una pestaña
    donde escribías `exit`, o cuya shell se cayó, quedaba en memoria para siempre
    con su scrollback (hasta 2 MB), su fd de pty y su proceso sin cosechar — y en
    la barra, ocupando lugar como «terminada» eternamente.
    La charla NO se pierde: al soltar la pestaña su transcript deja de estar
    "usado" y reaparece abajo, en «Terminadas hoy», retomable con ⟳.
    Los 15 minutos de gracia son para que se vea que terminó antes de que se vaya."""
    with TABS_LOCK:
        muertas = [(tid, t) for tid, t in TABS.items()
                   if not t.viva and (ahora - t.last_out) > gracia]
        for tid, _ in muertas:
            TABS.pop(tid, None)
    for _, t in muertas:
        t.soltar()


def estado_general():
    ahora = time.time()
    _cosechar_tabs(ahora)
    parseadas = _sesiones_parseadas()
    meta = _meta_leer()
    with TABS_LOCK:
        tabs = list(TABS.values())
    usadas = _vincular_transcripts(tabs, parseadas)

    tabs_json = []
    for t in sorted(tabs, key=lambda x: x.creado):
        p = next((x for x in parseadas if x["id"] == t.transcript_id), None)
        quieto = ahora - t.last_out
        if not t.viva:
            estado = "terminada"
        elif p:
            # Dos señales, y se piden LAS DOS para decir "trabajando": el
            # transcript (que tarda hasta un minuto en enfriarse) y el pty. La
            # TUI redibuja su spinner mientras piensa, así que pty quieto ≥8 s
            # = terminó o está pidiendo permiso. Sin esto el aviso de "te
            # espera" llegaba hasta un minuto tarde (o nunca, si la TUI quedó
            # frenada en una pregunta con el transcript recién escrito).
            estado = ("trabajando"
                      if (ahora - p["mtime"]) < 60 and quieto < 8 else "esperando")
        else:
            estado = "trabajando" if quieto < 6 else "esperando"
        tabs_json.append(_aplicar_meta({
            "id": t.id, "cwd": t.cwd, "proyecto": _proyecto_lindo(t.cwd),
            "titulo": t.titulo or "Nueva sesión", "estado": estado,
            "viva": t.viva, "quieto_seg": int(quieto),
            "hace_seg": int(ahora - (p["mtime"] if p else t.last_out)),
            "ultimo_quien": p["ultimo_quien"] if p else "",
            "ultimo_txt": p["ultimo_txt"] if p else "",
        }, t.transcript_id, meta))

    ttys_mios = {t.tty.replace("/dev/", "") for t in tabs}
    procs = procesos_claude(ttys_mios)
    afuera = [p for p in parseadas if p["id"] not in usadas]

    # Emparejado ESTABLE sesión<->proceso. Antes era solo por cwd y el transcript
    # más reciente le "robaba" el proceso a otra sesión de la misma carpeta:
    # cerrabas una Terminal y en la app seguía figurando viva.
    vivos = {pr["pid"]: pr for pr in procs}
    for sid in list(_mapeo_pid):
        if _mapeo_pid[sid] not in vivos:
            del _mapeo_pid[sid]  # ese proceso murió: la sesión quedó terminada
    for s in afuera:
        s.update({"viva": False, "tty": "", "pid": None})
    asignados = set()
    # 1) sesiones que ya tienen su proceso conocido
    for s in afuera:
        pid = _mapeo_pid.get(s["id"])
        if pid and pid in vivos and pid not in asignados and vivos[pid]["cwd"] == s["cwd"]:
            s.update({"viva": True, "tty": vivos[pid]["tty"], "pid": pid})
            asignados.add(pid)
    # 2) procesos nuevos: a la sesión libre de la misma carpeta cuyo inicio
    #    más se acerque al arranque del proceso
    for pid, pr in vivos.items():
        if pid in asignados:
            continue
        cands = [s for s in afuera if not s["viva"] and s["cwd"] == pr["cwd"]]
        if not cands:
            continue
        s = min(cands, key=lambda x: abs(_iso_a_epoch(x["primer_ts"]) - pr["start"]))
        s.update({"viva": True, "tty": pr["tty"], "pid": pid})
        _mapeo_pid[s["id"]] = pid
        asignados.add(pid)
    for s in afuera:
        if s["viva"]:
            s["tipo"] = "terminal" if (s["tty"] and s["tty"] != "??") else "app"
            s["estado"] = "trabajando" if (ahora - s["mtime"]) < 60 else "esperando"
        else:
            s["tipo"] = "terminal"
            s["estado"] = "terminada"
        if s["auto"] or _proyecto_lindo(s["cwd"]) == "Temporal":
            s["tipo"] = "automatica"
        s["proyecto"] = _proyecto_lindo(s["cwd"])
        s["hace_seg"] = int(ahora - s["mtime"])
        s.pop("mtime", None)

    for s in afuera:
        _aplicar_meta(s, s["id"], meta)

    orden = {"trabajando": 0, "esperando": 1, "terminada": 2}
    afuera.sort(key=lambda s: (orden[s["estado"]], s["hace_seg"]))

    return {"tabs": tabs_json, "afuera": afuera, "proyectos": proyectos()}


def proyectos():
    lista = [{"nombre": "Home", "cwd": os.path.expanduser("~")}]
    if os.path.isdir(CARPETA_PROYECTOS):
        for n in sorted(os.listdir(CARPETA_PROYECTOS)):
            d = os.path.join(CARPETA_PROYECTOS, n)
            if os.path.isdir(d) and not n.startswith((".", "_")):
                lista.append({"nombre": n, "cwd": d})
    else:
        # máquina sin ~/Claude/Projects (p. ej. otra persona): carpetas comunes
        for n in ("Desktop", "Documents", "Projects"):
            d = os.path.join(os.path.expanduser("~"), n)
            if os.path.isdir(d):
                lista.append({"nombre": n, "cwd": d})
    return lista


# ---------------------------------------------------------------------------
# Saltar a ventanas de Terminal de afuera
# ---------------------------------------------------------------------------

def abrir_terminal(tty):
    if not re.fullmatch(r"ttys\d{3}", tty or ""):
        return False, "tty inválido"
    dev = "/dev/" + tty
    script_terminal = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is "{dev}" then
                    set selected of t to true
                    set index of w to 1
                    activate
                    return "ok"
                end if
            end repeat
        end repeat
    end tell
    return "no"'''
    script_iterm = f'''
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with sess in sessions of t
                    if tty of sess is "{dev}" then
                        select t
                        select w
                        activate
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "no"'''
    for script in (script_terminal, script_iterm):
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=10)
            if r.stdout.strip() == "ok":
                return True, "listo"
        except Exception as e:
            print(f"⚠️ osascript para traer la pestaña al frente falló (pruebo la otra app): {e}", file=sys.stderr)
            continue
    return False, "no encontré esa pestaña (¿Terminal cerrada?)"


def abrir_app_claude():
    try:
        subprocess.run(["osascript", "-e", 'tell application "Claude" to activate'],
                       capture_output=True, text=True, timeout=10)
        return True, "listo"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _es_local(nombre):
    """Anti DNS-rebinding (mejora que vino de la Cacho de Lu, 13-ago-2026):
    una web maliciosa abierta en el navegador puede hacer que su dominio
    resuelva a 127.0.0.1 y hablarle a este server "desde adentro". En ese
    ataque el header Host/Origin SIEMPRE trae el dominio del atacante, así
    que alcanza con exigir que el nombre sea de esta máquina.
    OJO: acá no vale rechazar todo lo que no sea 127.0.0.1 — se entra
    también por Tailscale (userspace proxy) desde el celular/laptop. Por eso:
    - IP literal (127.0.0.1, 100.x de Tailscale, LAN): vale SIEMPRE — una IP
      no se puede "rebindear", el navegador ya conectó a esa IP.
    - Nombres: solo localhost, MagicDNS (*.ts.net), mDNS (*.local) o nombres
      sin punto (un dominio público del atacante necesita un punto sí o sí).

    `.local` se agregó el 16-ago-2026: desde el celular/laptop se entra por el
    nombre Bonjour de la máquina (`equipo.local:8811`) y eso caía en "nombre con
    punto que no es .ts.net" ⇒ 403 `host no permitido` — con la pantalla del PIN
    fuera de alcance, o sea imposible de entrar y sin ninguna pista de por qué.
    No abre la puerta: `.local` es un TLD reservado a mDNS, no se registra ni se
    resuelve en el DNS público, así que no sirve para el ataque que esto tapa
    (una web de internet que apunta su dominio a 127.0.0.1)."""
    if not nombre:
        return False
    nombre = nombre.strip().lower().rstrip(".")   # el punto final del FQDN es legal
    if not nombre:                     # "." o "..": queda vacío al sacarle el punto y, sin
        return False                   # esta línea, caía en "no tiene punto" ⇒ aceptado
    if nombre.endswith((".ts.net", ".local")) or "." not in nombre:
        return True  # localhost, mac-mini, nombre.ts.net, nombre.local…
    try:
        ipaddress.ip_address(nombre)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _cookie_pendiente = None   # token a entregar en esta respuesta (login por ?pin=)

    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._cookie_pendiente:
            self.send_header("Set-Cookie",
                             self._COOKIE_SESION.format(tok=self._cookie_pendiente))
            self._cookie_pendiente = None
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._body_leido = True
        return self.rfile.read(n) if n else b""

    def _rebinding(self, origin_null_ok=False):
        """True si el Host u Origin delatan una web ajena (ver _es_local).

        Anota en el log de seguridad QUÉ nombre rechazó (16-ago-2026): este 403 es
        el único que se le puede aparecer al dueño legítimo entrando por un nombre
        que la lista no contempla, y sin el nombre a la vista queda un
        `{"error": "host no permitido"}` pelado que se diagnostica a ciegas. Con
        `_una_vez` para que un bot machacando no escriba un log de gigas."""
        host = self.headers.get("Host")
        if host and not _es_local(urlparse("//" + host).hostname or ""):
            self._log_rebinding("Host", host)
            return True
        origin = self.headers.get("Origin")
        if origin == "null" and origin_null_ok:
            # SOLO en la ruta del login, y solo el literal "null" (un dominio ajeno
            # sigue rechazado por el Host y por acá). Cinturón sobre el arreglo de
            # `_pagina_pin`: si mañana otro navegador vuelve a mandar `Origin: null`
            # al mandar el formulario, el dueño entra igual en vez de comerse un 403
            # que no puede diagnosticar desde el teléfono. No regala nada: para que
            # este POST sirva de algo hay que traer el PIN correcto —y quien lo tiene
            # ya puede entrar por la puerta— y los 8 intentos del freno siguen rigiendo.
            return False
        if origin and not _es_local(urlparse(origin).hostname or ""):
            self._log_rebinding("Origin", origin)
            return True  # cubre también Origin: null (hostname vacío)
        return False

    def _rechazo_host(self):
        """El 403 del anti-rebinding, EXPLICADO si lo está viendo una persona.

        Antes contestaba `{"error": "host no permitido"}` a todo. Para un navegador eso es
        una pantalla negra con una línea de JSON: no dice qué nombre se rechazó, ni que la
        puerta está sana y el problema es la dirección con la que se golpeó. Al dueño
        entrando desde el teléfono lo deja adivinando (pasó el 16-ago-2026). A un script se
        le sigue contestando el JSON de siempre — no rompe a nadie que ya lo lea.
        Mostrar el nombre recibido no regala nada: lo eligió quien hizo la request."""
        if "text/html" not in (self.headers.get("Accept") or ""):
            return self._json({"error": "host no permitido"}, 403)
        host = html_escape((self.headers.get("Host") or "(sin Host)")[:120])
        cuerpo = (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>body{background:#11151c;color:#e7ecf3;font:16px/1.5 -apple-system,"
            "system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh}"
            "div{max-width:34rem;padding:2rem}code{background:#1c2430;padding:.15em .4em;"
            "border-radius:4px;word-break:break-all}h1{font-size:1.25rem;margin:0 0 .75rem}"
            "p{color:#9fb0c4}</style><div>"
            "<h1>No se entra por esta dirección</h1>"
            f"<p>Golpeaste con el nombre <code>{host}</code>, y solo se aceptan la IP, "
            "<code>localhost</code>, los nombres de la red privada "
            "(<code>.ts.net</code>, <code>.local</code>) o un nombre sin puntos. Es la "
            "defensa contra que una web de internet le hable a este servidor por vos.</p>"
            "<p>No es el PIN ni el servidor: está todo bien de este lado. Entrá por la "
            "dirección de siempre.</p></div>").encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._seguridad()
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    _REBIND_VISTOS = set()          # (cabecera, valor) ya anotados; tope abajo
    _REBIND_LOCK = threading.Lock()

    def _log_rebinding(self, cabecera, valor):
        clave = (cabecera, valor[:120])
        with self._REBIND_LOCK:
            if clave in self._REBIND_VISTOS:
                return
            if len(self._REBIND_VISTOS) > 200:
                self._REBIND_VISTOS.clear()
            self._REBIND_VISTOS.add(clave)
        _log_seguridad(f"RECHAZO por {cabecera} ajeno: {clave[1]!r} (ip {self._ip})")

    # --- Puerta: token de sesión (adentro) vs PIN (login) -------------------
    _COOKIE_SESION = ("cacho_sesion={tok}; Path=/; Max-Age=31536000; "
                      "SameSite=Lax; HttpOnly")

    @property
    def _ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _cookie(self, nombre):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            parte = parte.strip()
            if parte.startswith(nombre + "="):
                return parte[len(nombre) + 1:]
        return ""

    def _adentro(self):
        """True si la request trae un token de sesión válido. Este camino NO pasa
        por el freno: un token de 256 bits no se adivina, y frenarlo sería dejar
        afuera al dueño cada vez que alguien golpea la puerta."""
        return _sesion_valida(self._cookie("cacho_sesion"))

    def _pin_ok(self):
        """True si la request trae credencial de login válida por la URL: el PIN
        (?pin=) o un ticket de un solo uso (?t=, el que usa el lanzador). Cuenta
        como intento: si está mal, suma al freno."""
        q = parse_qs(urlparse(self.path).query)
        if q.get("t"):
            if _ticket_usar(q["t"][0]):
                _acierto_pin(self._ip)
                return True
            _fallo_pin(self._ip)
            return False
        if not q.get("pin"):
            return False
        if _pin_igual(q["pin"][0]):
            _acierto_pin(self._ip)
            return True
        _fallo_pin(self._ip)
        return False

    def _seguridad(self, referrer="no-referrer"):
        """Cabeceras de endurecimiento de la página. Cuestan cero y tapan familias
        enteras de ataque: nada de esta app se carga de afuera ni tiene por qué
        vivir dentro del iframe de otro sitio.

        `referrer` es parámetro por UNA página: la del PIN (ver `_pagina_pin`), que
        con `no-referrer` se dejaba afuera a sí misma."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "object-src 'none'; frame-ancestors 'none'")

    def _pagina(self):
        cuerpo = PAGINA.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._seguridad()
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _redirigir_sin_pin(self, tok):
        """303 a la misma ruta sin el ?pin=, entregando el token de sesión."""
        u = urlparse(self.path)
        q = {k: v for k, v in parse_qs(u.query).items() if k != "pin"}
        destino = u.path + ("?" + urlencode(q, doseq=True) if q else "")
        self.send_response(303)
        self.send_header("Location", destino or "/")
        self.send_header("Set-Cookie", self._COOKIE_SESION.format(tok=tok))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _bloqueo(self, espera):
        """429 INSTANTÁNEO. Clave: no dormir. Dormir la request no frena a nadie
        (el atacante las lanza en paralelo) y le regala un thread por intento."""
        cuerpo = json.dumps({"error": "demasiados PIN equivocados",
                             "reintentar_en_seg": int(espera) + 1}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Retry-After", str(int(espera) + 1))
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _pagina_pin(self, error=False):
        aviso = ('<div class="err">PIN incorrecto</div>' if error else "")
        cuerpo = PAGINA_PIN.replace("{{AVISO}}", aviso).encode()
        self.send_response(403 if error else 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # Las MISMAS cabeceras que el resto de la app (16-ago-2026): esta pantalla
        # se estaba sirviendo pelada, y es la única que ve alguien que todavía no
        # entró — o sea, justo la que pide la llave de la máquina. Sin
        # X-Frame-Options una web ajena la mete en un iframe (el Host es una IP
        # literal, que _es_local acepta a propósito, y una navegación de iframe no
        # manda Origin) y le dibuja encima lo que quiera para que el PIN se tipee
        # ahí. Verificado con curl: no salía ni una cabecera de seguridad.
        #
        # PERO el `no-referrer` del resto NO va acá (16-ago-2026, «sigue sin entrar
        # Cacho en el celular»): esta pantalla es la única que manda un FORMULARIO, y
        # por la spec de Fetch, en una navegación que no es GET con la política
        # `no-referrer` el navegador manda `Origin: null` — que el anti-rebinding de
        # más arriba rechaza. Resultado: tipeás el PIN bien y te contesta 403
        # `host no permitido`, o sea la puerta trabada con la llave puesta. No se ve
        # desde la propia máquina porque ahí ya está la cookie y esta pantalla nunca
        # aparece; se ve SOLO desde un dispositivo nuevo, que es cuando más molesta.
        # `same-origin` protege lo mismo hacia afuera (a otro sitio no le llega nada)
        # y deja que el formulario diga de dónde viene.
        self._seguridad(referrer="same-origin")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    # Lo ÚNICO que se sirve sin PIN. Antes era "/static/ entero", y esa carpeta
    # es justamente donde se dejan archivos para mirar (mails, capturas, informes):
    # o sea, material del negocio servido a cualquiera que llegue al puerto, sin
    # autenticarse. Ahora solo pasa el logo —que lo necesita la propia pantalla del
    # PIN— y el ping, que el lanzador usa como healthcheck y no dice nada de nadie.
    _SIN_PIN = {"/api/ping", "/static/cacho.png"}

    def do_GET(self):
        if self._rebinding():
            return self._rechazo_host()
        ruta = urlparse(self.path).path
        if ruta in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            return self._icono_ios()
        if ruta not in self._SIN_PIN and not self._adentro():
            # ya no estás adentro: esto es un LOGIN, y el login está frenado
            espera = _bloqueado(self._ip)
            if espera:
                return self._bloqueo(espera)
            if not self._pin_ok():
                if ruta.startswith("/api/"):
                    return self._json({"error": "falta el PIN"}, 403)
                return self._pagina_pin()
            if not ruta.startswith("/api/"):
                # Navegador: se le da el token y se REDIRIGE a la URL sin el ?pin=.
                # Si no, el PIN queda a la vista en la barra, en el historial y en
                # cualquier captura de pantalla que se saque de Cacho.
                return self._redirigir_sin_pin(_sesion_nueva())
            # Script local (cacho_lanzar.py) con ?pin=: se le deja pasar ESTA
            # request y no se le abre sesión. Si se le diera token, cada llamada
            # dejaría uno nuevo y en tres lanzamientos echaría de la lista a los
            # dispositivos de verdad (el cupo es de 20).
        if ruta == "/":
            return self._pagina()
        elif ruta.startswith("/static/"):
            return self._static(os.path.basename(ruta))
        elif ruta == "/api/ping":
            self._json({"ok": True, "boot": BOOT_ID})
        elif ruta == "/api/estado":
            try:
                self._json(estado_general())
            except Exception as e:
                self._json({"error": repr(e)}, 500)
        elif ruta.startswith("/api/sesion/") and ruta.endswith("/ver"):
            sid = ruta.split("/")[3]
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                return self._json({"error": "id inválido"}, 400)
            p = _buscar_transcript(sid)
            if not p:
                return self._json({"error": "no encontré esa sesión"}, 404)
            try:
                self._json(_leer_conversacion(p))
            except Exception as e:
                self._json({"error": repr(e)}, 500)
        elif ruta.startswith("/api/term/") and ruta.endswith("/stream"):
            self._stream(ruta.split("/")[3])
        else:
            self._json({"error": "no existe"}, 404)

    def _stream(self, tid):
        with TABS_LOCK:
            t = TABS.get(tid)
        if not t:
            return self._json({"error": "no existe"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = Queue()
        with t.lock:
            snapshot = bytes(t.buf)
            t.subs.append(q)
        try:
            if snapshot:
                self._sse(snapshot)
            while True:
                try:
                    d = q.get(timeout=15)
                except Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if d is None:
                    self.wfile.write(b"event: fin\ndata: \n\n")
                    self.wfile.flush()
                    break
                # Juntar lo que ya esté esperando en la cola y mandarlo en UN evento.
                #
                # Por qué (17-ago-2026): una sesión que trabaja fuerte escupe muchísimo por el
                # PTY — medido en vivo, la carga de mails iba a 261-486 KB/s. Mandando un evento
                # SSE por trozo, el navegador recibía 17-30 eventos/s y cada uno le costaba un
                # b64bytes() + un term.write() que xterm.js encola y parsea aparte. El renderer
                # de la pestaña de Cacho llegó a picos de 328 MB (y ese día a 1.839 MB), ahogó la
                # Mac y congeló la ventana: parecía que "se colgó Cacho" con el server impecable.
                #
                # Agrupar NO pierde un byte —es el mismo stream, en bloques más grandes— y le
                # saca al front la mayor parte del trabajo por byte. El tope evita el otro
                # extremo: un bloque gigante que tarde en pintarse y trabe la pestaña.
                if len(d) < COALESCE_MAX:
                    trozos = [d]
                    total = len(d)
                    fin = False
                    while total < COALESCE_MAX:
                        try:
                            extra = q.get_nowait()
                        except Empty:
                            break
                        if extra is None:   # la sesión terminó mientras juntábamos
                            fin = True
                            break
                        trozos.append(extra)
                        total += len(extra)
                    d = b"".join(trozos)
                    self._sse(d)
                    if fin:
                        self.wfile.write(b"event: fin\ndata: \n\n")
                        self.wfile.flush()
                        break
                    continue
                self._sse(d)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with t.lock:
                if q in t.subs:
                    t.subs.remove(q)

    # tipos que se sirven de /static. Lo que no esté acá se manda como descarga:
    # antes CUALQUIER extensión desconocida salía como application/javascript.
    _TIPOS = {".css": "text/css", ".js": "application/javascript",
              ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
              ".html": "text/html", ".json": "application/json",
              ".pdf": "application/pdf", ".txt": "text/plain"}

    def _static(self, nombre):
        """Sirve static/<nombre> con ETag.

        Antes iba `Cache-Control: max-age=86400` a secas: cambiabas la cara de Cacho
        (o cualquier imagen que se pasa por acá para mirarla) y el navegador seguía
        mostrando la vieja hasta un día entero, sin forma de saber por qué. Ahora
        revalida siempre y el server contesta 304 vacío si no cambió — cuesta lo
        mismo que un ping. Las libs (xterm) sí quedan cacheadas duro: no cambian."""
        p = os.path.join(AQUI, "static", nombre)
        if not os.path.isfile(p):
            return self._json({"error": "no existe"}, 404)
        st = os.stat(p)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        ext = os.path.splitext(nombre)[1].lower()
        inmutable = nombre.startswith(("xterm.", "addon-"))
        cache = "max-age=31536000, immutable" if inmutable else "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(p, "rb") as fh:
            cuerpo = fh.read()
        tipo = self._TIPOS.get(ext, "application/octet-stream")
        if tipo.startswith(("text/", "application/javascript", "application/json")):
            tipo += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if ext in (".html", ".svg"):
            # `sandbox` los deja en un origen OPACO: se ven igual, pero dejan de
            # ser "código de Cacho". Sin esto, cualquier .html que alguien deje en
            # static/ (que es la bandeja donde se dejan cosas para mirar: mails,
            # informes, capturas) corre con el origen del server y puede llamar a
            # /api/term/*/input con la cookie puesta — o sea, escribir en la
            # terminal. Un archivo de adorno no puede tener ese poder.
            self.send_header("Content-Security-Policy",
                             "sandbox; default-src 'none'; img-src data: 'self'; "
                             "style-src 'unsafe-inline'; font-src data:")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _sse(self, data):
        self.wfile.write(b"data: " + base64.b64encode(data) + b"\n\n")
        self.wfile.flush()

    def _icono_ios(self):
        p = os.path.join(AQUI, "static", "cacho.png")
        if not os.path.isfile(p):
            return self._json({"error": "no existe"}, 404)
        with open(p, "rb") as fh:
            cuerpo = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self):
        """Despacha el POST y, si se lo rechazó SIN leer su cuerpo, cierra la conexión.

        Sin esto (16-ago-2026, entrando desde el teléfono): el POST del login trae
        `pin=NNNN` en el cuerpo; si el server contesta y vuelve sin leerlo —bloqueo por
        PIN equivocado, 403 anti-rebinding, 404—, esos bytes quedan en el socket y, como
        acá se habla HTTP/1.1 con keep-alive, el navegador reusa la conexión y la request
        siguiente se lee corrompida: `501 Unsupported method ('pin=8940GET')`, y desde ahí
        Cacho no abre más hasta cerrar el navegador. Cerrar la conexión al rechazar corta
        el problema de raíz, valga para el endpoint que valga."""
        self._body_leido = False
        try:
            self._despachar_post()
        finally:
            if not self._body_leido and int(self.headers.get("Content-Length") or 0) > 0:
                self.close_connection = True

    def _despachar_post(self):
        u = urlparse(self.path)
        ruta = u.path
        if self._rebinding(origin_null_ok=(ruta == "/pin")):
            return self._json({"error": "host no permitido"}, 403)
        q = parse_qs(u.query)

        if ruta == "/api/ticket":
            # Lo pide el lanzador de la app, mandando el PIN en el CUERPO (no en la
            # URL ni como argumento): así el PIN no aparece en `ps` ni en ningún log.
            espera = _bloqueado(self._ip)
            if espera:
                return self._bloqueo(espera)
            datos = parse_qs(self._body().decode("utf-8", "replace"))
            if not (self._adentro() or _pin_igual(datos.get("pin", [""])[0])):
                _fallo_pin(self._ip)
                return self._json({"error": "falta el PIN"}, 403)
            _acierto_pin(self._ip)
            return self._json({"t": _ticket_nuevo(), "vida_seg": TICKET_VIDA})

        if ruta == "/pin":
            espera = _bloqueado(self._ip)
            if espera:
                return self._bloqueo(espera)
            datos = parse_qs(self._body().decode("utf-8", "replace"))
            if _pin_igual(datos.get("pin", [""])[0]):
                _acierto_pin(self._ip)
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 self._COOKIE_SESION.format(tok=_sesion_nueva()))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                _fallo_pin(self._ip)
                self._pagina_pin(error=True)
            return

        if not self._adentro():
            espera = _bloqueado(self._ip)
            if espera:
                return self._bloqueo(espera)
            if not self._pin_ok():
                return self._json({"error": "falta el PIN"}, 403)
            # ?pin= en un POST = script local: pasa esta request, sin abrir sesión
            # (ver el mismo razonamiento en do_GET)

        if ruta.startswith("/api/sesion/") and ruta.endswith("/meta"):
            # Fijar arriba / renombrar / reordenar una sesión de la barra izquierda.
            sid = ruta.split("/")[3]
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                return self._json({"error": "id inválido"}, 400)
            try:
                d = json.loads(self._body().decode("utf-8", "replace") or "{}")
            except ValueError:
                return self._json({"error": "json inválido"}, 400)
            campos = {}
            if "fija" in d:
                campos["fija"] = bool(d["fija"]) or None      # False = borrar la clave
            if "nombre" in d:
                # el nombre lo escribe el usuario: se recorta y se limpian los saltos
                n = re.sub(r"\s+", " ", str(d["nombre"] or "")).strip()[:80]
                campos["nombre"] = n or None                  # vacío = volver al automático
            if "orden" in d:
                try:
                    campos["orden"] = int(d["orden"])
                except (TypeError, ValueError):
                    campos["orden"] = None
            if not campos:
                return self._json({"error": "nada que guardar"}, 400)
            return self._json({"ok": True, "meta": _meta_set(sid, campos)})

        if ruta == "/api/term/new":
            cwd = q.get("cwd", [""])[0]
            resume = q.get("resume", [""])[0]
            if resume:
                # retomar una charla terminada: la carpeta sale del transcript,
                # no del cliente (y el id se valida antes de tocar el shell)
                if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", resume):
                    return self._json({"error": "id inválido"}, 400)
                p = _buscar_transcript(resume)
                s = _leer_sesion(p) if p else None
                if not s:
                    return self._json({"error": "no encontré esa sesión"}, 404)
                cwd = s["cwd"]
                if not cwd:
                    # honesto: no es que la carpeta falte, es que el transcript no
                    # nos dijo cuál era (ver el rescate del cwd en _leer_sesion)
                    return self._json(
                        {"error": "el transcript no dice en qué carpeta corría"}, 400)
                if not os.path.isdir(cwd):
                    return self._json(
                        {"error": "la carpeta de esa sesión ya no existe"}, 400)
            else:
                permitidos = {p["cwd"] for p in proyectos()}
                if cwd not in permitidos:
                    return self._json({"error": "proyecto inválido"}, 400)
            # Tope de pestañas: cada una es un zsh + un `claude` + un pty. Sin
            # techo, un cliente en bucle (un script con un error, no hace falta
            # mala intención) deja la máquina sin procesos ni memoria.
            with TABS_LOCK:
                if len(TABS) >= MAX_TABS:
                    return self._json(
                        {"error": f"ya hay {MAX_TABS} pestañas abiertas; "
                                  "cerrá alguna con la ✕"}, 429)
            t = TermSession(cwd, resume_id=resume)
            with TABS_LOCK:
                TABS[t.id] = t
            return self._json({"id": t.id})

        if ruta.startswith("/api/term/"):
            partes = ruta.split("/")
            tid, accion = partes[3], partes[4] if len(partes) > 4 else ""
            with TABS_LOCK:
                t = TABS.get(tid)
            if not t:
                return self._json({"error": "no existe"}, 404)
            if accion == "input":
                try:
                    d = base64.b64decode(json.loads(self._body())["d"])
                    t.escribir(d)
                    return self._json({"ok": True})
                except OSError as e:
                    return self._json({"ok": False, "msg": str(e)})
            if accion == "resize":
                try:
                    b = json.loads(self._body())
                    t.resize(int(b["cols"]), int(b["rows"]))
                except Exception as e:
                    print(f"⚠️ resize de la pestaña {tid} vino mal formado (lo ignoro): {e}", file=sys.stderr)
                    # antes contestaba ok:True igual — el front creía que la terminal
                    # había quedado del tamaño nuevo cuando no se tocó nada
                    return self._json({"ok": False, "msg": str(e)})
                return self._json({"ok": True})
            if accion == "kill":
                t.matar()
                with TABS_LOCK:
                    TABS.pop(tid, None)
                return self._json({"ok": True})
            return self._json({"error": "no existe"}, 404)

        if ruta == "/api/subir":
            # drag & drop de archivos: se guardan y se pega la ruta en la terminal
            nombre = os.path.basename(q.get("nombre", ["archivo"])[0]) or "archivo"
            nombre = re.sub(r"[^\w. ()@áéíóúñÁÉÍÓÚÑ-]", "_", nombre)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 500 * 1024 * 1024:
                return self._json({"error": "archivo muy grande (>500 MB)"}, 413)
            destino_dir = os.path.expanduser("~/Library/Caches/Cacho/subidas")
            os.makedirs(destino_dir, exist_ok=True)
            destino = os.path.join(destino_dir, nombre)
            if os.path.exists(destino):
                base, ext = os.path.splitext(nombre)
                destino = os.path.join(destino_dir,
                                       f"{base}-{int(time.time())}{ext}")
            restante = n
            with open(destino, "wb") as fh:
                while restante > 0:
                    trozo = self.rfile.read(min(65536, restante))
                    if not trozo:
                        break
                    fh.write(trozo)
                    restante -= len(trozo)
            return self._json({"ruta": destino})

        if ruta == "/api/abrir":
            if q.get("app", [""])[0] == "claude":
                ok, msg = abrir_app_claude()
            else:
                ok, msg = abrir_terminal(q.get("tty", [""])[0])
            return self._json({"ok": ok, "msg": msg})

        self._json({"error": "no existe"}, 404)

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------

PAGINA_PIN = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cacho</title>
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="Cacho">
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#262624;color:#e8e6dc;font-family:-apple-system,system-ui,sans-serif}
  .caja{text-align:center;padding:32px}
  .caja img{width:96px;height:96px;border-radius:22px}
  h1{font-size:22px;margin:14px 0 2px}
  p{color:#a8a698;margin:4px 0 18px;font-size:14px}
  input{font-size:26px;letter-spacing:8px;text-align:center;width:190px;padding:10px;
        border-radius:12px;border:1px solid #4a483f;background:#1d1c1a;color:#e8e6dc}
  button{display:block;margin:16px auto 0;font-size:16px;padding:10px 34px;border:0;
         border-radius:12px;background:#c96442;color:#fff;font-weight:600}
  .err{color:#ff8b6b;margin-top:12px;font-size:14px}
</style>
</head>
<body>
  <form class="caja" method="post" action="/pin">
    <img src="/static/cacho.png" alt="">
    <h1>Cacho</h1>
    <p>PIN de esta máquina (está en ~/.cacho_pin)</p>
    <input name="pin" inputmode="numeric" autocomplete="one-time-code" autofocus>
    <button>Entrar</button>
    {{AVISO}}
  </form>
</body>
</html>"""

PAGINA = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Cacho</title>
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="Cacho">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="icon" type="image/png" href="/static/cacho.png">
<link rel="stylesheet" href="/static/xterm.min.css">
<script src="/static/xterm.min.js"></script>
<script src="/static/addon-fit.min.js"></script>
<style>
:root{
  --bg:#FAF9F5; --panel:#F0EEE6; --card:#FFFFFF; --borde:#E3E0D5;
  --tinta:#1F1E1B; --gris:#6E6C64; --acento:#2D9CDB; --ocre:#B8860B;
  --term-bg:#1E1D1B;
  /* letra de display: el nombre "Cacho" y los títulos de sesión. Futura,
     elegida el 16-ago-2026 sobre otras 11 candidatas (antes era Georgia).
     Un solo lugar para cambiarla. */
  --display: Futura, "Century Gothic", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#262624; --panel:#1F1E1C; --card:#30302E; --borde:#3B3A36;
    --tinta:#F5F4EF; --gris:#A8A69D; --acento:#4FB3E8;
  }
}
*{box-sizing:border-box; margin:0; padding:0}
html,body{height:100%}
body{
  display:flex; background:var(--bg); color:var(--tinta); overflow:hidden;
  font:15px/1.4 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
}
/* ---------- barra lateral ---------- */
#side{
  /* la barra va SIEMPRE oscura, aunque el dispositivo esté en modo claro —
     el resto de la app sigue el tema del sistema */
  --panel:#1F1E1C; --card:#30302E; --borde:#3B3A36;
  --tinta:#F5F4EF; --gris:#A8A69D; --acento:#4FB3E8;
  width:300px; flex:none; background:var(--panel); border-right:1px solid var(--borde);
  display:flex; flex-direction:column; padding:14px 10px 10px;
}
#side h1{
  font-family:var(--display); font-weight:500; font-size:19px;
  display:flex; align-items:center; gap:8px; padding:2px 8px 12px;
}
#side h1 .logo{color:var(--acento); font-size:17px}
.logo-foto{width:32px; height:32px; border-radius:50%; flex:none}
#btn-nueva{
  display:flex; align-items:center; gap:8px; width:100%;
  background:var(--acento); color:#fff; border:0; border-radius:10px;
  padding:9px 14px; font-size:12px; font-weight:600; cursor:pointer;
}
#btn-nueva:hover{filter:brightness(.95)}
#menu-proy{
  display:none; background:var(--card); border:1px solid var(--borde);
  border-radius:10px; margin-top:6px; overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.12);
}
#menu-proy.ver{display:block}
#menu-proy button{
  display:block; width:100%; text-align:left; background:none; border:0;
  padding:8px 14px; font-size:12.5px; color:var(--tinta); cursor:pointer;
}
#menu-proy button:hover{background:var(--panel)}
#listas{flex:1; overflow-y:auto; margin-top:14px}
.seccion{
  color:var(--gris); font-size:10px; font-weight:700; letter-spacing:.08em;
  text-transform:uppercase; padding:10px 8px 6px;
}
.item{
  border-radius:10px; padding:8px 10px; cursor:pointer; margin-bottom:2px;
}
.item:hover{background:var(--card)}
.item.activo{background:var(--card); box-shadow:inset 2px 0 0 var(--acento)}
.item .fila{display:flex; align-items:center; gap:8px}
.dot{width:8px; height:8px; border-radius:50%; flex:none}
.dot.trabajando{background:var(--acento); animation:lat 1.4s ease-in-out infinite}
.dot.esperando{background:var(--ocre)}
.dot.terminada{background:var(--gris); opacity:.5}
@keyframes lat{0%,100%{box-shadow:0 0 0 0 rgba(45,156,219,.5)}50%{box-shadow:0 0 0 5px rgba(45,156,219,0)}}
.item .tit{
  flex:1; font-size:12.5px; font-weight:500; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis;
}
.item .sub{
  color:var(--gris); font-size:10.5px; margin-top:2px; padding-left:16px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.item .snippet{
  color:var(--gris); font-size:10.5px; margin-top:2px; padding-left:16px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-style:italic;
}
.item .x{
  border:0; background:none; color:var(--gris); opacity:.5; cursor:pointer;
  font-size:12px; padding:0 3px; border-radius:4px; flex:none;
}
.item .x:hover{opacity:1; color:var(--tinta)}
/* Fijar arriba / renombrar: aparecen al pasar por encima para no ensuciar la lista.
   En el celular no hay hover, así que ahí van siempre visibles (media query abajo). */
.item .acc{
  background:none; border:0; color:var(--gris); cursor:pointer; font-size:11px;
  padding:0 2px; opacity:0; transition:opacity .12s;
}
.item:hover .acc{opacity:.75}
.item .acc:hover{opacity:1}
.item .acc.fijar.on{opacity:1}
.item.fijada{background:var(--card); box-shadow:inset 2px 0 0 #d9a441}
.item.fijada .tit{font-weight:600}
.item.fijada[draggable="true"]{cursor:grab}
.item.arrastrando{opacity:.45; cursor:grabbing}
.item .tit.propio{font-style:normal}
/* Sesión que terminó y te está esperando sin que la hayas mirado: se marca
   hasta que la abrís (ver `esperan` en el JS). El punto ocre solo dice
   "quieta"; esto dice "quieta Y recién terminada, andá". */
.item.avisada{background:rgba(184,134,11,.13)}
.item.avisada .tit{font-weight:700}
.item .campanita{color:var(--ocre); font-size:11px; flex:none}
/* ---------- Cacho ----------
   Mientras alguna sesión de la app está trabajando, Cacho se sienta abajo de
   la lista. Es el dibujo de él, y está SENTADO: respira, nada más. Nada de
   recortarle la cabeza o las patas para animarlas por separado — la imagen se
   escala entera y no se deforma nunca. Cuando no hay trabajo, desaparece. */
#corriendo{
  display:none; position:relative; height:46px; margin:8px 2px 0;
  border-top:1px solid var(--borde);
  align-items:flex-end; justify-content:center;
  contain:layout style;   /* el respirar no toca el layout de la lista */
}
#corriendo.ver{display:flex}
#corriendo img{
  height:42px; display:block; transform-origin:bottom center;
  animation:respira 3.4s ease-in-out infinite;
}
@keyframes respira{
  0%,100%{transform:scale(1)}
  50%    {transform:scale(1.035)}
}
#pie{color:var(--gris); font-size:10.5px; padding:10px 8px 2px}
#btn-avisos{
  display:none; width:100%; margin-top:6px; background:none; cursor:pointer;
  border:1px dashed var(--borde); border-radius:10px; padding:7px 10px;
  color:var(--gris); font-size:11px; text-align:left;
}
#btn-avisos.ver{display:block}
#btn-avisos:hover{color:var(--tinta); border-color:var(--acento)}
/* ---------- paleta de búsqueda (⌘K) ---------- */
#paleta{
  display:none; position:fixed; inset:0; z-index:200;
  background:rgba(0,0,0,.45); padding-top:12vh; justify-content:center;
}
#paleta.ver{display:flex}
#paleta .caja{
  --panel:#1F1E1C; --card:#30302E; --borde:#3B3A36;
  --tinta:#F5F4EF; --gris:#A8A69D; --acento:#4FB3E8;
  width:min(560px, 92vw); max-height:66vh; display:flex; flex-direction:column;
  background:var(--panel); color:var(--tinta); border:1px solid var(--borde);
  border-radius:14px; overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,.5);
}
#paleta input{
  border:0; border-bottom:1px solid var(--borde); background:none; outline:none;
  color:var(--tinta); padding:14px 16px; font-family:inherit;
  font-size:16px;   /* 16px o iOS hace auto-zoom al enfocar (igual que #texto-m) */
}
#paleta .res{overflow-y:auto; padding:6px}
#paleta .op{
  display:flex; align-items:center; gap:9px; padding:8px 10px;
  border-radius:9px; cursor:pointer; font-size:13px;
}
#paleta .op.sel{background:var(--card)}
#paleta .op .qué{flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
#paleta .op .dónde{color:var(--gris); font-size:10.5px; flex:none}
#paleta .vacio{color:var(--gris); font-size:12.5px; padding:14px 12px}
#paleta .pieP{
  color:var(--gris); font-size:10.5px; padding:8px 14px; border-top:1px solid var(--borde);
}
/* ---------- zona principal ---------- */
#main{flex:1; display:flex; flex-direction:column; min-width:0}
#terms{flex:1; position:relative; padding:14px; min-height:0}
.term-box{
  position:absolute; inset:14px; background:var(--term-bg);
  border-radius:12px; padding:10px 14px 10px 12px; display:none;
}
.term-box.ver{display:block}
.term-box .xterm{height:100%}
.xterm-viewport::-webkit-scrollbar{width:8px}
.xterm-viewport::-webkit-scrollbar-track{background:transparent}
.xterm-viewport::-webkit-scrollbar-thumb{background:rgba(232,230,220,.22); border-radius:4px}
.xterm-viewport::-webkit-scrollbar-thumb:hover{background:rgba(232,230,220,.4)}
#vacio{
  position:absolute; inset:0; display:flex; flex-direction:column; gap:10px;
  align-items:center; justify-content:center; color:var(--gris);
  font-family:var(--display); font-size:17px;
}
/* ---------- visor de solo-lectura (automáticas / terminadas) ---------- */
.ver-box{
  position:absolute; inset:14px; background:var(--card); border:1px solid var(--borde);
  border-radius:12px; display:none; flex-direction:column; overflow:hidden;
}
.ver-box.ver{display:flex}
.ver-head{
  flex:none; display:flex; align-items:center; gap:10px;
  padding:12px 16px; border-bottom:1px solid var(--borde); background:var(--panel);
}
.ver-head .vtit{font-family:var(--display); font-size:15px; font-weight:500; flex:1;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.ver-head .vsub{color:var(--gris); font-size:11px; flex:none}
#btn-retomar{flex:none; border:1px solid var(--borde); background:var(--card);
  color:var(--tinta); border-radius:8px; padding:4px 12px; font-size:11.5px;
  font-weight:600; cursor:pointer}
#btn-retomar:hover{background:var(--acento); color:#fff; border-color:var(--acento)}
.ver-cuerpo{flex:1; overflow-y:auto; padding:14px 16px}
.msg{margin-bottom:12px; max-width:860px}
.msg .quien{font-size:10.5px; font-weight:700; color:var(--gris);
  text-transform:uppercase; letter-spacing:.06em; margin-bottom:3px}
.msg .quien .hora{font-weight:400; text-transform:none; letter-spacing:0; margin-left:6px}
.msg .texto{white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; line-height:1.5;
  background:var(--panel); border:1px solid var(--borde); border-radius:10px; padding:8px 12px}
.msg.vos .texto{background:transparent; border-color:var(--acento); border-width:1px}
.msg.tool .texto{color:var(--gris); font-size:11.5px; font-style:italic;
  background:transparent; border-style:dashed; padding:5px 12px}
.ver-nota{color:var(--gris); font-size:11px; text-align:center; padding:4px 0 10px}
#vacio .logo{color:var(--acento); font-size:34px}
/* dictado de macOS: el texto en composición debe envolver, no irse de largo */
.xterm .composition-view{
  white-space:pre-wrap !important; word-break:break-all;
  max-width:100%; overflow-wrap:anywhere;
}
#terms.arrastrando .term-box.ver{outline:2px dashed var(--acento); outline-offset:-6px}
/* ---------- dictado por micrófono (escritorio, Chrome) ---------- */
#btn-mic, #btn-enviar-esc{
  /* bottom = CENTRO del botón (translateY lo baja media altura): quedan
     centrados en la fila del "> " de Claude Code, entre las dos líneas.
     La var la calcula ubicarBotones() midiendo el terminal real. */
  position:absolute; bottom:var(--centro-botones, 69px); transform:translateY(50%);
  z-index:12; display:none;
  width:34px; height:34px; border-radius:50%; cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,.18); align-items:center; justify-content:center;
  opacity:.92; transition:bottom .18s ease-out;
}
@media (prefers-reduced-motion: reduce){
  #btn-mic, #btn-enviar-esc{transition:none}
  /* con "reducir movimiento" prendido en macOS, Cacho se queda quieto en un
     costado en vez de correr (sigue indicando que hay trabajo, sin moverse) */
  #corriendo img, #barra-m .logo-foto{animation:none}
}
#btn-mic{right:64px; border:1px solid var(--borde); background:var(--card); color:var(--gris)}
#btn-enviar-esc{right:22px; border:none; background:var(--acento); color:#fff}
#btn-mic.ver, #btn-enviar-esc.ver{display:flex}
#btn-mic:hover, #btn-enviar-esc:hover{filter:brightness(.97)}
#btn-mic.grabando{background:var(--acento); border-color:var(--acento); color:#fff; animation:lat 1.4s ease-in-out infinite}
#mic-live{
  position:absolute; right:22px; bottom:calc(var(--centro-botones, 69px) + 27px); z-index:12; max-width:440px;
  background:var(--card); border:1px solid var(--borde); border-radius:12px;
  padding:10px 14px; font-size:13px; line-height:1.45; color:var(--tinta);
  box-shadow:0 6px 24px rgba(0,0,0,.25); display:none; white-space:pre-wrap;
}
#mic-live.ver{display:block}
#mic-live .int{color:var(--gris); font-style:italic}
@media (max-width:700px){ #btn-mic, #btn-enviar-esc, #mic-live{display:none !important} }
/* ---------- SOLO TELÉFONO (≤700px): el escritorio no entra acá ----------
   Estética tipo app de Claude en iOS: barra superior con ☰, la lista de
   sesiones es un cajón que se desliza desde la izquierda con velo detrás. */
#barra-m{display:none}
#velo{display:none}
#input-m{display:none}
@media (max-width:700px){
  body{flex-direction:column}
  /* En el celular no hay hover: los botones de fijar/renombrar van siempre visibles
     y más grandes, que se puedan tocar con el dedo. */
  .item .acc{opacity:.8; font-size:15px; padding:2px 5px}
  #barra-m{
    display:flex; align-items:center; gap:10px; flex:none;
    padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;
    background:var(--bg); border-bottom:1px solid var(--borde);
  }
  #barra-m button{
    border:0; background:none; color:var(--tinta); font-size:22px;
    padding:2px 6px; cursor:pointer;
  }
  #barra-m .logo-foto{width:26px; height:26px}
  /* en el celular la barra lateral es un cajón cerrado: el que avisa que hay
     trabajo es la cara de Cacho de arriba, respirando */
  body.hay-trabajo #barra-m .logo-foto{animation:respira 3.4s ease-in-out infinite}
  #barra-m .tit-m{
    flex:1; font-family:var(--display); font-size:17px; font-weight:500;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  #side{
    position:fixed; top:0; left:0; bottom:0; z-index:30;
    width:84vw; max-width:330px;
    padding-top:calc(env(safe-area-inset-top) + 14px);
    padding-bottom:calc(env(safe-area-inset-bottom) + 10px);
    transform:translateX(-105%); transition:transform .22s ease;
    box-shadow:6px 0 30px rgba(0,0,0,.25);
    border-right:1px solid var(--borde);
  }
  body.menu-abierto #side{transform:translateX(0)}
  body.menu-abierto #velo{
    display:block; position:fixed; inset:0; z-index:25;
    background:rgba(0,0,0,.45);
  }
  .item{padding:11px 10px}
  .item .tit{font-size:14px}
  .item .sub, .item .snippet{font-size:11.5px}
  #btn-nueva{padding:12px 14px; font-size:14px}
  #terms{padding:8px}
  .term-box{inset:8px; border-radius:10px; padding:8px 10px}
  #vacio{font-size:15px; padding:0 24px; text-align:center}
  /* barra de escritura nativa (estilo WhatsApp): en el teléfono NO se escribe
     dentro de xterm — el teclado de iOS rompe su textarea oculto (duplica texto).
     Se escribe acá y se manda entero a la sesión. */
  #input-m{
    display:block; flex:none; background:var(--panel);
    border-top:1px solid var(--borde);
    padding:6px 8px calc(env(safe-area-inset-bottom) + 6px);
  }
  /* con el teclado abierto sobra poco alto: la paleta arranca más arriba */
  #paleta{padding-top:6vh}
  #paleta .caja{max-height:74vh}
  #teclas-m{display:flex; gap:5px; margin-bottom:6px}
  #teclas-m button{
    flex:1; border:1px solid var(--borde); background:var(--card); color:var(--tinta);
    border-radius:8px; padding:6px 0; font-size:12px; font-family:Menlo, monospace;
    min-width:0; white-space:nowrap;
  }
  #teclas-m button:active{background:var(--borde)}
  /* El de modo no compite por ancho con las teclas sueltas: lleva texto (el modo
     en el que está la sesión, leído de la pantalla de la TUI) y se ancha solo. */
  #teclas-m #btn-modo{flex:none; padding:6px 9px}
  #teclas-m #btn-modo.plan{border-color:var(--acento); color:var(--acento); font-weight:700}
  #teclas-m #btn-modo.ojo{border-color:var(--ocre); color:var(--ocre); font-weight:700}
  #fila-envio{display:flex; gap:8px; align-items:flex-end}
  #texto-m{
    flex:1; resize:none; border:1px solid var(--borde); border-radius:18px;
    background:var(--card); color:var(--tinta); padding:9px 14px;
    font:16px/1.35 -apple-system, sans-serif;  /* 16px: evita el auto-zoom de iOS */
    max-height:120px; outline:none;
  }
  #btn-enviar{
    flex:none; width:38px; height:38px; border:0; border-radius:50%;
    background:var(--acento); color:#fff; font-size:17px;
  }
  #btn-enviar:active{filter:brightness(.85)}
  #btn-adj{
    flex:none; width:38px; height:38px; border:1px solid var(--borde);
    border-radius:50%; background:var(--card); color:var(--tinta); font-size:20px;
  }
  #btn-adj:active{background:var(--borde)}
  #btn-mic-m{
    flex:none; width:38px; height:38px; border:1px solid var(--borde);
    border-radius:50%; background:var(--card); color:var(--tinta);
    display:flex; align-items:center; justify-content:center;
  }
  #btn-mic-m:active{background:var(--borde)}
  #btn-mic-m.grabando{
    background:var(--acento); border-color:var(--acento); color:#fff;
    animation:lat 1.4s ease-in-out infinite;
  }
}
/* avisos no bloqueantes (creando sesión, subiendo archivo, errores) */
#aviso-m{
  display:none; position:fixed; left:50%; transform:translateX(-50%);
  top:calc(env(safe-area-inset-top) + 10px); z-index:60; max-width:86vw;
  background:var(--card); border:1px solid var(--borde); color:var(--tinta);
  padding:8px 16px; border-radius:18px; font-size:13px;
  box-shadow:0 6px 24px rgba(0,0,0,.25);
}
#aviso-m.ver{display:block}
#aviso-m.error{border-color:#D9534F; color:#D9534F}
#aviso-m .deshacer{
  border:0; background:var(--acento); color:#fff; cursor:pointer;
  border-radius:12px; padding:4px 12px; font-size:12.5px; font-weight:600;
  font-family:inherit; margin-left:4px;
}
</style>
</head>
<body>
<div id="barra-m">
  <button id="btn-menu" title="Sesiones">☰</button>
  <img class="logo-foto" src="/static/cacho.png" alt="">
  <span class="tit-m" id="tit-m">Cacho</span>
  <button id="btn-buscar" title="Buscar sesión">🔍</button>
</div>
<div id="velo"></div>
<div id="aviso-m"></div>
<div id="side">
  <h1><img class="logo-foto" src="/static/cacho.png" alt=""> Cacho</h1>
  <button id="btn-nueva">＋ Nueva sesión</button>
  <div id="menu-proy"></div>
  <div id="listas"></div>
  <div id="corriendo"><img src="/static/cacho-dibujo.png" alt="Cacho"></div>
  <button id="btn-avisos">🔔 Avisarme cuando una sesión termine</button>
  <div id="pie">cargando…</div>
</div>
<div id="paleta">
  <div class="caja">
    <input id="paleta-q" placeholder="Buscar sesión o proyecto…" autocomplete="off"
           autocorrect="off" spellcheck="false">
    <div class="res" id="paleta-res"></div>
    <div class="pieP">↑↓ moverse · ⏎ abrir · esc cerrar</div>
  </div>
</div>
<div id="main">
  <div id="terms">
    <div id="vacio"><img src="/static/cacho.png" style="width:84px;height:84px;border-radius:50%">Abrí una sesión nueva o elegí una de la izquierda.</div>
    <button id="btn-mic" title="Dictar por micrófono"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg></button>
    <button id="btn-enviar-esc" title="Enviar (Enter en la sesión activa)"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg></button>
    <div id="mic-live"></div>
  </div>
  <div id="input-m">
    <div id="teclas-m">
      <button id="btn-modo" data-seq="&#27;[Z"
              title="Cambiar modo (shift+tab): normal → auto-aceptar → plan">⇧⇥ modo</button>
      <button data-seq="&#27;">esc</button>
      <button data-seq="&#9;">tab</button>
      <button data-seq="&#27;[A">↑</button>
      <button data-seq="&#27;[B">↓</button>
      <button data-seq="&#3;">^C</button>
      <button data-seq="&#13;">⏎</button>
      <button data-seq="&#27;" data-repetir="1"
              title="Editar el mensaje anterior (esc esc)">✎</button>
    </div>
    <div id="fila-envio">
      <button id="btn-adj" title="Adjuntar foto o archivo">＋</button>
      <input type="file" id="file-m" multiple style="display:none">
      <textarea id="texto-m" rows="1" enterkeyhint="send" autocapitalize="sentences"
        placeholder="Escribile a la sesión…"></textarea>
      <button id="btn-mic-m" title="Dictar por micrófono"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg></button>
      <button id="btn-enviar" title="Enviar">➤</button>
    </div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
// teléfono: la barra lateral pasa a ser un cajón (ver CSS @media ≤700px)
const MOVIL = matchMedia("(max-width:700px)").matches;
function menu(abrir){ document.body.classList.toggle("menu-abierto", abrir); }
let estado = {tabs:[], afuera:[], proyectos:[]};
let abiertas = {};        // id -> {term, fit, es, box}
let activa = null;

// Sesiones que terminaron y todavía no miraste (ver "avisos" más abajo). Se
// declaran acá arriba porque itemHTML() las lee para marcar el ítem.
const esperan = new Set();
function claveDe(o){ return (o.__tab ? "tab:" : "ses:") + o.id; }

// escapa TAMBIÉN comillas: esc() se usa dentro de atributos (data-vtit,
// data-cwd…) y un título con " rompía el HTML de la barra lateral entera
function esc(t){return String(t==null?"":t).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function hace(seg){
  if(seg < 60) return seg + " s";
  if(seg < 3600) return Math.floor(seg/60) + " min";
  return Math.floor(seg/3600) + " h";
}
function b64bytes(b64){
  const bin = atob(b64), a = new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) a[i]=bin.charCodeAt(i);
  return a;
}
function b64de(str){
  const enc = new TextEncoder().encode(str);
  let bin=""; enc.forEach(b=>bin+=String.fromCharCode(b));
  return btoa(bin);
}

// aviso flotante no bloqueante (nada de alert(): en el teléfono no se ve
// qué falló y en el escritorio corta el flujo)
let avisoTimer = null;
let deshacerTimer = null;   // el del toast "Deshacer" (ver ofrecerDeshacer); se declara
                            // acá y no allá abajo porque aviso() lo cancela y `let`
                            // tiene zona muerta: usarlo antes sería ReferenceError
let ptyAvisado = 0;   // freno: onData dispara por tecla, no repetir el aviso
function avisoPty(){
  const t = Date.now();
  if(t - ptyAvisado < 5000) return;
  ptyAvisado = t;
  aviso("La sesión ya no acepta escritura (¿terminó?)", true);
}
function aviso(txt, error){
  const el = $("#aviso-m");
  if(!el) return;
  clearTimeout(avisoTimer);
  clearTimeout(deshacerTimer);   // si había un "Deshacer" abierto, este aviso lo reemplaza
                                 // (y su temporizador ya no debe apagar el nuevo)
  if(!txt){ el.classList.remove("ver"); return; }
  el.textContent = txt;
  el.classList.toggle("error", !!error);
  el.classList.add("ver");
  avisoTimer = setTimeout(() => el.classList.remove("ver"), error ? 6000 : 4000);
}

/* ---------------- terminales ---------------- */
/* Botones flotantes (mic + enviar): centrados en la fila del input de
   Claude Code. La caja del prompt son dos líneas de "─"; se busca la de
   ABAJO en las últimas filas visibles y los botones se centran en la fila
   inmediatamente arriba (la del ">"). Así no importa cuántas filas tenga
   el pie (estado, avisos): se mide lo que hay, no se adivina. Si no se
   encuentra caja (shell pelado, buffer scrolleado) no se toca nada. */
function ubicarBotones(){
  if(MOVIL || !activa) return;
  const a = abiertas[activa];
  if(!a) return;
  const screen = a.box.querySelector(".xterm-screen");
  if(!screen || !a.term.rows) return;
  const sr = screen.getBoundingClientRect(), tr = $("#terms").getBoundingClientRect();
  if(!sr.height) return;
  const h = sr.height / a.term.rows;
  const buf = a.term.buffer.active;
  for(let f = a.term.rows - 1; f >= Math.max(1, a.term.rows - 6); f--){
    const linea = buf.getLine(buf.viewportY + f);
    if(!linea) continue;
    const guiones = (linea.translateToString(true).match(/─/g) || []).length;
    if(guiones > a.term.cols * .5){
      // centro de la fila f-1 (el input), medido desde abajo de #terms
      const centro = (tr.bottom - sr.bottom) + (a.term.rows - f + .5) * h;
      $("#terms").style.setProperty("--centro-botones", centro.toFixed(1) + "px");
      return;
    }
  }
}
function abrirTab(id){
  if(abiertas[id]){ activar(id); return; }
  const box = document.createElement("div");
  box.className = "term-box"; box.dataset.id = id;
  $("#terms").appendChild(box);
  const term = new Terminal({
    fontFamily:'"SF Mono", Menlo, monospace', fontSize: MOVIL ? 12 : 15,
    cursorBlink:true, scrollback:8000, allowProposedApi:true,
    theme:{
      background:"#1E1D1B", foreground:"#E8E6DC", cursor:"#2D9CDB",
      cursorAccent:"#1E1D1B", selectionBackground:"rgba(45,156,219,.35)"
    }
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(box);
  // teléfono: tocar la terminal NO debe abrir el teclado sobre el textarea
  // oculto de xterm (iOS lo rompe: autocorrector duplica el texto). Se
  // escribe siempre por la barra de abajo (#input-m).
  if(MOVIL && term.textarea) term.textarea.setAttribute("inputmode", "none");
  // reubicar los botones flotantes cuando el terminal pinta (solo la pestaña
  // activa tiene stream, así que esto no corre por pestañas de fondo)
  if(!MOVIL && term.onWriteParsed){
    let t = null;
    term.onWriteParsed(() => { clearTimeout(t); t = setTimeout(ubicarBotones, 200); });
  }
  // dictando con el mic de escritorio, Enter en el TECLADO debe pegar lo
  // dictado antes de enviarse — sin esto Claude recibía un Enter con el
  // input vacío y lo dictado quedaba colgado en el globo (14-ago-2026)
  term.attachCustomKeyEventHandler(ev => {
    if(ev.type === "keydown" && ev.key === "Enter" && micActivo && micTab === id
       && !ev.shiftKey && !ev.altKey && !ev.ctrlKey && !ev.metaKey){
      micParar(() => fetch(`/api/term/${id}/input`, {method:"POST",
        body: JSON.stringify({d: b64de("\r")})}).catch(()=>{}));
      return false;   // este Enter no pasa a la terminal: va después del paste
    }
    return true;
  });
  term.onData(d => fetch(`/api/term/${id}/input`, {
    method:"POST", body: JSON.stringify({d: b64de(d)})
  }).then(r => r.json()).then(j => {
    if(j && j.ok === false) avisoPty();   // el server dice por qué no escribe
  }).catch(()=>{}));
  term.onResize(({cols, rows}) => fetch(`/api/term/${id}/resize`, {
    method:"POST", body: JSON.stringify({cols, rows})
  }).catch(()=>{}));
  abiertas[id] = {term, fit, es:null, box};
  activar(id);   // conecta el stream (y corta el de la pestaña que deja atrás)
}

// stream SSE de una pestaña. OJO: los navegadores cortan a ~6 conexiones
// por host (Safari iOS Y TAMBIÉN Chrome escritorio — visto 13-ago: con 6
// sesiones abiertas en la ventana de Cacho el pool quedó agotado y crear
// otra colgaba TODO mudo), y cada stream abierto es una conexión que no se
// suelta. Por eso se mantiene abierto SOLO el stream de la sesión activa
// (ver activar()); al volver a conectar, el server re-manda el buffer entero.
function conectar(id){
  const a = abiertas[id];
  if(!a || a.es) return;
  a.term.reset();
  const es = new EventSource(`/api/term/${id}/stream`);
  es.onmessage = e => a.term.write(b64bytes(e.data));
  es.addEventListener("fin", () => {
    a.term.write("\r\n\x1b[38;2;45;156;219m— sesión terminada —\x1b[0m\r\n");
    es.close(); a.es = null;
  });
  es.onerror = () => {
    // readyState 2 = CLOSED: el navegador NO va a reintentar (error HTTP o
    // corte definitivo). Sin esto la terminal quedaba congelada muda.
    if(es.readyState === 2){
      a.es = null;
      a.term.write("\r\n\x1b[38;2;110;108;100m— conexión cortada: tocá la sesión en la barra para reconectar —\x1b[0m\r\n");
    }
  };
  a.es = es;
}
function desconectar(id){
  const a = abiertas[id];
  if(a && a.es){ a.es.close(); a.es = null; }
}

function activar(id){
  activa = id;
  if(id) limpiarAviso("tab:" + id);   // la miraste: se apaga el 🔔 y baja el contador
  // una sola conexión de stream viva (ver conectar()): se corta la de las
  // demás pestañas y se (re)conecta la activa — escritorio Y teléfono
  Object.keys(abiertas).forEach(k => { if(k !== id) desconectar(k); });
  if(id && abiertas[id]) conectar(id);
  if(MOVIL && id) menu(false);   // elegiste sesión: el cajón se guarda solo
  document.querySelectorAll(".term-box, .ver-box").forEach(b =>
    b.classList.toggle("ver", b.dataset.id === id));
  $("#vacio").style.display = id ? "none" : "flex";
  const a = abiertas[id];
  if(a){
    requestAnimationFrame(() => {
      a.fit.fit();
      if(!MOVIL) a.term.focus();
      // resize explícito: sin esto el pty puede quedar con el ancho viejo
      fetch(`/api/term/${id}/resize`, {method:"POST",
        body: JSON.stringify({cols: a.term.cols, rows: a.term.rows})}).catch(()=>{});
      ubicarBotones();
      if(MOVIL) setTimeout(pintarModo, 300);   // el buffer se llena al reconectar
    });
  }
  render();
}

function cerrarTab(id, matar){
  const a = abiertas[id];
  if(a){ if(a.es) a.es.close(); a.term.dispose(); a.box.remove(); delete abiertas[id]; }
  if(matar) fetch(`/api/term/${id}/kill`, {method:"POST"}).catch(()=>{});
  if(activa === id){
    const resto = estado.tabs.filter(t => t.id !== id && abiertas[t.id]);
    activar(resto.length ? resto[resto.length-1].id : null);
  }
  refrescar();
}

async function nueva(cwd){
  $("#menu-proy").classList.remove("ver");
  // feedback visible: antes fallaba MUDA (si el fetch se colgaba o el server
  // devolvía error, en el teléfono parecía que el botón no hacía nada)
  aviso("Creando sesión…");
  try{
    const r = await fetch("/api/term/new?cwd=" + encodeURIComponent(cwd), {method:"POST"});
    const j = await r.json();
    if(!j.id) throw new Error(j.error || ("HTTP " + r.status));
    await refrescar(); abrirTab(j.id); aviso("");
  }catch(err){
    aviso("No pude crear la sesión: " + err.message, true);
  }
}

/* ------- visor de solo-lectura: automáticas y sesiones terminadas ------- */
let verTitulo = "";   // título de la sesión que se está viendo (para la barra móvil)

function abrirVer(sid, titulo, viva){
  limpiarAviso("ses:" + sid);
  // una sola caja de visor: al abrir otra sesión se reutiliza
  let box = document.querySelector(".ver-box");
  if(!box){
    box = document.createElement("div");
    box.className = "ver-box";
    box.innerHTML = '<div class="ver-head"><span class="vtit"></span>' +
      '<button id="btn-retomar" title="Reabre esta charla en una pestaña con claude --resume">⟳ Retomar</button>' +
      '<span class="vsub">solo lectura</span></div><div class="ver-cuerpo"></div>';
    $("#terms").appendChild(box);
    box.querySelector("#btn-retomar").addEventListener("click", retomar);
  }
  box.dataset.id = "ver:" + sid;
  box.dataset.sid = sid;
  // retomar solo tiene sentido si la sesión ya no está corriendo en otro lado
  box.querySelector("#btn-retomar").style.display = viva ? "none" : "";
  verTitulo = titulo || "Sesión";
  box.querySelector(".vtit").textContent = verTitulo;
  box.querySelector(".ver-cuerpo").innerHTML =
    '<div class="ver-nota">cargando conversación…</div>';
  activar("ver:" + sid);
  pintarVer();
}

async function retomarSid(sid){
  if(!sid) return;
  try{
    const r = await fetch("/api/term/new?resume=" + sid, {method:"POST"});
    const j = await r.json();
    if(!j.id) throw new Error(j.error || "?");
    await refrescar();
    abrirTab(j.id);
  }catch(err){
    aviso("No pude retomar la charla: " + err.message, true);
  }
}
function retomar(){
  const box = document.querySelector(".ver-box");
  if(box) retomarSid(box.dataset.sid);
}

/* Red de seguridad de la ✕ de un click: la pestaña muere, pero la CHARLA no.
   Durante unos segundos se ofrece reabrirla con `claude --resume`, que es
   exactamente lo que hace el botón ⟳ del visor. Sin esto, errarle al click
   en la barra costaba la sesión y no había vuelta atrás. */
function ofrecerDeshacer(t){
  const el = $("#aviso-m");
  if(!el) return;
  clearTimeout(avisoTimer); clearTimeout(deshacerTimer);
  el.classList.remove("error");
  el.innerHTML = "Cerré «" + esc(t.titulo) + "» · " +
    '<button class="deshacer">Deshacer</button>';
  el.classList.add("ver");
  deshacerTimer = setTimeout(() => el.classList.remove("ver"), 9000);
  el.querySelector(".deshacer").onclick = () => {
    clearTimeout(deshacerTimer);
    el.classList.remove("ver");
    retomarSid(t.sid);
  };
}

async function pintarVer(){
  const box = document.querySelector(".ver-box");
  if(!box || !activa || activa !== box.dataset.id) return;
  try{
    const r = await fetch(`/api/sesion/${box.dataset.sid}/ver`);
    const j = await r.json();
    if(j.error) throw new Error(j.error);
    if(j.titulo){ verTitulo = j.titulo; box.querySelector(".vtit").textContent = j.titulo; }
    const cuerpo = box.querySelector(".ver-cuerpo");
    const abajo = cuerpo.scrollHeight - cuerpo.scrollTop - cuerpo.clientHeight < 60;
    cuerpo.innerHTML =
      (j.recortado ? '<div class="ver-nota">(conversación larga: se muestra el final)</div>' : "") +
      j.items.map(m => `
        <div class="msg ${m.q==="Vos"?"vos":""} ${m.tool?"tool":""}">
          <div class="quien">${esc(m.q)}<span class="hora">${esc(m.ts)}</span></div>
          <div class="texto">${m.tool ? "⚙ " + esc(m.t) : esc(m.t)}</div>
        </div>`).join("") ||
      '<div class="ver-nota">sin mensajes todavía</div>';
    if(abajo) cuerpo.scrollTop = cuerpo.scrollHeight;
  }catch(err){
    box.querySelector(".ver-cuerpo").innerHTML =
      '<div class="ver-nota">no pude leer la sesión (' + esc(err.message) + ')</div>';
  }
}

/* ---------------- render ---------------- */
// Un ítem de la barra. `o` es la sesión (pestaña o de afuera) y `attrs` los data-* que
// definen qué pasa al hacerle click (abrir pestaña / enfocar Terminal / ver read-only).
// Botones: 📌 fijar arriba · ✏️ ponerle nombre propio · ✕ cerrar (solo pestañas).
function itemHTML(o, attrs, opts){
  opts = opts || {};
  const sid = o.sid || "";
  const acciones = sid ? `
      <button class="acc fijar ${o.fija?"on":""}" data-fijar="${esc(sid)}"
        title="${o.fija?"Soltar de arriba":"Fijar arriba"}">${o.fija?"📌":"📍"}</button>
      <button class="acc" data-renombrar="${esc(sid)}" data-nombre="${esc(o.titulo)}"
        title="Ponerle un nombre">✏️</button>` : "";
  const espera = esperan.has(claveDe(o));
  return `
      <div class="item ${opts.activo?"activo":""} ${opts.fijada?"fijada":""} ${espera?"avisada":""}" ${attrs}
           data-sid="${esc(sid)}" ${opts.fijada?'draggable="true"':""}>
        <div class="fila"><span class="dot ${o.estado}"></span>
          <span class="tit ${o.renombrada?"propio":""}">${esc(o.titulo)}</span>
          ${espera?'<span class="campanita" title="terminó y te espera">🔔</span>':""}
          <span style="color:var(--gris);font-size:10px">${hace(o.hace_seg)}</span>
          ${acciones}${opts.botonX||""}</div>
        <div class="sub">${esc(o.proyecto)}</div>
        ${o.ultimo_txt&&!opts.sinSnippet?'<div class="snippet">'+esc(o.ultimo_quien)+": "+esc(o.ultimo_txt)+"</div>":""}
      </div>`;
}

// Los data-* de cada tipo de sesión, para poder dibujarla igual en su sección o arriba.
function attrsDe(o){
  if(o.__tab) return `data-tab="${o.id}"`;
  if(o.tipo === "terminal" && o.viva) return `data-tty="${esc(o.tty)}"`;
  return `data-ver="${esc(o.id)}" data-vtit="${esc(o.titulo)}" data-viva="${o.viva?1:0}"`;
}

function render(){
  // barra lateral
  const tabs = estado.tabs.map(t => Object.assign({__tab:true}, t));
  const vivasAfuera = estado.afuera.filter(s => s.viva);
  const term = vivasAfuera.filter(s => s.tipo === "terminal");
  const autos = vivasAfuera.filter(s => s.tipo !== "terminal");
  const muertas = estado.afuera.filter(s => !s.viva).length;
  const esActiva = o => o.__tab ? o.id===activa : ("ver:"+o.id)===activa;
  const botonX = t => `<button class="x" data-x="${t.id}"
            title="Cerrar esta sesión">✕</button>`;
  let h = "";

  // ── FIJADAS: salen de su sección y suben al tope, en el orden que las dejaste ──
  const fijadas = tabs.concat(estado.afuera).filter(o => o.fija);
  fijadas.sort((a,b) => (a.orden==null?9999:a.orden) - (b.orden==null?9999:b.orden));
  const fijos = new Set(fijadas.map(o => o.sid));
  if(fijadas.length){
    // con snippet: las fijadas son justamente las que mirás de reojo para
    // saber en qué anda cada una sin tener que abrirlas
    h += '<div class="seccion">📌 Fijadas</div>' + fijadas.map(o =>
      itemHTML(o, attrsDe(o), {activo:esActiva(o), fijada:true,
                               botonX: o.__tab ? botonX(o) : ""})).join("");
  }
  const libres = a => a.filter(o => !fijos.has(o.sid));

  const tabsL = libres(tabs);
  if(tabsL.length){
    h += '<div class="seccion">En esta app</div>' +
      tabsL.map(t => itemHTML(t, attrsDe(t), {activo:esActiva(t), botonX:botonX(t)})).join("");
  }
  const termL = libres(term);
  if(termL.length){
    h += '<div class="seccion">En Terminal (afuera)</div>' +
      termL.map(s => itemHTML(s, attrsDe(s), {})).join("");
  }
  const autosL = libres(autos);
  if(autosL.length){
    h += '<div class="seccion">Automáticas</div>' +
      autosL.map(s => itemHTML(s, attrsDe(s), {activo:esActiva(s)})).join("");
  }
  const term12 = libres(estado.afuera.filter(s => !s.viva)).slice(0, 12);
  if(term12.length){
    h += '<div class="seccion">Terminadas hoy</div>' +
      term12.map(s => itemHTML(s, attrsDe(s), {activo:esActiva(s), sinSnippet:true})).join("");
  }
  $("#listas").innerHTML = h || '<div class="seccion">Sin sesiones vivas</div>';
  // Cacho corre mientras haya trabajo adentro de la app (en el teléfono, donde
  // no se ve la barra, es el logo de arriba el que trota)
  const hayTrabajo = estado.tabs.some(t => t.estado === "trabajando");
  $("#corriendo").classList.toggle("ver", hayTrabajo);
  document.body.classList.toggle("hay-trabajo", hayTrabajo);
  $("#pie").textContent = estado.tabs.length + " en la app · " +
    vivasAfuera.length + " afuera · " + muertas + " terminadas hoy" +
    (MOVIL ? "" : " · ⌘K buscar");
  if(MOVIL){
    const t = estado.tabs.find(t => t.id === activa);
    $("#tit-m").textContent = t ? t.titulo :
      (activa && activa.startsWith("ver:") ? verTitulo : "Cacho");
  }
  micVisible();
}

/* ---------------- fijar / renombrar / reordenar ---------------- */
async function guardarMeta(sid, campos){
  try{
    const r = await fetch(`/api/sesion/${sid}/meta`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(campos)});
    const j = await r.json();
    if(j.error) throw new Error(j.error);
    await refrescar();          // el server ya es la fuente: se repinta con lo guardado
  }catch(err){
    $("#pie").textContent = "no pude guardar: " + err.message;
  }
}

// El drag vive contra el DOM, pero render() reescribe #listas entero cada 4 s: si el
// refresco cae en medio del arrastre, el ítem se evapora en la mano. Por eso se pausa.
let arrastrando = null, pausaRefresco = false;

document.addEventListener("dragstart", e => {
  const it = e.target.closest(".item.fijada");
  if(!it) return;
  arrastrando = it.dataset.sid; pausaRefresco = true;
  it.classList.add("arrastrando");
  e.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragover", e => {
  if(!arrastrando) return;
  const sobre = e.target.closest(".item.fijada");
  if(!sobre || sobre.dataset.sid === arrastrando) return;
  e.preventDefault();
  const caja = sobre.getBoundingClientRect();
  const arriba = (e.clientY - caja.top) < caja.height / 2;
  const yo = document.querySelector(`.item.fijada[data-sid="${arrastrando}"]`);
  if(yo) sobre.parentNode.insertBefore(yo, arriba ? sobre : sobre.nextSibling);
});
document.addEventListener("drop", e => { if(arrastrando) e.preventDefault(); });
document.addEventListener("dragend", async () => {
  if(!arrastrando) return;
  arrastrando = null;
  document.querySelectorAll(".arrastrando").forEach(n => n.classList.remove("arrastrando"));
  const ids = [...document.querySelectorAll(".item.fijada")].map(n => n.dataset.sid);
  for(let i = 0; i < ids.length; i++) await guardarMeta(ids[i], {orden: i});
  pausaRefresco = false;
});

/* ---------------- eventos ---------------- */
document.addEventListener("click", e => {
  if(e.target.closest("#btn-buscar")){ menu(false); abrirPaleta(); return; }
  if(e.target.closest("#btn-menu")){
    menu(!document.body.classList.contains("menu-abierto"));
    return;
  }
  if(e.target.id === "velo"){ menu(false); return; }
  const fij = e.target.closest("[data-fijar]");
  if(fij){
    e.stopPropagation();
    guardarMeta(fij.dataset.fijar, {fija: !fij.classList.contains("on")});
    return;
  }
  const ren = e.target.closest("[data-renombrar]");
  if(ren){
    e.stopPropagation();
    const actual = ren.dataset.nombre || "";
    const n = prompt("Nombre para esta sesión (vacío = volver al automático):", actual);
    if(n !== null) guardarMeta(ren.dataset.renombrar, {nombre: n});
    return;
  }
  const x = e.target.closest(".x");
  if(x){
    // Un click y chau (16-ago-2026). Antes pedía confirmar con un segundo
    // click, pero la confirmación se vencía sola a los 2,5 s: en la práctica
    // había que darle tres veces para que cerrara. La red es el "Deshacer".
    e.stopPropagation();
    const id = x.dataset.x;
    const t = estado.tabs.find(t => t.id === id);
    cerrarTab(id, true);
    if(t && t.sid) ofrecerDeshacer(t);   // sin transcript no hay qué retomar
    return;
  }
  const item = e.target.closest(".item");
  if(item){
    if(item.dataset.tab){ abrirTab(item.dataset.tab); return; }
    if(item.dataset.tty){
      if(item.dataset.sid) limpiarAviso("ses:" + item.dataset.sid);
      fetch("/api/abrir?tty=" + item.dataset.tty, {method:"POST"})
        .then(r => r.json()).then(j => { if(j && !j.ok) aviso(j.msg || "No pude abrir esa Terminal", true); })
        .catch(() => aviso("No pude abrir esa Terminal", true));
      return;
    }
    if(item.dataset.ver){ abrirVer(item.dataset.ver, item.dataset.vtit, item.dataset.viva === "1"); return; }
  }
  if(e.target.closest("#btn-nueva")){
    const m = $("#menu-proy");
    m.innerHTML = estado.proyectos.map(p =>
      `<button data-cwd="${esc(p.cwd)}">${esc(p.nombre)}</button>`).join("");
    m.classList.toggle("ver");
    return;
  }
  const bp = e.target.closest("#menu-proy button");
  if(bp){ nueva(bp.dataset.cwd); return; }
  $("#menu-proy").classList.remove("ver");
});

window.addEventListener("resize", () => {
  const a = abiertas[activa];
  if(a){ a.fit.fit(); requestAnimationFrame(ubicarBotones); }
});

/* ---- barra de escritura del teléfono ---- */
let micMParar = null;   // lo define el bloque del mic móvil (si hay Web Speech API)
function mandar(raw, siFalla){
  if(!activa) return;
  fetch(`/api/term/${activa}/input`, {
    method:"POST", body: JSON.stringify({d: b64de(raw)})
  }).then(r => r.json()).then(j => {
    // el server contesta ok:false si el pty ya no acepta escritura (sesión
    // terminada): sin esto el texto se perdía MUDO
    if(j && j.ok === false){ avisoPty(); if(siFalla) siFalla(); }
  }).catch(() => {
    aviso("No se envió — sin conexión con el server", true);
    if(siFalla) siFalla();
  });
}
function enviarTexto(){
  if(micMParar) micMParar();   // si estabas dictando (mic de Chrome), corta y deja el texto final
  const ta = $("#texto-m");
  // El dictado de iOS escribe texto "provisorio" (marked text) que recién se
  // confirma al cerrar el dictado. Si se lee ta.value en el medio, se manda
  // una hipótesis vieja y el resto se pierde (mensajes truncados, 14-ago-2026).
  // blur() obliga a iOS a confirmar lo dictado; el valor se lee un tick después.
  ta.blur();
  setTimeout(() => {
    if(!ta.value.trim()){ ta.focus(); return; }
    const valor = ta.value;
    ta.value = ""; ta.style.height = "auto";
    // bracketed paste: el texto entra entero (con saltos de línea incluidos)
    // y el \r final lo envía — igual que pegar y dar Enter. Si falla, el
    // texto vuelve al cajón en vez de perderse.
    mandar("\x1b[200~" + valor + "\x1b[201~\r", () => { ta.value = valor; });
    ta.focus();   // mejor esfuerzo por dejar el teclado abierto, como antes
  }, 150);
}
if(MOVIL){
  const ta = $("#texto-m");
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  });
  ta.addEventListener("keydown", e => {
    if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); enviarTexto(); }
  });
  // pointerdown + preventDefault: manda sin robarle el foco al textarea
  // (el teclado queda abierto para seguir escribiendo)
  $("#btn-enviar").addEventListener("pointerdown", e => {
    e.preventDefault(); enviarTexto();
  });
  document.querySelectorAll("#teclas-m button").forEach(b =>
    b.addEventListener("pointerdown", e => {
      e.preventDefault();
      mandar(b.dataset.seq);
      // esc×2 (editar el mensaje anterior): la TUI espera DOS escapes
      // SEPARADOS; pegados los lee como una secuencia sola y no hace nada.
      if(b.dataset.repetir) setTimeout(() => mandar(b.dataset.seq), 90);
      // el modo cambia recién cuando la TUI repinta su pie
      if(b.id === "btn-modo") setTimeout(pintarModo, 400);
    }));
  // ＋ adjuntar desde el teléfono: mismo circuito que el drag&drop del
  // escritorio (/api/subir guarda en ~/Library/Caches/Cacho/subidas y acá
  // se pega la ruta en la sesión activa, sin Enter: agregás texto y mandás)
  $("#btn-adj").addEventListener("click", () => {
    if(!activa || !abiertas[activa]){ aviso("Abrí una sesión primero", true); return; }
    $("#file-m").click();
  });
  $("#file-m").addEventListener("change", async () => {
    const files = [...$("#file-m").files];
    if(!files.length) return;
    if(!activa || !abiertas[activa]){ aviso("Abrí una sesión primero", true); return; }
    aviso(files.length === 1 ? "Subiendo " + files[0].name + "…"
                             : "Subiendo " + files.length + " archivos…");
    let pegar = "", fallaron = [];
    for(const f of files){
      try{
        const r = await fetch("/api/subir?nombre=" + encodeURIComponent(f.name),
                              {method:"POST", body:f});
        const j = await r.json();
        if(j.ruta) pegar += '"' + j.ruta + '" ';
        else fallaron.push(f.name);
      }catch(err){ fallaron.push(f.name); }
    }
    $("#file-m").value = "";
    if(pegar && !fallaron.length){
      mandar("\x1b[200~" + pegar + "\x1b[201~");
      aviso("Listo: la ruta quedó en la sesión — agregá texto si querés y mandá ⏎");
    } else if(pegar){
      mandar("\x1b[200~" + pegar + "\x1b[201~");
      aviso("Subí " + (files.length - fallaron.length) + " de " + files.length +
            " — falló: " + fallaron.join(", "), true);
    } else {
      aviso("No pude subir " + (files.length === 1 ? "el archivo" : "ningún archivo"), true);
    }
  });
  // teclado de iOS: la página se achica al alto visible real para que la
  // barra quede pegada arriba del teclado y la terminal no quede tapada
  if(window.visualViewport){
    const vv = window.visualViewport;
    const ajustar = () => {
      document.body.style.height = vv.height + "px";
      window.scrollTo(0, 0);
      const a = abiertas[activa];
      if(a) a.fit.fit();
    };
    vv.addEventListener("resize", ajustar);
    vv.addEventListener("scroll", ajustar);
    ajustar();
  }
}

/* ---- dictado por micrófono (escritorio): Web Speech API de Chrome ----
   Igual que el micrófono de WhatsApp: tocás el botón, hablás, volvés a tocar.
   El texto entra en la sesión activa con bracketed paste SIN Enter: lo
   revisás y lo mandás con ⏎ o con el botón de enviar de al lado (mismo
   comportamiento que el dictado de macOS).
   Chrome sobre 127.0.0.1 es "secure context", así que la API anda en la
   ventana de Cacho.app. En el teléfono hay un mic propio en la barra de
   abajo (#btn-mic-m, más adelante), que dicta al cajón de texto. */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let mic = null, micActivo = false, micFinal = "", micInterim = "", micTab = null;
function micVisible(){
  const hay = !MOVIL && !!activa && !!abiertas[activa];
  const b = $("#btn-mic");
  // grabando queda visible SIEMPRE (aunque cambies a un visor): si se
  // escondiera no habría forma de cortar el dictado
  if(b) b.classList.toggle("ver", (hay && !!SR) || micActivo);
  const e = $("#btn-enviar-esc");
  if(e) e.classList.toggle("ver", hay);
}
function micPintar(interim){
  const el = $("#mic-live");
  el.innerHTML = esc(micFinal) + (interim ? '<span class="int">' + esc(interim) + "</span>" : "");
  el.classList.toggle("ver", micActivo && !!(micFinal || interim));
}
function micAviso(txt){
  const el = $("#mic-live");
  el.innerHTML = '<span class="int">' + esc(txt) + "</span>";
  el.classList.add("ver");
  setTimeout(() => { if(!micActivo) el.classList.remove("ver"); }, 5000);
}
function micParar(despues){
  // BUG histórico (14-ago-2026): acá se usaba SOLO micFinal, pero Chrome
  // recién "confirma" lo hablado tras una pausa — si parabas (botón, ➤ o
  // Enter) sin pausar, TODO seguía siendo hipótesis (interim) y se tiraba:
  // "grabo, aprieto Enter y se borra todo". Ahora la hipótesis cuenta.
  micActivo = false;
  $("#btn-mic").classList.remove("grabando");
  if(mic){
    mic.onresult = null;   // lo pendiente ya lo tomamos acá: sin duplicados
    try{ mic.stop(); }catch(e){}
  }
  const texto = (micFinal + micInterim).trim();
  micFinal = ""; micInterim = "";
  $("#mic-live").classList.remove("ver");
  const tab = micTab;
  micTab = null;
  if(texto && tab && abiertas[tab]){
    fetch(`/api/term/${tab}/input`, {method:"POST",
      body: JSON.stringify({d: b64de("\x1b[200~" + texto + "\x1b[201~")})})
      .then(r => r.json()).then(j => {
        if(j && j.ok === false){
          try{ navigator.clipboard.writeText(texto); }catch(e){}
          aviso("La sesión ya no acepta escritura — lo dictado quedó copiado al portapapeles", true);
          return;
        }
        // el Enter (si lo hay) va DESPUÉS de que el TUI digirió el paste
        if(despues) setTimeout(despues, 250);
      })
      .catch(() => {   // que lo dictado no se pierda mudo
        try{ navigator.clipboard.writeText(texto); }catch(e){}
        aviso("Falló el pegado del dictado — quedó copiado al portapapeles", true);
      });
    abiertas[tab].term.focus();
  } else if(despues){
    despues();
  }
}
function micArrancar(){
  if(!activa || !abiertas[activa]) return;
  micTab = activa;           // el texto va a la pestaña donde arrancaste a hablar
  mic = new SR();
  mic.lang = "es-UY"; mic.continuous = true; mic.interimResults = true;
  mic.onresult = e => {
    let interim = "";
    for(let i = e.resultIndex; i < e.results.length; i++){
      const r = e.results[i];
      if(r.isFinal) micFinal += r[0].transcript + " ";
      else interim += r[0].transcript;
    }
    micInterim = interim;   // la hipótesis vigente: micParar() la usa al cortar
    micPintar(interim);
  };
  mic.onerror = e => {
    if(e.error === "not-allowed" || e.error === "service-not-allowed"){
      micActivo = false;
      $("#btn-mic").classList.remove("grabando");
      micAviso("Chrome no tiene permiso de micrófono para Cacho — dáselo en el candado de la barra o en Ajustes de Chrome.");
    }
    // "no-speech"/"aborted": el onend lo rearranca solo mientras siga activo
  };
  // Chrome corta solo tras unos segundos de silencio: mientras el botón siga
  // encendido, se rearranca y la grabación continúa sin perder lo dictado
  mic.onend = () => { if(micActivo) try{ mic.start(); }catch(e){} };
  micActivo = true; micFinal = ""; micInterim = "";
  $("#btn-mic").classList.add("grabando");
  try{ mic.start(); }catch(e){ micActivo = false; $("#btn-mic").classList.remove("grabando"); }
}
$("#btn-mic").addEventListener("click", () => micActivo ? micParar() : micArrancar());

// botón de enviar (estilo app de Claude): manda Enter a la sesión activa,
// para poder operar SOLO con el mouse (dictás con el mic, mandás con la
// flecha). Si estabas grabando, primero corta y pega lo dictado; el Enter
// va un toque después, para que el TUI de claude digiera el paste.
$("#btn-enviar-esc").addEventListener("click", () => {
  const id = activa;   // SIEMPRE la sesión visible (si dictaste en otra, el
  if(!id || !abiertas[id]) return;   // paste va allá pero el Enter no)
  const enter = () => fetch(`/api/term/${id}/input`, {method:"POST",
    body: JSON.stringify({d: b64de("\r")})})
    .catch(() => aviso("No se envió el Enter — probá de nuevo", true));
  // si estabas dictando, micParar pega lo dictado (incluida la hipótesis)
  // y recién DESPUÉS de que el paste llegó dispara el Enter
  if(micActivo) micParar(enter); else enter();
});

/* ---- mic de la barra del teléfono: dicta al cajón de texto ----
   A diferencia del mic de escritorio (que pega directo en la terminal), acá
   el texto cae en #texto-m: lo revisás y lo mandás con ➤, igual que si lo
   hubieras tipeado. OJO: sobre una IP de Tailscale por http:// Safari
   puede negar el micrófono porque no es "secure context" — en ese caso el
   botón avisa y queda el mic del teclado de iOS, que dicta en el mismo cajón. */
if(MOVIL){
  const bm = $("#btn-mic-m");
  if(!SR){
    bm.style.display = "none";   // sin Web Speech API: queda el mic del teclado
  } else {
    const ta = $("#texto-m");
    let micM = null, micMActivo = false, base = "", fin = "", interimM = "";
    const pintar = interim => {
      ta.value = base + fin + (interim || "");
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
    };
    micMParar = () => {
      if(!micMActivo) return;
      micMActivo = false;
      bm.classList.remove("grabando");
      if(micM){
        micM.onresult = null;   // lo pendiente lo tomamos acá: sin duplicados
        try{ micM.stop(); }catch(e){}
      }
      // BUG histórico (14-ago-2026): acá se hacía pintar("") con SOLO lo
      // confirmado — si parabas (➤ o Enter) sin pausar antes, la hipótesis
      // (casi todo lo hablado) se borraba del cajón. Ahora se conserva.
      if(interimM){ fin += interimM + " "; interimM = ""; }
      pintar("");   // queda TODO lo dictado en el cajón; se manda con ➤
    };
    const arrancar = () => {
      base = ta.value ? ta.value.replace(/\s+$/, "") + " " : "";
      fin = ""; interimM = "";
      micM = new SR();
      micM.lang = "es-UY"; micM.continuous = true; micM.interimResults = true;
      micM.onresult = e => {
        let interim = "";
        for(let i = e.resultIndex; i < e.results.length; i++){
          const r = e.results[i];
          if(r.isFinal) fin += r[0].transcript + " ";
          else interim += r[0].transcript;
        }
        interimM = interim;   // la hipótesis vigente: micMParar la conserva
        pintar(interim);
      };
      micM.onerror = e => {
        if(e.error === "not-allowed" || e.error === "service-not-allowed"){
          micMParar();
          aviso("Safari no dio permiso de micrófono — usá el mic del teclado de iOS", true);
        }
        // "no-speech"/"aborted": el onend rearranca solo mientras siga activo
      };
      micM.onend = () => { if(micMActivo) try{ micM.start(); }catch(e){} };
      micMActivo = true;
      bm.classList.add("grabando");
      try{ micM.start(); }catch(e){
        micMActivo = false; bm.classList.remove("grabando");
        aviso("No pude arrancar el dictado en este navegador", true);
      }
    };
    bm.addEventListener("click", () => micMActivo ? micMParar() : arrancar());
  }
}

/* ---- recarga automática tras reinicio del server ----
   Reiniciar el server NO recarga esta ventana (open -a solo la enfoca), así
   que sin esto quedaba corriendo el JS viejo para siempre. El server manda
   su BOOT_ID en /api/ping; si cambia, la página se recarga sola. Mientras
   el server está caído el fetch falla y no pasa nada. */
let bootVisto = null;
function chequearBoot(){
  fetch("/api/ping").then(r => r.json()).then(j => {
    if(!j.boot) return;
    if(bootVisto === null){ bootVisto = j.boot; return; }
    // no recargar si hay algo a medio escribir: dictado activo (escritorio
    // o teléfono) o texto sin mandar en el cajón del teléfono — la recarga
    // se lo llevaba puesto; se reintenta solo en el próximo chequeo
    const aMedias = micActivo ||
      $("#btn-mic-m").classList.contains("grabando") ||
      (MOVIL && $("#texto-m").value.trim() !== "");
    if(j.boot !== bootVisto && !aMedias) location.reload();
  }).catch(()=>{});
}
chequearBoot();
setInterval(chequearBoot, 15000);

/* ---- drag & drop de archivos: guardar y pegar la ruta en la terminal ---- */
["dragover", "dragenter"].forEach(ev =>
  document.addEventListener(ev, e => {
    e.preventDefault();
    $("#terms").classList.add("arrastrando");
  }));
document.addEventListener("dragleave", e => {
  if(!e.relatedTarget) $("#terms").classList.remove("arrastrando");
});
// Después de soltar un archivo, el foco NO queda en la terminal: Chrome se lo
// deja al documento y había que dar un click adentro para que el Enter cayera
// en la sesión (queja de 16-ago-2026). Un solo focus() no alcanza — el
// navegador termina de digerir el drop DESPUÉS de nuestro handler y lo pisa.
// Por eso se insiste unas cuantas veces durante medio segundo.
let focoTimers = [];
function enfocarSesion(){
  const a = abiertas[activa];
  // En el teléfono NO se enfoca xterm: su textarea oculto rompe el teclado de
  // iOS (por eso queda inputmode="none"). Ahí el foco va al cajón de abajo.
  if(MOVIL){ const ta = $("#texto-m"); if(ta) ta.focus(); return; }
  if(!a) return;
  focoTimers.forEach(clearTimeout);
  focoTimers = [0, 60, 180, 400].map(ms => setTimeout(() => {
    try{ window.focus(); a.term.focus(); }catch(e){}
  }, ms));
}
// Si en medio de la insistencia tocás otra cosa (la barra, un botón), mandás
// vos: se cortan los reintentos para no robarte el foco de vuelta.
document.addEventListener("pointerdown", () => {
  focoTimers.forEach(clearTimeout); focoTimers = [];
}, true);

document.addEventListener("drop", async e => {
  e.preventDefault();  // sin esto Chrome navega al archivo y "te lo tira afuera"
  $("#terms").classList.remove("arrastrando");
  if(!activa || !abiertas[activa]){ aviso("Abrí una sesión antes de soltar el archivo", true); return; }
  enfocarSesion();   // ya mismo: subir un video puede tardar y el foco no espera
  const files = [...(e.dataTransfer.files || [])];
  let pegar = "", fallaron = 0;
  if(files.length){
    aviso(files.length === 1 ? "Subiendo " + files[0].name + "…"
                             : "Subiendo " + files.length + " archivos…");
    for(const f of files){
      try{
        const r = await fetch("/api/subir?nombre=" + encodeURIComponent(f.name),
                              {method:"POST", body: f});
        const j = await r.json();
        if(j.ruta) pegar += '"' + j.ruta + '" '; else fallaron++;
      }catch(err){ fallaron++; }
    }
  } else {
    pegar = e.dataTransfer.getData("text") || "";
  }
  if(!pegar){
    if(files.length) aviso("No pude subir " + (files.length === 1 ? "el archivo" : "ningún archivo"), true);
    return;
  }
  fetch(`/api/term/${activa}/input`, {
    method:"POST", body: JSON.stringify({d: b64de(pegar)})
  }).catch(()=>{});
  enfocarSesion();   // el Enter tiene que caer en la sesión, sin click previo
  aviso(fallaron ? "Subí " + (files.length - fallaron) + " de " + files.length
                 : "Listo: escribí qué querés y mandá ⏎", !!fallaron);
});

/* ---- botón de modo del teléfono (shift+tab) -------------------------------
   Shift+Tab cicla los modos de Claude Code (normal → auto-aceptar → plan…),
   pero en el teclado de iOS no existe: en el celular no había forma de entrar
   en modo plan. El botón manda ⇧⇥ de verdad (ESC [ Z, lo que manda una
   terminal) y además MUESTRA en qué modo está la sesión, leyéndolo del pie
   que la propia TUI pinta ("plan mode on (shift+tab to cycle)"). Se lee de la
   pantalla y no de un estado propio: la fuente es lo que Claude muestra, así
   no se desincroniza si cambiás el modo desde el escritorio. */
function modoActual(){
  const a = abiertas[activa];
  if(!a || !a.term) return "";
  const buf = a.term.buffer.active;
  let txt = "";
  for(let f = a.term.rows - 1; f >= Math.max(0, a.term.rows - 8); f--){
    const l = buf.getLine(buf.viewportY + f);
    if(l) txt += l.translateToString(true).toLowerCase() + "\n";
  }
  // Se busca el NOMBRE del modo, no el "(shift+tab to cycle)" del final: en la
  // pantalla angosta del teléfono la TUI corta esa línea ("…(shift+tab to  ·").
  // Los cinco de Claude Code v2.1 (verificado ciclando con ⇧⇥ el 16-ago-2026):
  //   ⏵⏵ accept edits on → ⏸ plan mode on → ⏵⏵ bypass permissions on
  //   → ⏵⏵ auto mode on → ⏸ manual mode on → vuelve a empezar
  if(txt.includes("accept edits on")) return "edits";
  if(txt.includes("plan mode on")) return "plan";
  if(txt.includes("bypass permissions on")) return "bypass";
  if(txt.includes("auto mode on")) return "auto";
  if(txt.includes("manual mode on")) return "manual";
  return "";   // TUI sin línea de modo (o sin TUI): el botón queda neutro
}
function pintarModo(){
  const b = $("#btn-modo");
  if(!b) return;
  const m = modoActual();
  b.textContent = "⇧⇥ " + (m || "modo");
  b.classList.toggle("plan", m === "plan");
  // bypass y auto ejecutan SIN preguntarte: que se vean distinto, de un vistazo
  b.classList.toggle("ojo", m === "bypass" || m === "auto");
}

/* ---- avisos: que Cacho golpee la puerta cuando una sesión termina ----------
   Con tres o cuatro sesiones andando, el problema no es que Claude tarde: es
   que termina y nadie se entera. El punto de color ya decía "quieta", pero
   había que estar mirando la barra. Acá se detecta la TRANSICIÓN
   trabajando→esperando (el server la calcula rápido: pty quieto ≥8 s, ver
   estado_general) y se avisa: notificación del sistema + sonidito + contador
   en el título de la pestaña + 🔔 en el ítem hasta que lo abrís.
   No avisan las automáticas: corren solas y no esperan nada de nadie. */
let estadoPrev = {};          // clave -> último estado visto
let audioCtx = null, ultimoBeep = 0;

function beep(){
  const t = Date.now();
  if(t - ultimoBeep < 1500) return;   // 3 sesiones que terminan juntas: un solo bip
  ultimoBeep = t;
  try{
    const AC = window.AudioContext || window.webkitAudioContext;
    if(!AC) return;
    audioCtx = audioCtx || new AC();
    if(audioCtx.state === "suspended") audioCtx.resume();
    const t0 = audioCtx.currentTime;
    [880, 1320].forEach((f, i) => {   // dos notas cortas, sin archivo de audio
      const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
      const ini = t0 + i * 0.13;
      osc.type = "sine"; osc.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ini);
      g.gain.exponentialRampToValueAtTime(0.13, ini + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ini + 0.12);
      osc.connect(g); g.connect(audioCtx.destination);
      osc.start(ini); osc.stop(ini + 0.14);
    });
  }catch(e){}
}

function notificar(o){
  beep();
  if(navigator.vibrate) try{ navigator.vibrate([40, 60, 40]); }catch(e){}
  if(!("Notification" in window) || Notification.permission !== "granted") return;
  try{
    const n = new Notification(o.titulo || "Sesión", {
      body: (o.proyecto || "") + " — terminó, te espera",
      icon: "/static/cacho.png", tag: claveDe(o),
    });
    n.onclick = () => { window.focus(); if(o.__tab) abrirTab(o.id); n.close(); };
  }catch(e){}
}

function pintarTitulo(){
  document.title = esperan.size ? "(" + esperan.size + ") Cacho" : "Cacho";
}
function limpiarAviso(clave){
  if(esperan.delete(clave)) pintarTitulo();
}

function revisarAvisos(){
  // SOLO las pestañas de la app. Las sesiones de Terminal de afuera terminan
  // decenas de veces por día mientras trabajás sentado frente a ellas, y Cacho
  // no tiene forma de saber si las estás mirando: avisarlas era garantía de
  // sonar todo el día al pedo. Lo de adentro sí lo controla la app.
  const lista = estado.tabs.map(t => Object.assign({__tab:true}, t));
  const vistos = {};
  for(const o of lista){
    const k = claveDe(o);
    vistos[k] = o.estado;
    if(o.estado === "trabajando"){
      esperan.delete(k);   // arrancó de nuevo (le contestaste por otro lado)
    } else if(estadoPrev[k] === "trabajando" && o.estado === "esperando"){
      // si la estás mirando de frente no hace falta molestar
      const mirando = document.hasFocus() && o.__tab && activa === o.id;
      if(!mirando){ esperan.add(k); notificar(o); }
    }
  }
  for(const k of [...esperan]) if(!(k in vistos)) esperan.delete(k);
  estadoPrev = vistos;
  pintarTitulo();
}

function pintarBotonAvisos(){
  const hay = ("Notification" in window);
  $("#btn-avisos").classList.toggle("ver", hay && Notification.permission === "default");
}
$("#btn-avisos").addEventListener("click", () => {
  beep();   // el click también destraba el audio del navegador
  if(!("Notification" in window)){ aviso("Este navegador no tiene notificaciones", true); return; }
  Notification.requestPermission().then(p => {
    pintarBotonAvisos();
    aviso(p === "granted" ? "Listo: te aviso cuando una sesión termine"
                          : "El navegador no dio permiso de notificaciones", p !== "granted");
  });
});
pintarBotonAvisos();

/* ---- pegar del portapapeles (⌘V): captura → archivo → ruta en la sesión ----
   Cmd+Shift+4 y pegar es el camino corto para mostrarle algo a Claude. Antes
   había que guardar la captura y arrastrarla. Mismo circuito que el drag&drop
   (/api/subir), pero el portapapeles no trae nombre: se lo inventamos. */
document.addEventListener("paste", async e => {
  if($("#paleta").classList.contains("ver")) return;   // en la paleta, pegar es pegar
  const items = [...((e.clipboardData && e.clipboardData.items) || [])];
  const files = items.filter(i => i.kind === "file").map(i => i.getAsFile()).filter(Boolean);
  if(!files.length) return;      // texto pelado: que siga su camino normal
  e.preventDefault();
  if(!activa || !abiertas[activa]){ aviso("Abrí una sesión antes de pegar", true); return; }
  const enCajon = MOVIL && document.activeElement === $("#texto-m");
  aviso(files.length === 1 ? "Subiendo la captura…" : "Subiendo " + files.length + " archivos…");
  let pegar = "", fallaron = 0;
  for(const f of files){
    const ext = (f.type.split("/")[1] || "png").replace(/[^\w]/g, "");
    // hora LOCAL, no UTC: el nombre del archivo tiene que coincidir con la hora
    // del reloj de uno o después no se sabe cuál captura es cuál
    const d = new Date(), dd = n => String(n).padStart(2, "0");
    const nombre = f.name || ("captura-" + d.getFullYear() + "-" +
      dd(d.getMonth()+1) + "-" + dd(d.getDate()) + "-" +
      dd(d.getHours()) + dd(d.getMinutes()) + dd(d.getSeconds()) + "." + ext);
    try{
      const r = await fetch("/api/subir?nombre=" + encodeURIComponent(nombre),
                            {method:"POST", body:f});
      const j = await r.json();
      if(j.ruta) pegar += '"' + j.ruta + '" '; else fallaron++;
    }catch(err){ fallaron++; }
  }
  if(!pegar){ aviso("No pude subir " + (files.length === 1 ? "la captura" : "los archivos"), true); return; }
  if(enCajon){
    const ta = $("#texto-m");
    ta.value = (ta.value ? ta.value.replace(/\s+$/, "") + " " : "") + pegar;
    ta.dispatchEvent(new Event("input"));
  } else {
    mandar("\x1b[200~" + pegar + "\x1b[201~");
    enfocarSesion();
  }
  aviso(fallaron ? "Subí " + (files.length - fallaron) + " de " + files.length :
        "Listo: la ruta quedó en la sesión — escribí qué querés y mandá ⏎", !!fallaron);
});

/* ---- ⌘K: buscador de sesiones y proyectos --------------------------------
   Con la lista larga, encontrar "la sesión de Meta Ads" con el ojo cuesta más
   que escribir tres letras. Busca en título, proyecto y snippet; si no hay
   sesión que coincida, ofrece abrir una NUEVA en el proyecto que matchee. */
let paletaOpts = [], paletaSel = 0;

function opcionesPaleta(q){
  const norm = s => String(s || "").toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "");   // "meta ads" encuentra "Metá"
  // Por PALABRAS, no por substring: si el proyecto arranca con un emoji o
  // tiene algo en el medio, buscar la frase seguida no encuentra nada. Con
  // palabras sueltas, el orden y lo que haya entremedio dejan de importar.
  const palabras = norm(q).trim().split(/\s+/).filter(Boolean);
  const casaCon = txt => { const s = norm(txt); return palabras.every(w => s.includes(w)); };
  const casa = o => casaCon(o.titulo + " " + o.proyecto + " " + (o.ultimo_txt||""));
  const out = [];
  estado.tabs.map(x => Object.assign({__tab:true}, x)).filter(casa).forEach(o =>
    out.push({icono:"●", qué:o.titulo, dónde:o.proyecto, estado:o.estado,
              hacer:() => abrirTab(o.id)}));
  estado.afuera.filter(s => s.viva).filter(casa).forEach(o =>
    out.push({icono: o.tipo === "terminal" ? "▣" : "⚙", qué:o.titulo,
              dónde: o.proyecto + (o.tipo === "terminal" ? " · Terminal" : " · automática"),
              estado:o.estado,
              hacer:() => o.tipo === "terminal" && o.tty
                ? fetch("/api/abrir?tty=" + o.tty, {method:"POST"})
                    .then(r => r.json())
                    .then(j => { if(j && !j.ok) aviso(j.msg || "No pude abrir esa Terminal", true); })
                    .catch(() => aviso("No pude abrir esa Terminal", true))
                : abrirVer(o.id, o.titulo, o.viva)}));
  estado.afuera.filter(s => !s.viva).filter(casa).slice(0, 8).forEach(o =>
    out.push({icono:"◌", qué:o.titulo, dónde:o.proyecto + " · terminada",
              estado:"terminada", hacer:() => abrirVer(o.id, o.titulo, false)}));
  // se matchea contra la frase entera para que escribir "nueva" (o "sesión en
  // manage") también llegue acá, no solo tipear el nombre pelado del proyecto
  estado.proyectos.filter(p => casaCon("nueva sesión en " + p.nombre))
    .forEach(p => out.push({icono:"＋", qué:"Nueva sesión en " + p.nombre,
                            dónde:"", estado:"", hacer:() => nueva(p.cwd)}));
  return out.slice(0, 40);
}

function pintarPaleta(){
  const res = $("#paleta-res");
  if(!paletaOpts.length){ res.innerHTML = '<div class="vacio">Nada con eso.</div>'; return; }
  res.innerHTML = paletaOpts.map((o, i) => `
    <div class="op ${i===paletaSel?"sel":""}" data-i="${i}">
      <span class="dot ${esc(o.estado)}" style="${o.estado?"":"display:none"}"></span>
      <span class="qué">${esc(o.icono)} ${esc(o.qué)}</span>
      <span class="dónde">${esc(o.dónde)}</span>
    </div>`).join("");
  const sel = res.querySelector(".op.sel");
  if(sel) sel.scrollIntoView({block:"nearest"});
}

function abrirPaleta(){
  $("#paleta").classList.add("ver");
  $("#paleta-q").value = "";
  paletaSel = 0; paletaOpts = opcionesPaleta("");
  pintarPaleta();
  setTimeout(() => $("#paleta-q").focus(), 0);
}
function cerrarPaleta(){
  $("#paleta").classList.remove("ver");
  const a = abiertas[activa];
  if(a && !MOVIL) a.term.focus();
}
$("#paleta-q").addEventListener("input", e => {
  paletaOpts = opcionesPaleta(e.target.value); paletaSel = 0; pintarPaleta();
});
$("#paleta-q").addEventListener("keydown", e => {
  // el max(0, …) NO sobra: con la lista vacía, length-1 es -1 y el índice se
  // iba a negativo (paletaOpts[-1] = undefined)
  if(e.key === "ArrowDown"){ e.preventDefault(); paletaSel = Math.max(0, Math.min(paletaSel+1, paletaOpts.length-1)); pintarPaleta(); }
  else if(e.key === "ArrowUp"){ e.preventDefault(); paletaSel = Math.max(paletaSel-1, 0); pintarPaleta(); }
  else if(e.key === "Enter"){ e.preventDefault(); const o = paletaOpts[paletaSel]; if(o){ cerrarPaleta(); o.hacer(); } }
  else if(e.key === "Escape"){ e.preventDefault(); cerrarPaleta(); }
});
$("#paleta").addEventListener("click", e => {
  const op = e.target.closest(".op");
  if(op){ const o = paletaOpts[+op.dataset.i]; cerrarPaleta(); if(o) o.hacer(); return; }
  if(e.target.id === "paleta") cerrarPaleta();   // click en el velo
});
// El atajo va en captura: xterm se come el keydown si el foco está en la terminal.
document.addEventListener("keydown", e => {
  if((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")){
    e.preventDefault(); e.stopPropagation();
    $("#paleta").classList.contains("ver") ? cerrarPaleta() : abrirPaleta();
  }
}, true);

async function refrescar(){
  if(pausaRefresco) return;   // hay un arrastre en curso: no repintar la lista debajo
  try{
    const r = await fetch("/api/estado");
    const j = await r.json();
    if(j.error) throw new Error(j.error);
    estado = j;
    revisarAvisos();          // antes de render(): el 🔔 se pinta en la lista
    render();
    pintarBotonAvisos();      // el permiso pudo darse desde el candado del navegador
    if(MOVIL) pintarModo();   // por si cambiaste el modo desde otro lado
    if($("#paleta").classList.contains("ver")){
      paletaOpts = opcionesPaleta($("#paleta-q").value);
      paletaSel = Math.min(paletaSel, Math.max(0, paletaOpts.length - 1));
      pintarPaleta();
    }
  }catch(err){
    $("#pie").textContent = "sin conexión con el server… (" + err.message + ")";
  }
}

(async () => {
  await refrescar();
  // arranque: si no hay ninguna pestaña viva, abrir una en el proyecto principal
  if(!estado.tabs.some(t => t.viva)){
    const pref = estado.proyectos[0];
    if(pref) await nueva(pref.cwd);
  } else {
    abrirTab(estado.tabs.filter(t => t.viva).slice(-1)[0].id);
  }
  setInterval(() => { refrescar(); pintarVer(); }, 4000);
})();
</script>
</body>
</html>"""


class Servidor(ThreadingHTTPServer):
    """
    ThreadingHTTPServer que no grita cuando el navegador cuelga la llamada.

    Por qué (17-ago-2026): el log ~/Library/Logs/sesiones-claude.log tenía 591
    tracebacks de ConnectionResetError y 16 de BrokenPipeError en 900 KB. No son
    fallas: son los SSE/fetch que el front corta al recargar la página o al
    cambiar de pestaña, y socketserver.handle_error los imprime enteros a stderr.
    El problema es que TAPAN lo que sí importa — el único error real del log (un
    JSONDecodeError) estaba enterrado entre 600 tracebacks iguales.

    La regla de la casa (errores explícitos y ruidosos) se mantiene: solo se
    silencian esas dos excepciones, que significan "el cliente se fue". Cualquier
    otra sigue saliendo con su traceback completo.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    server = Servidor((BIND, PORT), Handler)
    print(f"Cacho en http://{BIND}:{PORT}  (Ctrl-C para cortar)")
    try:
        server.serve_forever()
    finally:
        with TABS_LOCK:
            for t in TABS.values():
                t.matar()

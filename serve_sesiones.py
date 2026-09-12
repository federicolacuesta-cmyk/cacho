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
from urllib.parse import parse_qs, quote, urlencode, urlparse

# `areas.py` vive en `tools/` en este repo y en la RAÍZ del repo público de Cacho (que se
# arma con publicar_cacho.py y aplana el árbol). Se prueban los dos lugares en vez de dar por
# hecho uno: si el import fallara, Cacho no abriría — y el repo público es el que menos ojos
# tiene encima para darse cuenta.
_AQUI = os.path.dirname(os.path.abspath(__file__))
for _d in (os.path.dirname(_AQUI), _AQUI):
    if _d not in sys.path:
        sys.path.insert(0, _d)
import areas                                # noqa: E402 — las áreas: cara, color, palabras
# Con qué arranca una pestaña con área. Vive en el repo de la casa, no en el de Cacho:
# afuera el módulo no está y Cacho tiene que abrir igual — una pestaña con área arranca
# sin system prompt, como cualquier otra. Importarlo pelado dejaba el Cacho público sin
# abrir (ModuleNotFoundError antes del server), y ahí es donde menos ojos hay para verlo.
try:
    import agente_arranque                  # noqa: E402
except ImportError:
    agente_arranque = None
# Quién entra y con qué jaula (el usuario vs. Administración). Mismo criterio que los de arriba:
# en el repo público de Cacho este módulo puede no estar, y Cacho tiene que abrir igual — sin
# él, el único que entra es el del PIN de la máquina, que es como fue hasta el 10-set-2026.
try:
    import cacho_perfiles                   # noqa: E402
except ImportError:
    cacho_perfiles = None
import costo_sesion
# La bandeja de archivos de cada sesión (11-set-2026): qué pasó por la charla y sus
# miniaturas. Guardado como los otros: si no viaja, Cacho abre sin bandeja.
try:
    import cacho_bandeja
except ImportError:
    cacho_bandeja = None
# El uso del plan (tubo de la barra lateral). Guardado: en el repo público de Cacho
# este módulo no viaja, y sin él Cacho tiene que abrir igual — muestra "sin dato".
try:
    import uso_claude
except ImportError:
    uso_claude = None

# La firma de tu casa (logo + bandera) para la pantalla de entrada, si tenés un módulo
# `marca_web` con `firma_html(color, alto=, gap=)` dos carpetas más arriba. Si no está,
# la pantalla abre igual, sin firma.
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(_AQUI)))
    import marca_web as _marca
except ImportError:
    _marca = None

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CARPETA_PROYECTOS = os.path.expanduser("~/Claude/Projects")
AQUI = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CACHO_PORT", "8811"))
BIND = "127.0.0.1"   # NO cambiar a 0.0.0.0: expondría terminales por red
CHUNK = 256 * 1024   # bytes que se leen de cabeza/cola de cada transcript
TOPE_COLA = 8 * 1024 * 1024   # hasta dónde se retrocede buscando una línea entera en la cola
TOPE_BUFFER = 2_000_000  # scrollback que se guarda por terminal
COALESCE_MAX = 64 * 1024  # tope al juntar trozos del PTY en un solo evento SSE (ver _stream)
# Tope de lo que se re-manda al ENTRAR de nuevo a una pestaña, cuando no se puede
# mandar solo lo nuevo (ver _stream). Antes se re-mandaban los 2 MB enteros en UN
# evento: el navegador armaba un texto de 2,7 MB, lo decodificaba y se lo daba de
# golpe a la terminal, todo en el hilo que pinta. Medido el 19-ago-2026 con 19
# pestañas abiertas: 9,5 MB para repartir, 2,6 MB la más gorda. Resultado, el panel
# quedaba negro y mudo unos segundos por pestaña, y como al cambiar de pestaña se
# empezaba de cero, la sensación era "no muestra nada en ninguna".
SNAPSHOT_MAX = 512 * 1024
SSE_TROZO = 64 * 1024     # el arranque se manda en pedazos: la pantalla pinta mientras llega

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


def _quien_es(pin):
    """(perfil, quien) para un PIN. `(None, "")` si no es de nadie.

    Es el único lugar donde se decide quién es quién: el PIN de la máquina es el usuario; los de
    `inputs/administracion/acceso.json` son las chicas de Administración, y su pestaña nace
    enjaulada (ver `cacho_perfiles.py`). El orden importa poco pero se prueba primero el de
    el usuario, que es el que se usa cien veces por día."""
    if _pin_igual(pin):
        return "duenio", ""
    if cacho_perfiles is not None:
        quien = cacho_perfiles.quien_por_pin(pin)
        if quien:
            return cacho_perfiles.ADMIN, quien
    return None, ""


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

_sesiones = {}        # token -> {"t": último uso (epoch), "perfil", "quien"}
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
            _sesiones = {}
            for k, v in d.items():
                if not isinstance(k, str):
                    continue
                try:
                    f = _ficha(v)
                except (TypeError, ValueError):
                    continue          # una entrada ilegible se cae sola, no echa a las demás
                if ahora - f["t"] < SESION_VIDA:
                    _sesiones[k] = f
    except FileNotFoundError:
        _sesiones = {}          # primera vez en esta máquina: normal
    except Exception as e:
        # Arrancar de cero acá ECHA A TODOS los dispositivos: hay que volver a
        # tipear el PIN en el teléfono y en la laptop sin saber por qué. Que al
        # menos quede escrito, que es lo único que lo explica después.
        _sesiones = {}
        _log_seguridad(f"sesiones_web.json ilegible ({e!r}): se cerró la sesión "
                       "de TODOS los dispositivos; hay que re-tipear el PIN")


def _sesion_nueva(perfil="duenio", quien=""):
    """El token ya no dice sólo «entró»: dice QUIÉN entró (10-set-2026).

    Hasta hoy Cacho tenía una sola identidad y el token era un timestamp suelto. Con
    Administración adentro, el perfil tiene que viajar con la sesión: es lo que decide si la
    pestaña nace con shell o enjaulada, y qué pestañas ve. Los tokens viejos (un float) siguen
    valiendo como perfil `duenio` — cambiar el formato no puede echar a los dispositivos del usuario,
    que es lo que pasaría si el archivo persistido dejara de entenderse."""
    import secrets
    tok = secrets.token_urlsafe(32)
    with _SES_LOCK:
        _sesiones[tok] = {"t": time.time(), "perfil": perfil, "quien": quien}
        sobran = len(_sesiones) - SESION_MAX
        if sobran > 0:               # se van las más viejas
            for k in sorted(_sesiones, key=lambda k: _sesiones[k]["t"])[:sobran]:
                del _sesiones[k]
        _sesiones_guardar()
    return tok


def _ficha(v):
    """Una entrada del archivo, venga del formato viejo (float) o del nuevo (dict)."""
    if isinstance(v, dict):
        return {"t": float(v.get("t") or 0), "perfil": v.get("perfil") or "duenio",
                "quien": v.get("quien") or ""}
    return {"t": float(v), "perfil": "duenio", "quien": ""}


def _sesion_valida(tok):
    """La ficha de la sesión, o None. Sigue siendo falsy cuando no vale, así que los `if` de
    siempre no cambian de sentido."""
    if not tok:
        return None
    with _SES_LOCK:
        for k in _sesiones:
            if hmac.compare_digest(k, tok):
                _sesiones[k]["t"] = time.time()
                return dict(_sesiones[k])
    return None


# ─── Tickets de un solo uso (para el lanzador de la app) ──────────────────────
# El lanzador abría Chrome con `--app=http://…/?pin=NNNN`. Eso deja el PIN en la
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


def _fin_de(d):
    """Cómo termina la charla, según el ÚLTIMO registro que importa del transcript
    (11-set-2026). Es lo que decide «te espera»; el pty solo lo dirime en un caso.
      termino     → Claude cerró el turno (habló y paró): te espera, sí o sí.
      pensando    → le escribiste, le volvió una herramienta, o dejaste un mensaje
                    en la cola mientras trabajaba: NO te espera, aunque el pty esté
                    quieto (pensar largo no dibuja nada).
      herramienta → pidió una herramienta y todavía no volvió: o está corriendo
                    (spinner → pty vivo) o está pidiendo permiso / una respuesta
                    (pty quieto). Ahí sí manda el pty.
      None        → este registro no dice nada (títulos, snapshots, cola vaciada).
    RAÍZ: el estado salía de «pty quieto ≥ 8 s», y eso miente para los dos lados:
    una pestaña nueva sin nada escrito quedaba «esperando» y entraba en el 🔔; una
    que terminó pero sigue dibujando (aviso, barra) quedaba «trabajando» y no."""
    t = d.get("type")
    if t == "queue-operation":
        return "pensando" if d.get("operation") == "enqueue" else None
    if t == "system" and d.get("subtype") == "turn_duration":
        # Al cerrar el turno Claude Code anota cuántos subagentes siguen corriendo: con
        # alguno pendiente la sesión se despierta SOLA con la task-notification, no espera
        # a nadie (58 turnos así en los transcripts de la casa; policía 11-set-2026).
        return "pensando" if d.get("pendingBackgroundAgentCount") else None
    if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
        return None
    if t == "assistant":
        stop = (d.get("message") or {}).get("stop_reason")
        if stop is None:
            return "pensando"          # mensaje a medio escribir: sigue en eso
        return "herramienta" if stop == "tool_use" else "termino"
    return "pensando"


_RE_ARCHIVO = re.compile(
    r"[\w.\-]+\.(py|js|ts|json|md|html|css|liquid|sh|sql|sqlite|csv|xlsx|png|jpe?g|"
    r"mp4|pdf|plist|txt|yml|yaml|jsonl)\b")
# comandos que no dicen nada de qué se está haciendo
_CMD_MUDOS = {"cd", "cat", "echo", "printf", "true", "sudo", "time", "ls", "pwd", "env",
              "for", "do", "done", "if", "then", "else", "fi", "while", "read", "set"}


def _objeto_de_bash(cmd):
    """Lo más parlante de una línea de comando: el archivo que toca, y si no,
    el programa que corre. Devuelve "" si no hay nada legible — antes salía el
    primer token pelado y en la barra se leían cosas como "cat" o media ruta."""
    m = _RE_ARCHIVO.search(cmd)
    if m:
        return m.group(0)[:26]
    for tok in cmd.split():
        limpio = tok.strip("\"'`(){}$")
        # los programas van en minúscula: si empieza con mayúscula es un pedazo
        # de ruta o de texto, no un comando
        if ("=" in limpio or "/" in limpio or len(limpio) < 3
                or limpio in _CMD_MUDOS or not limpio.isascii()
                or not limpio[0].islower()):
            continue      # dos letras sueltas ("vs") son pedazos de comando, no un programa
        return limpio[:20]
    return ""


def _pistas_de(content):
    """(herramientas, objetos) de un mensaje del asistente.

    Los OBJETOS son lo concreto que se está tocando —el archivo, el programa, el
    dominio—, que es lo que hace entendible una charla mirándola de reojo desde
    la barra: "editando serve_sesiones.py" dice muchísimo más que "Edit". Los
    archivos van primero: son los que mejor identifican de qué se trata."""
    if not isinstance(content, list):
        return [], []
    tools, archivos, otros = [], [], []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        nombre = b.get("name", "?")
        tools.append(nombre)
        inp = b.get("input")
        if not isinstance(inp, dict):
            continue
        ruta = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
        if ruta:
            archivos.append(os.path.basename(str(ruta).rstrip("/"))[:26] or str(ruta)[:26])
            continue
        if nombre == "Bash" and inp.get("command"):
            obj = _objeto_de_bash(str(inp["command"]))
            if obj:
                (archivos if _RE_ARCHIVO.fullmatch(obj) else otros).append(obj)
            continue
        url = inp.get("url")
        if url:
            otros.append(re.sub(r"^https?://(www\.)?", "", str(url)).split("/")[0][:28])
            continue
        for k in ("pattern", "query", "description"):
            if inp.get(k):
                otros.append(_limpiar(str(inp[k]), 26))
                break
    return tools, archivos + otros


# --- De qué va la charla (tema + ícono) ------------------------------------
# La barra tenía una sola pista de contenido: el título que escribe el CLI una
# vez, al principio, y que ya no vuelve a cambiar (verificado 21-ago-2026: en un
# transcript hay 31 líneas `ai-title` y las 31 dicen lo mismo). Una charla que
# arranca en una cosa y termina en otra queda rotulada con la primera para
# siempre, y a veces con dos palabras ("Images") que a los tres días no
# significan nada. Esto le pone al lado un TEMA deducido de lo último que pasó,
# con su ícono, y avisa cuando el tema de ahora no es el del arranque.
#
# Es deliberadamente tonto —contar palabras, sin modelo—: corre en cada lectura
# de transcript, tiene que ser instantáneo y sobre todo NO puede inventar. Si no
# reconoce nada, no muestra nada, que es mejor que rotular mal.
TEMAS_BASE = [
    ("🚓", "auditoría", ["auditor", "policía", "policia", "vulnerab", "seguridad",
                         "revisar a fondo", "code review", "code-review", "revisión"]),
    ("🐛", "arreglo", ["error", "falla", "fallo", "bug", "roto", "rota", "no anda",
                       "traceback", "excepción", "arreglar", "arreglá", "se cuelga"]),
    ("🧮", "números", ["contab", "factur", "banco", "bancari", "iva", "costo",
                       "presupuesto", "caja", "balance", "gasto", "cobranza", "saldo"]),
    ("📊", "informes", ["informe", "reporte", "brief", "resumen", "dashboard",
                        "panel", "monitor", "métrica", "planilla"]),
    ("🗃️", "datos", ["sqlite", "sql", "select ", "base de datos", "consulta",
                     "tabla", "query", "join ", "csv"]),
    ("⚙️", "procesos", ["launchd", "plist", "rutina", "cron", "automat", "job",
                        "sync", "daemon", "servidor", "server", "puerto"]),
    ("🌐", "web", ["theme", "liquid", "css", "html", "landing", "seo", "sitio",
                   "página web", "navegador", "chrome"]),
    ("📣", "difusión", ["campaña", "anuncio", "creativ", "publicid", " ads",
                        "pauta", "posteo", "reel", "audiencia"]),
    ("💬", "mensajes", ["whatsapp", "mensaje", "chat", "mail", "correo",
                        "notificación", "aviso"]),
    ("🖼️", "imagen", [".png", ".jpg", "imagen", "foto", "diseño", "render",
                      "video", ".mp4", "logo"]),
    ("📝", "escritura", ["documento", "redact", "artículo", "nota ", ".md",
                         "texto", "escribir"]),
    ("🧑‍💻", "código", [".py", ".js", "función", "script", "refactor", "código",
                       "commit", "git "]),
]
# Ampliable sin tocar código: mismo formato, en el estado de la app. Lo de acá
# arriba es vocabulario general; el de cada uno (nombres de sistemas, de gente,
# de proyectos) va en ese archivo, que no viaja con el programa.
TEMAS_FILE = os.path.expanduser("~/Library/Application Support/Cacho/temas.json")
_temas_cache = {"mtime": -1, "datos": []}


def _temas():
    """TEMAS_BASE + los del archivo del usuario, que valen DOBLE: nombran cosas
    de la casa ("cristales", "franquicias") y por eso aciertan más que una
    palabra genérica que puede aparecer en cualquier charla."""
    try:
        m = os.path.getmtime(TEMAS_FILE)
    except OSError:
        m = 0
    if m != _temas_cache["mtime"]:
        propios = []
        if m:
            try:
                with open(TEMAS_FILE, encoding="utf-8") as fh:
                    d = json.load(fh)
                for t in (d.get("temas") or []):
                    palabras = [str(p).lower() for p in (t.get("palabras") or []) if p]
                    if t.get("nombre") and palabras:
                        propios.append((t.get("icono") or "•", str(t["nombre"]),
                                        palabras, 2))
            except Exception as e:
                print(f"⚠️ temas.json ilegible, sigo con los de fábrica: {e}", file=sys.stderr)
                propios = []
        _temas_cache.update(
            mtime=m, datos=propios + [(i, n, p, 1) for i, n, p in TEMAS_BASE])
    return _temas_cache["datos"]


def _tema_de(texto):
    """(icono, nombre) del tema que más pesa en ese texto, o ("", "")."""
    if not texto:
        return "", ""
    t = texto.lower()
    mejor, puntaje_mejor = None, 0
    for icono, nombre, palabras, peso in _temas():
        p = sum(t.count(w) for w in palabras) * peso
        if p > puntaje_mejor:
            mejor, puntaje_mejor = (icono, nombre), p
    if not mejor or puntaje_mejor < 3:
        return "", ""      # una mención suelta no es un tema
    return mejor


# Cuántos mensajes del final miro para decir "de qué va ahora". Pocos y la charla
# parece cambiar de tema cada vez que se contesta un "dale"; muchos y no se entera
# nunca de que cambió.
VENTANA_TEMA = 24
CONFIRMACIONES = {
    "dale", "si", "sí", "ok", "oka", "okey", "listo", "bien", "perfecto", "genial",
    "gracias", "no", "nop", "sip", "claro", "exacto", "seguí", "segui", "continuá",
    "continua", "andá", "anda", "hacelo", "dale gracias", "buenísimo",
    "buenisimo", "gracias!", "ahora sí", "ahora si", "bárbaro", "barbaro", "gracias.",
}

_VERBOS = [
    (("Edit", "Write", "NotebookEdit", "MultiEdit"), "✎ editando"),
    (("Bash", "BashOutput"), "⌘ corriendo"),
    (("Read", "Grep", "Glob", "Agent", "Task"), "👀 mirando"),
    (("WebSearch", "WebFetch"), "🔎 buscando"),
]


def _verbo_de(tools):
    for nombres, verbo in _VERBOS:
        if any(t in nombres for t in tools):
            return verbo
    for t in tools:
        if t.startswith("mcp__"):
            return "🔌 " + t.split("__")[1][:14]
    return ""


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
            # La cola tiene que traer al menos UNA línea entera: si el último registro es
            # una imagen leída (1,35 MB en base64), 256 KB no alcanzan y el estado salía de la
            # CABEZA del transcript (policía 11-set-2026). Se retrocede hasta encontrar un
            # salto de línea anterior, con tope.
            desde = max(CHUNK, st.st_size - CHUNK)
            while True:
                fh.seek(desde)
                tail = fh.read()
                if b"\n" in tail[:-1] or desde <= CHUNK or st.st_size - desde >= TOPE_COLA:
                    break
                desde = max(CHUNK, desde - 4 * CHUNK)

    head_lines = head.decode("utf-8", "replace").splitlines()
    if st.st_size > CHUNK:
        head_lines = head_lines[:-1]
    tail_lines = tail.decode("utf-8", "replace").splitlines()[1:] if tail else []

    s = {
        "id": os.path.basename(path)[:-6],
        "cwd": "", "branch": "", "titulo": "", "primer_msg": "",
        "primer_ts": "", "ultimo_quien": "", "ultimo_txt": "",
        "mtime": st.st_mtime, "auto": False,
        # de qué va AHORA (ver el bloque de temas, más arriba)
        "icono": "", "tema": "", "tema_ini": "", "area": areas.DEFECTO,
        "contexto": "", "pedido": "",
        "fin": "",   # cómo termina la charla (ver _fin_de): decide «te espera»
        # cuánto pesa arrastrar esta charla (24-ago-2026). Sale de tail_lines,
        # que ya se leyó: no agrega ni una lectura de disco al refresco del panel.
        "ctx_tokens": 0, "peso": costo_sesion.VERDE, "peso_nota": "",
    }

    iniciales = []      # el arranque de la charla, para saber después si mutó
    for line in head_lines:
        d = _parse_linea(line)
        if not d:
            continue
        t = d.get("type")
        if (t in ("user", "assistant") and not d.get("isMeta")
                and not d.get("isSidechain") and len(iniciales) < VENTANA_TEMA):
            txt_ini, _ = _texto_de((d.get("message") or {}).get("content"))
            if txt_ini:
                iniciales.append(_limpiar(txt_ini, 400))
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

    # Lo último que pasó, para deducir el tema de ahora: texto de los últimos
    # mensajes, la herramienta más reciente (el verbo) y los archivos tocados.
    recientes, tools_rec, objs_rec = [], [], []
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
        if not s["fin"]:
            s["fin"] = _fin_de(d) or ""
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        contenido = (d.get("message") or {}).get("content")
        txt, tools = _texto_de(contenido)
        if len(recientes) < VENTANA_TEMA:
            if txt:
                recientes.append(_limpiar(txt, 400))
            if d.get("type") == "assistant":
                herr, objs = _pistas_de(contenido)
                if herr and not tools_rec:
                    tools_rec = herr          # la más nueva manda: es lo que hace ahora
                for o in objs:
                    if o and o not in objs_rec and len(objs_rec) < 3:
                        objs_rec.append(o)
        # EL PEDIDO: el último mensaje tuyo que pide algo, no el último cualquiera.
        # "dale", "sí", "gracias" no explican nada de la charla, y son la mitad de
        # lo que uno escribe.
        if d.get("type") == "user" and txt and not s["pedido"]:
            limpio = _limpiar(txt, 150)
            # "toolu_" = eco de una herramienta que volvió por el canal del
            # usuario, no algo que haya escrito nadie
            if (len(limpio) >= 14 and "toolu_" not in limpio
                    and limpio.lower().strip(" .!¡") not in CONFIRMACIONES):
                s["pedido"] = limpio
        if s["ultimo_quien"]:
            continue
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
        # tema del arranque vs. tema de ahora. Se comparan para poder avisar que
        # la charla se fue a otro lado (que es cuando el título viejo engaña).
        arranque = s["titulo"] + " " + " ".join(iniciales)
        _, s["tema_ini"] = _tema_de(arranque)
        s["icono"], s["tema"] = _tema_de(" ".join(recientes) + " " + " ".join(objs_rec))
        if not s["tema"]:                     # sin señal reciente, vale la del arranque
            s["icono"], s["tema"] = _tema_de(arranque)
        # De qué ÁREA es esta sesión (23-ago-2026). Del mismo texto que el tema, porque es
        # lo que la sesión está haciendo AHORA: una charla que arrancó en pauta y terminó en
        # el banco es de Xara, no de Jaime. Lo que decide de verdad es el arranque + lo
        # reciente juntos; con lo reciente solo, un «dale, gracias» al final movía la sesión
        # de área. Si el usuario la corrigió a mano, esto queda pisado en `_aplicar_meta`.
        s["area"] = areas.de_texto(arranque + " " + " ".join(recientes) + " "
                                   + " ".join(objs_rec))
        verbo = _verbo_de(tools_rec)
        partes = [p for p in (verbo, ", ".join(objs_rec[:2])) if p]
        s["contexto"] = " ".join(partes)
        s["ctx_tokens"] = costo_sesion.contexto_de_lineas(tail_lines or head_lines)
        s["peso"] = costo_sesion.veredicto(s["ctx_tokens"])
        s["peso_nota"] = costo_sesion.en_criollo(s["ctx_tokens"])
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

    Lee la COLA del archivo hasta juntar la charla, NO hasta llenar un cupo de bytes.
    Esa diferencia es todo el asunto (10-set-2026): una pestaña que mira creativos
    guarda cada imagen que abre como base64 adentro del transcript —45 líneas de
    hasta 1,3 MB sobre 22 MB totales, o sea el 90% del archivo en algo que no aporta
    una palabra a la conversación—, así que la ventana de bytes se llenaba de
    imágenes y las charlas de Jaime se veían con 12 mensajes de 250. Ahora la
    escalera sube hasta el archivo entero y el corte lo pone `max_msgs`; el base64
    se saca ANTES de parsear (era el costo que obligaba a leer poca cola).
    El resultado se cachea por (mtime, tamaño): el visor se repinta cada 4 s y una
    sesión terminada ya no cambia nunca."""
    st = os.stat(path)
    clave = (path, st.st_mtime, st.st_size)
    with _conv_lock:
        hit = _cache_conv.get(path)
        if hit and hit[0] == clave:
            return hit[1]

    titulo, items, recortado = "", [], False
    for tope in (1024 * 1024, 8 * 1024 * 1024, 32 * 1024 * 1024, st.st_size):
        titulo, items = _parsear_cola(path, st, tope)
        recortado = st.st_size > tope
        if len(items) >= max_msgs or not recortado:
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


# El base64 de una imagen adentro del transcript. Se lo saca ANTES de `json.loads`:
# una sola de esas líneas pesa 1,3 MB, el visor la parsea entera y `_texto_de` la tira
# igual (es un tool_result, no habla). Sacarlo es lo que hace barato leer la cola larga.
_RE_B64 = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=]{2000,}"')


def _sin_imagenes(linea):
    return _RE_B64.sub('"data":""', linea) if len(linea) > 100_000 else linea


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
        d = _parse_linea(_sin_imagenes(line))
        if not d:
            continue
        if d.get("type") == "ai-title" and d.get("aiTitle"):
            titulo = d["aiTitle"]
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        txt, tools = _texto_de((d.get("message") or {}).get("content"))
        ts = _hora_local(d.get("timestamp") or "")  # HH:MM
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


# El proyecto donde vive este programa. Casi todas las charlas son de ahí, así que
# repetir su nombre en cada renglón de la barra ocupa el lugar donde iría algo que
# sí distingue una charla de otra: la barra solo lo escribe cuando NO es este.
# Se deduce de dónde está el archivo, para que la copia de otra máquina —u otra
# persona— acierte sola sin que nadie configure nada.
PROY_CASA = _proyecto_lindo(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _hora_local(ts):
    """HH:MM en la hora de la máquina. El transcript viene en UTC («…Z»): hasta el
    11-set-2026 el visor mostraba `ts[11:16]` crudo y toda la charla se leía tres
    horas adelantada (lo destapó la bandeja de archivos, que la convertía bien)."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except Exception:
        return ts[11:16]


def _iso_a_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception as e:
        print(f"⚠️ timestamp raro en sesión ({ts!r}), lo tomo como 0: {e}", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# Procesos `claude` vivos fuera de la app
# ---------------------------------------------------------------------------

_proc_cache = {}  # pid -> {tty, cwd, start, sid}; nada de eso cambia en vida del proceso

# Subcomandos del CLI que NO son una charla: el proceso se llama `claude` igual y
# `pgrep -x claude` lo trae, pero no tiene transcript. Sin esta lista, el emparejado
# de abajo le regalaba ese proceso a la sesión libre más cercana de la misma carpeta
# y una charla TERMINADA figuraba viva en «En Terminal (afuera)» para siempre
# (26-ago-2026: `claude remote-control` del tmux `claude-rc`, prendido desde el martes,
# mantenía "Brief Reunion Supervisores" —cerrada hacía 22 h— colgada en la barra).
NO_ES_CHARLA = {
    "remote-control", "mcp", "config", "doctor", "update", "install",
    "migrate-installer", "plugin", "setup-token", "agents", "monitor",
}


def _subcomando_y_sid(pid):
    """(subcomando, session-id) de la línea de comandos del proceso.

    El subcomando es el PRIMER argumento, y solo si no es una opción: así lo pide el
    CLI (`claude remote-control …`). Mirar "el primer no-flag" en cualquier posición
    sería adivinar — el valor de un flag (`--session-id <uuid>`, `--model <x>`) también
    es un token sin guiones. El session-id se lee del `--session-id` cuando está:
    emparejar por él es exacto y no adivina.
    """
    try:
        linea = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as e:
        print(f"⚠️ no pude leer la línea de comandos del proceso claude {pid}: {e}",
              file=sys.stderr)
        return "", ""
    args = linea.split()[1:]
    sub = args[0] if args and not args[0].startswith("-") else ""
    sid = ""
    if "--session-id" in args:
        i = args.index("--session-id")
        if i + 1 < len(args):
            sid = args[i + 1]
    return sub, sid


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
                sub, sid = _subcomando_y_sid(pid)
                info = {"tty": tty, "cwd": cwd, "start": start,
                        "sid": sid, "sub": sub}
                _proc_cache[pid] = info
            except Exception as e:
                print(f"⚠️ no pude leer ps/lsof del proceso claude {pid} (lo salteo): {e}", file=sys.stderr)
                continue
        if info["tty"] in ttys_excluidos:
            continue  # es una pestaña nuestra
        if info.get("sub") in NO_ES_CHARLA:
            continue  # es el CLI haciendo otra cosa, no una charla con transcript
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


# El id de la charla lo elige la pestaña y se lo IMPONE a claude (`--session-id`).
# Se comprueba UNA vez que esta máquina lo acepte: si tuviera una versión vieja del
# CLI, arrancar con una opción desconocida dejaría la pestaña muerta al instante.
_SESSION_ID_OK = None
_SESSION_ID_LOCK = threading.Lock()
_PATH_PESTANA = ("$HOME/.local/bin:$HOME/.claude/local:$HOME/.npm-global/bin:"
                 "/opt/homebrew/bin:/usr/local/bin:$PATH")


_CLAUDE_BIN = None
_BIN_LOCK = threading.Lock()


def _binario_claude():
    """La RUTA del ejecutable, cacheada. Vacío si no está en esta máquina.

    Una pestaña enjaulada NO puede nacer en una shell de login. El alias de la casa
    (`~/.zshrc`) es `claude --dangerously-skip-permissions`: la bandera se la pone el alias,
    no el código, así que un `--settings` con restricciones sería papel pintado mientras la
    pestaña salga de esa shell. `command -v` devuelve el ejecutable, no el alias."""
    global _CLAUDE_BIN
    with _BIN_LOCK:
        if _CLAUDE_BIN is None:
            try:
                r = subprocess.run(
                    ["/bin/zsh", "-lc", f'PATH="{_PATH_PESTANA}"; command -v claude'],
                    capture_output=True, text=True, timeout=30)
                ruta = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
                _CLAUDE_BIN = ruta if ruta.startswith("/") and os.access(ruta, os.X_OK) else ""
            except (OSError, subprocess.SubprocessError, IndexError):
                _CLAUDE_BIN = ""
    return _CLAUDE_BIN


def _soporta_session_id():
    """Si el `claude` de esta máquina acepta --session-id (cacheado)."""
    global _SESSION_ID_OK
    with _SESSION_ID_LOCK:
        if _SESSION_ID_OK is None:
            try:
                salida = subprocess.run(
                    ["/bin/zsh", "-lc", f'PATH="{_PATH_PESTANA}"; claude --help'],
                    capture_output=True, text=True, timeout=30).stdout
                _SESSION_ID_OK = "--session-id" in salida
                if not _SESSION_ID_OK:
                    print("⚠️ este claude no acepta --session-id: las pestañas nuevas "
                          "vuelven a emparejarse con su charla a ojo (títulos que se "
                          "pueden cruzar). Actualizá Claude Code.", file=sys.stderr)
            except Exception as e:
                # No poder preguntar no es razón para romper la pestaña: se asume que no.
                print(f"⚠️ no pude ver si claude acepta --session-id, sigo sin él: {e}",
                      file=sys.stderr)
                _SESSION_ID_OK = False
        return _SESSION_ID_OK


def arranque_stream(buf, escritos, desde):
    """Qué mandarle a un navegador que se (re)conecta a una pestaña.

    Devuelve (bytes a mandar, si tiene que borrar lo que ya pintó, byte en que arranca).

    `desde` es hasta qué byte dice tener el navegador; -1 (o basura) = no tiene nada.
    Si lo que le falta sigue en el buffer, se le manda SOLO eso y no se le borra la
    pantalla: volver a una pestaña deja de costar. Si viene de cero o quedó tan atrás
    que el pedazo que le falta ya se recortó, se rearma la pantalla — pero con la COLA
    del scrollback, nunca con los 2 MB enteros (que era lo que dejaba el panel negro
    y mudo unos segundos por pestaña, 19-ago-2026).
    """
    atraso = escritos - desde
    # el delta también se topea: si mientras mirabas otra pestaña esta escupió 1,9 MB,
    # mandárselos "porque los tenemos" sería el mismo atragantón que se está sacando.
    # Pasado el tope, la cola sola ya alcanza (es todo redibujado de la misma pantalla).
    if 0 <= desde <= escritos and atraso <= min(len(buf), SNAPSHOT_MAX):
        return (bytes(buf[len(buf) - atraso:]) if atraso else b""), False, desde
    cola = bytes(buf[-SNAPSHOT_MAX:])
    return cola, True, escritos - len(cola)


class TermSession:
    def __init__(self, cwd, resume_id="", modelo="", area="", jaula=None):
        # `jaula`: la pestaña es de ADMINISTRACIÓN (ver cacho_perfiles.py). Cambia tres cosas
        # y ninguna es cosmética: no hay shell, la carpeta es la suya (no el repo) y cada
        # herramienta pasa por tools/xara_guardia.py antes de correr.
        self.jaula = jaula or None
        # De quién es esta pestaña. `duenio` es el dueño de siempre; con jaula, la persona.
        # Sin esto, Flo abriría Cacho y vería —y podría escribir en— las pestañas del usuario.
        self.duenio = (jaula or {}).get("quien") or "duenio"
        if self.jaula:
            cwd = self.jaula["cwd"]
            area = self.jaula.get("area") or area
        # `area`: con quién se abre la charla. Hasta el 5-set-2026 sólo se guardaba como
        # metadato (color, tira) y la sesión nacía PELADA: el usuario tocaba a Carla y hablaba
        # con nadie. Ahora arranca sabiendo quién es y qué leer (agente_arranque.py).
        self.area = area if (area and areas.valida(area)) else ""
        # `modelo`: pedido explícito para ESTA pestaña (p. ej. una cita premium que
        # necesita Fable). Vacío = el default de la máquina (~/.cacho_modelo).
        self.modelo_pedido = modelo
        self.id = uuid.uuid4().hex[:8]
        self.cwd = cwd
        self.creado = time.time()
        self.last_out = time.time()
        self.buf = bytearray()
        # total de bytes que esta pestaña escupió DESDE SIEMPRE (el buf se recorta,
        # esto no). Es la regla que le deja al navegador pedir "dame de acá en
        # adelante" al volver a una pestaña, en vez de rearmarla entera. Ver _stream.
        self.escritos = 0
        self.subs = []
        self.lock = threading.Lock()
        # retomar una charla terminada: claude --resume sigue en el MISMO
        # transcript, así que se vincula de entrada (título y estado al toque)
        self.resume_id = resume_id
        # Charla NUEVA: el id lo elige la pestaña y se lo impone a claude con
        # --session-id, así queda pegada a SU transcript por construcción. Antes se
        # adivinaba después comparando la hora de creación de la pestaña con la del
        # primer mensaje de cada charla, y eso se cruzaba cuando varias arrancaban
        # en la misma carpeta con poco tiempo entre una y otra: el título mostrado
        # era el de la charla de al lado (visto 19-ago-2026 con tres corridas
        # automáticas lanzadas en el mismo minuto).
        self.sid_propio = "" if resume_id or not _soporta_session_id() else str(uuid.uuid4())
        self.transcript_id = resume_id or self.sid_propio
        self.titulo = ""

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE")}  # sin herencia de Claude Code:
        # si el server se levantó desde una sesión de Claude, esas variables
        # harían que las pestañas arranquen como "sesión hija" sin transcript
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        if self.jaula:
            # Las claves de la casa viven en el entorno del server (tu módulo de claves las
            # lee de ahí). Heredarlas en una pestaña enjaulada sería dejar las llaves
            # adentro de la jaula: no hace falta que el modelo tenga mala intención, alcanza
            # con que alguna herramienta imprima el entorno.
            for k in list(env):
                if re.search(r"KEY|TOKEN|SECRET|PASSW|_PW\b|CREDENTIAL", k, re.I):
                    del env[k]
            env.update(self.jaula.get("env") or {})
        master, slave = os.openpty()
        self.master = master
        self.tty = os.ttyname(slave)  # /dev/ttysNNN

        TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x20007461)

        def preexec():
            os.setsid()
            fcntl.ioctl(slave, TIOCSCTTY, 0)

        if self.jaula:
            # SIN SHELL, a propósito (ver _binario_claude). Si el binario no está, la pestaña
            # muere con un cartel en pantalla en vez de caer a una shell abierta: fallar hacia
            # el lado seguro es lo único que puede hacer una jaula que no se pudo armar.
            self.proc = subprocess.Popen(
                self._orden_enjaulada(), cwd=cwd, env=env,
                stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=preexec, close_fds=True)
        else:
            # shell interactiva de login: mismas alias/entorno que su Terminal
            self.proc = subprocess.Popen(
                ["/bin/zsh", "-il"], cwd=cwd, env=env,
                stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=preexec, close_fds=True)
        os.close(slave)
        threading.Thread(target=self._leer, daemon=True).start()
        if not self.jaula:
            threading.Thread(target=self._auto_claude, daemon=True).start()

    def _orden_enjaulada(self):
        """La línea de comando de una pestaña de Administración."""
        binario = _binario_claude()
        if not binario:
            return ["/bin/echo", "No encuentro Claude Code en esta máquina; avisale a el usuario."]
        cmd = [binario, "--settings", self.jaula["settings"]]
        modelo = self.modelo_pedido or _modelo_preferido()
        if modelo:
            cmd += ["--model", modelo]
        if self.resume_id:
            cmd += ["--resume", self.resume_id]
        elif self.sid_propio:
            cmd += ["--session-id", self.sid_propio]
        if self.area and agente_arranque:
            arranque = agente_arranque.archivo(self.area)
            if arranque:
                cmd += ["--append-system-prompt-file", arranque]
        return cmd

    def _auto_claude(self):
        time.sleep(0.9)  # que la shell levante el prompt
        # `claude` puede no estar en el PATH de la shell nueva (el instalador
        # oficial lo deja en ~/.local/bin y el de npm en ~/.npm-global/bin, como
        # en la MacBook). Si tampoco está ahí, decirlo en pantalla en vez de
        # dejar la pestaña negra.
        # resume_id ya viene validado ([0-9a-f-]) y sid_propio es un uuid4 nuestro:
        # los dos son seguros para la línea de comando
        if self.resume_id:
            claude = "claude --resume " + self.resume_id
        elif self.sid_propio:
            claude = "claude --session-id " + self.sid_propio
        else:
            claude = "claude"
        # El modelo: primero el pedido explícito de la pestaña, si no
        # ~/.cacho_modelo (tu default para toda pestaña nueva, si lo escribiste).
        # Sin archivo, el comportamiento es el de siempre: el default de la máquina.
        modelo = self.modelo_pedido or _modelo_preferido()
        if modelo:
            # entre comillas SIEMPRE: el sufijo de contexto va entre corchetes
            # (`claude-opus-5[1m]`) y zsh lo toma como glob → "no matches found".
            claude += " --model '" + modelo + "'"
        # El área: quién es y qué leer, por system prompt. Es la ÚNICA forma de dárselo a una
        # pestaña de la interfaz (no tiene prompt donde anteponer nada). El archivo se
        # reescribe en cada arranque desde la memoria y la skill vigentes; la ruta la pone
        # ese módulo (sin espacios ni comillas) y va entre comillas igual.
        if self.area and agente_arranque:
            arranque = agente_arranque.archivo(self.area)
            if arranque:
                claude += " --append-system-prompt-file '" + arranque + "'"
        cmd = (
            f'PATH="{_PATH_PESTANA}"; '
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
                self.escritos += len(msg)
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
                self.escritos += len(d)
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

# ─── Las pestañas SOBREVIVEN al reinicio del server (11-set-2026) ─────────────────────
# RAÍZ. Reiniciar Cacho (código nuevo → `reiniciar_cacho.sh`, o el diferido que espera el
# hueco) mataba TODAS las pestañas vivas: quedaban retomables con ⟳ abajo, pero el usuario no las
# retomaba — volvía, veía la barra vacía con UNA pestaña pelada abierta por el arranque, y
# reabría las alertas desde el panel, rehaciendo de cero lo que ya estaba hecho. El 11-set
# a las 11:49 el diferido encontró «hueco» (9 charlas quietas ≥38 min, teclado quieto 101
# min: el usuario trabaja desde el teléfono) y se llevó 9 pestañas, incluida una de 10 MB de
# Jaime y las de cierre de mes y policía; a las 13:15 el usuario reabrió 6 alertas y repitió el
# trabajo. La regla del hueco estaba bien: la pestaña que ESPERA a el usuario es justamente la
# que más vale, y ninguna heurística de "nadie la usa" la distingue de una abandonada.
# Entonces el reinicio no decide: guarda lo que había y lo vuelve a abrir. Cada pestaña
# vuelve con `--resume` sobre SU transcript, con su área y su modelo pedido; el id de
# pestaña es nuevo (uuid corto de este arranque) y el front, al ver pestañas vivas, ya no
# abre la pelada. Sólo se guardan las del usuario (la jaula la arma el server por quién entró).
PESTANAS_AL_REINICIAR = os.path.join(META_DIR, "pestanas_al_reiniciar.json")


def _guardar_pestanas_vivas():
    """Anota las pestañas vivas para que el próximo arranque las reabra. Nunca lanza:
    corre en el cierre, y un error acá no puede impedir que el server termine."""
    try:
        with TABS_LOCK:
            vivas = [{"sid": t.transcript_id, "cwd": t.cwd, "area": t.area,
                      "modelo": t.modelo_pedido}
                     for t in TABS.values()
                     if t.viva and t.transcript_id and not t.jaula]
        os.makedirs(META_DIR, exist_ok=True)
        tmp = PESTANAS_AL_REINICIAR + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(vivas, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, PESTANAS_AL_REINICIAR)
        print(f"cierre: {len(vivas)} pestaña(s) anotadas para reabrir", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ no pude anotar las pestañas vivas al cerrar: {e!r}", file=sys.stderr)


def _restaurar_pestanas():
    """Reabre (con --resume) las pestañas que el cierre anterior anotó. El archivo se
    consume: si el server se cae dos veces seguidas, la segunda arranca limpia y no
    reabre por duplicado. Una pestaña que no se pudo reabrir se dice, no se calla."""
    try:
        with open(PESTANAS_AL_REINICIAR, encoding="utf-8") as fh:
            vivas = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"⚠️ {PESTANAS_AL_REINICIAR} ilegible ({e!r}): no reabro nada", file=sys.stderr)
        vivas = []
    try:
        os.remove(PESTANAS_AL_REINICIAR)
    except OSError:
        pass
    abiertas = 0
    for v in vivas if isinstance(vivas, list) else []:
        sid, cwd = v.get("sid") or "", v.get("cwd") or ""
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid) or not os.path.isdir(cwd):
            print(f"⚠️ no reabro {sid[:8]!r}: sid o carpeta inválidos", file=sys.stderr)
            continue
        if not _buscar_transcript(sid):
            print(f"⚠️ no reabro {sid[:8]}: su transcript ya no está", file=sys.stderr)
            continue
        with TABS_LOCK:
            if len(TABS) >= MAX_TABS:
                print(f"⚠️ tope de {MAX_TABS} pestañas: no reabro {sid[:8]}", file=sys.stderr)
                break
        try:
            t = TermSession(cwd, resume_id=sid, modelo=v.get("modelo") or "",
                            area=v.get("area") or "")
        except Exception as e:
            print(f"⚠️ no pude reabrir {sid[:8]}: {e!r}", file=sys.stderr)
            continue
        with TABS_LOCK:
            TABS[t.id] = t
        abiertas += 1
    if vivas:
        print(f"arranque: {abiertas} de {len(vivas)} pestaña(s) reabiertas tras el reinicio",
              file=sys.stderr)

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
    # El área que puso el usuario a mano gana siempre sobre la que dedujo el clasificador, y no se
    # vuelve a discutir: `de_texto` corre en cada lectura (cada 4 s) y sin esto le movería la
    # sesión de área abajo de la mano justo después de corregirla.
    # `areas.valida` y no `m["area"]` a secas: si mañana se saca un área de `areas.py`, las
    # sesiones que el usuario había mandado ahí quedarían apuntando a una clave que ya no existe —
    # sin cara, sin color y fuera de todos los filtros, sin un solo error. Con la validación,
    # esas vuelven solas a manos del clasificador.
    guardada = areas.valida(m.get("area") or "")
    if guardada:
        item["area"] = guardada
        item["area_propia"] = True
    else:
        item.setdefault("area", areas.DEFECTO)
    if m.get("nombre"):
        item["titulo"] = m["nombre"]
        item["renombrada"] = True
    return item


def _vincular_transcripts(tabs, parseadas):
    """Asocia cada pestaña de la app con su transcript (título y estado).

    RED DE SEGURIDAD, no el camino normal: desde el 19-ago-2026 las pestañas
    nuevas nacen con su id de charla puesto por `--session-id`, así que llegan acá
    ya vinculadas. Esto solo emparcha lo que quedó suelto: pestañas abiertas antes
    de ese cambio, una máquina con un CLI viejo que no acepte la opción, o alguien
    que escribió `claude` a mano dentro de una pestaña. Adivinar por hora se cruza
    cuando arrancan varias charlas juntas en la misma carpeta.

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


def estado_general(duenio="duenio"):
    """La barra de Cacho, vista por QUIEN pregunta (10-set-2026).

    Una sesión de Administración ve sus pestañas y sus charlas, no las del usuario. El filtro va
    acá —en el que arma la respuesta— y no en el front: lo que no se manda no se puede
    mostrar por error, y el front es lo único que un navegador puede cambiar."""
    ahora = time.time()
    _cosechar_tabs(ahora)
    parseadas = _sesiones_parseadas()
    meta = _meta_leer()
    with TABS_LOCK:
        tabs = list(TABS.values())
    if duenio != "duenio":
        propia = os.path.realpath(cacho_perfiles.TRABAJO) if cacho_perfiles else ""
        tabs = [t for t in tabs if t.duenio == duenio]
        parseadas = [p for p in parseadas
                     if propia and os.path.realpath(p.get("cwd") or "/") == propia]
    usadas = _vincular_transcripts(tabs, parseadas)

    tabs_json = []
    for t in sorted(tabs, key=lambda x: x.creado):
        p = next((x for x in parseadas if x["id"] == t.transcript_id), None)
        quieto = ahora - t.last_out
        if not t.viva:
            estado = "terminada"
        elif p and p["fin"] == "termino":
            # Claude cerró el turno: te espera, aunque la TUI siga dibujando.
            estado = "esperando"
        elif p and p["fin"] == "pensando":
            # Le escribiste (o le volvió una herramienta): está en eso. El pty
            # quieto no dice nada acá — pensar largo no dibuja. El único freno es
            # un transcript frío CON pty quieto: se cayó y volvió al prompt.
            estado = ("trabajando"
                      if quieto < 8 or (ahora - p["mtime"]) < 60 else "esperando")
        elif p:
            # En una herramienta (o transcript sin registro que diga nada): la
            # TUI redibuja su spinner mientras corre, así que pty quieto ≥8 s =
            # está pidiendo permiso o una respuesta. Ahí sí manda el pty.
            estado = "trabajando" if quieto < 8 else "esperando"
        else:
            estado = "trabajando" if quieto < 6 else "esperando"
        # «Te espera» ≠ «quieta»: una pestaña nueva sin nada escrito está quieta y
        # no espera nada. Es lo que cuenta y muestra el 🔔 (11-set-2026).
        te_espera = (estado == "esperando" and bool(p)
                     and p["fin"] in ("termino", "herramienta"))
        tabs_json.append(_aplicar_meta({
            "id": t.id, "cwd": t.cwd, "proyecto": _proyecto_lindo(t.cwd),
            "titulo": t.titulo or "Nueva sesión", "estado": estado,
            "te_espera": te_espera,
            "viva": t.viva, "quieto_seg": int(quieto),
            "hace_seg": int(ahora - (p["mtime"] if p else t.last_out)),
            "ultimo_quien": p["ultimo_quien"] if p else "",
            "ultimo_txt": p["ultimo_txt"] if p else "",
            "icono": p["icono"] if p else "",
            "tema": p["tema"] if p else "",
            "area": (p or {}).get("area", areas.DEFECTO),
            "tema_ini": p["tema_ini"] if p else "",
            "contexto": p["contexto"] if p else "",
            "pedido": p["pedido"] if p else "",
            # El semáforo de peso también en las pestañas VIVAS. Sin esto el
            # cartel rojo con «Nueva» (que solo se dibuja en la activa) no
            # aparecía nunca donde importa: las de la app eran las únicas sin
            # el dato (24-ago-2026).
            "ctx_tokens": (p or {}).get("ctx_tokens", 0),
            "peso": (p or {}).get("peso", costo_sesion.VERDE),
            "peso_nota": (p or {}).get("peso_nota", ""),
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
    # 2) el proceso que trae `--session-id` DICE cuál es la suya: se le cree y no se
    #    adivina. (Un `claude --resume` sin ese flag sigue cayendo en la heurística
    #    de abajo, que es para lo que se escribió.)
    por_id = {s["id"]: s for s in afuera}
    for pid, pr in vivos.items():
        s = por_id.get(pr.get("sid") or "")
        if s and not s["viva"] and pid not in asignados:
            s.update({"viva": True, "tty": pr["tty"], "pid": pid})
            _mapeo_pid[s["id"]] = pid
            asignados.add(pid)
    # 3) procesos nuevos sin session-id: a la sesión libre de la misma carpeta cuyo
    #    inicio más se acerque al arranque del proceso
    for pid, pr in vivos.items():
        if pid in asignados or pr.get("sid"):
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
            # Sin pty a mano: el transcript manda y el mtime dirime lo demás.
            fresca = (ahora - s["mtime"]) < 60
            s["estado"] = ("esperando" if s["fin"] == "termino"
                           else "trabajando" if fresca else "esperando")
            s["te_espera"] = (s["estado"] == "esperando"
                              and s["fin"] in ("termino", "herramienta"))
        else:
            s["tipo"] = "terminal"
            s["estado"] = "terminada"
            s["te_espera"] = False
        if s["auto"] or _proyecto_lindo(s["cwd"]) == "Temporal":
            s["tipo"] = "automatica"
        s["proyecto"] = _proyecto_lindo(s["cwd"])
        s["hace_seg"] = int(ahora - s["mtime"])
        s.pop("mtime", None)

    for s in afuera:
        _aplicar_meta(s, s["id"], meta)

    orden = {"trabajando": 0, "esperando": 1, "terminada": 2}
    afuera.sort(key=lambda s: (orden[s["estado"]], s["hace_seg"]))

    return {"tabs": tabs_json, "afuera": afuera, "proyectos": proyectos(),
            "casa": PROY_CASA,
            # Las áreas viajan con el estado y no clavadas en el HTML: así
            # tocar un color o un rol en areas.py se ve sin reiniciar el server.
            "areas": areas.para_el_front(),
            # Cuál es el área por defecto también viaja: escrita a mano en el JS,
            # el día que se renombre la clave el front pediría una que no existe y
            # las sesiones sin área se quedarían sin cara, sin color y fuera de todo
            # filtro — sin un solo error. Es el error nº 2 de la lista de la casa.
            "area_defecto": areas.DEFECTO}


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

    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0:
            # negativo = rfile.read() lee hasta EOF: el hilo queda colgado hasta que el
            # cliente corte, y /pin y /api/ticket se leen ANTES del PIN
            n = 0
            self.close_connection = True
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

    def _quien(self):
        """El nombre de la persona de Administración, o "duenio". Sale del token, no de la URL:
        el cliente no puede decir quién es."""
        f = _sesion_valida(self._cookie("cacho_sesion")) or {}
        return f.get("quien") or "duenio"

    # LA PARED DE DATOS ES DE CÓDIGO (policía 11-set-2026). Una pestaña enjaulada de
    # Administración tipea una ruta en su charla y la bandeja la lista y la sirve: el que lee
    # es ESTE server (el usuario de la máquina, acceso total al disco), no el claude enjaulado, así
    # que el DENY de la jaula no la frena. Con estas dos puertas, para quien no es el usuario una
    # sesión es «suya» sólo si nació en la jaula, y un archivo sólo si vive adentro de ella.
    def _sesion_suya(self, transcript):
        if self._quien() == "duenio":
            return True
        if cacho_perfiles is None:
            return False
        try:
            cwd = _leer_sesion(transcript).get("cwd") or "/"
        except Exception as e:                       # noqa: BLE001 — pared: ante la duda, NO
            print(f"⚠️ pared de la jaula: no pude leer {os.path.basename(transcript)} ({e!r}); "
                  "se niega el acceso", file=sys.stderr)
            return False
        return os.path.realpath(cwd) == os.path.realpath(cacho_perfiles.TRABAJO)

    def _archivo_de_la_jaula(self, ruta):
        if cacho_perfiles is None:
            return False
        real = os.path.realpath(ruta)
        for base in (cacho_perfiles.TRABAJO, os.path.expanduser("~/Library/Caches/Cacho/subidas")):
            base = os.path.realpath(base)
            if real == base or real.startswith(base + os.sep):
                return True
        return False

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
        perfil, quien = _quien_es(q["pin"][0])
        if perfil:
            _acierto_pin(self._ip)
            self._login = (perfil, quien)
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
        firma = ('<div class="firma">%s</div>' % _marca.firma_html("negro", alto=30, gap=12)
                 if _marca else "")
        cuerpo = (PAGINA_PIN.replace("{{AVISO}}", aviso)
                            .replace("{{MARCA}}", firma).encode())
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
                perfil, quien = getattr(self, "_login", ("duenio", ""))
                return self._redirigir_sin_pin(_sesion_nueva(perfil, quien))
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
                self._json(estado_general(self._quien()))
            except Exception as e:
                self._json({"error": repr(e)}, 500)
        elif ruta == "/api/uso":
            # % consumido del plan Max (dato oficial, cacheado 5 min en uso_claude).
            # Sin dato se dice "sin dato": nunca se estima ni se inventa (regla de la casa).
            if uso_claude is None:
                self._json({"ok": False, "error": "uso_claude.py no está en esta instalación"})
            else:
                try:
                    self._json(uso_claude.uso())
                except Exception as e:
                    self._json({"ok": False, "error": repr(e)})
        elif ruta.startswith("/api/sesion/") and ruta.endswith("/ver"):
            sid = ruta.split("/")[3]
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                return self._json({"error": "id inválido"}, 400)
            p = _buscar_transcript(sid)
            if not p or not self._sesion_suya(p):
                return self._json({"error": "no encontré esa sesión"}, 404)
            try:
                self._json(_leer_conversacion(p))
            except Exception as e:
                self._json({"error": repr(e)}, 500)
        elif ruta.startswith("/api/sesion/") and ruta.endswith("/archivos"):
            # la bandeja: lo que pasó por la charla (ver cacho_bandeja.py)
            sid = ruta.split("/")[3]
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                return self._json({"error": "id inválido"}, 400)
            if cacho_bandeja is None:
                return self._json({"items": [], "sin_modulo": True})
            p = _buscar_transcript(sid)
            if not p or not self._sesion_suya(p):
                return self._json({"error": "no encontré esa sesión"}, 404)
            try:
                items = cacho_bandeja.archivos(p)
                if self._quien() != "duenio":
                    items = [it for it in items if self._archivo_de_la_jaula(it["ruta"])]
                self._json({"items": items})
            except Exception as e:
                print(f"⚠️ bandeja de {sid[:8]}: {e!r}", file=sys.stderr)
                self._json({"error": repr(e)}, 500)
        elif ruta == "/api/archivo":
            q = parse_qs(urlparse(self.path).query)
            self._archivo(q.get("sid", [""])[0], q.get("ruta", [""])[0],
                          q.get("mini", [""])[0] == "1")
        elif ruta.startswith("/api/term/") and ruta.endswith("/stream"):
            self._stream(ruta.split("/")[3])
        else:
            self._json({"error": "no existe"}, 404)

    def _archivo(self, sid, ruta, mini):
        """Sirve UN archivo de la bandeja de la sesión `sid` (o su miniatura).

        La única puerta es `cacho_bandeja.permitida()`: la ruta tiene que estar en la
        lista de ESA charla. Sin eso, esto sería «leer cualquier archivo del disco con
        PIN», y el PIN protege una app, no la máquina entera. Los .html salen en
        `sandbox` por lo mismo que los de /static: un archivo que se mira no puede
        correr con el origen de Cacho. Soporta `Range` porque sin eso Safari (iPhone)
        no reproduce un video ni abre un PDF grande."""
        if cacho_bandeja is None:
            return self._json({"error": "sin bandeja en esta instalación"}, 404)
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid or ""):
            return self._json({"error": "id inválido"}, 400)
        p = _buscar_transcript(sid)
        real = cacho_bandeja.permitida(p, ruta) if p and self._sesion_suya(p) else None
        if not real or (self._quien() != "duenio" and not self._archivo_de_la_jaula(real)):
            return self._json({"error": "ese archivo no está en la bandeja de esta sesión"}, 404)
        if mini:
            m = cacho_bandeja.miniatura(real)
            if not m:
                return self._json({"error": "sin miniatura"}, 404)
            real, ext = m, ".jpg"
        else:
            ext = os.path.splitext(real)[1].lower()
        try:
            st = os.stat(real)
        except OSError:
            return self._json({"error": "ya no está"}, 404)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        tipo = cacho_bandeja.MIME.get(ext, "application/octet-stream")
        if tipo.startswith("text/"):
            tipo += "; charset=utf-8"
        inicio, fin = 0, st.st_size - 1
        rango = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rango.strip()) if rango else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                inicio = int(m.group(1))
                if m.group(2):
                    fin = min(int(m.group(2)), fin)
            else:   # los últimos N bytes
                inicio = max(0, st.st_size - int(m.group(2)))
            if inicio > fin or inicio >= st.st_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{st.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {inicio}-{fin}/{st.st_size}")
        else:
            self.send_response(200)
        largo = fin - inicio + 1
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(largo))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        nombre = os.path.basename(real).encode("utf-8", "replace")
        self.send_header("Content-Disposition",
                         "inline; filename*=UTF-8''" + quote(nombre))
        if ext in (".html", ".htm", ".svg"):
            self.send_header("Content-Security-Policy",
                             "sandbox; default-src 'none'; img-src data: 'self'; "
                             "style-src 'unsafe-inline'; font-src data:")
        self.end_headers()
        try:
            with open(real, "rb") as fh:
                fh.seek(inicio)
                while largo > 0:
                    trozo = fh.read(min(256 * 1024, largo))
                    if not trozo:
                        break
                    self.wfile.write(trozo)
                    largo -= len(trozo)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream(self, tid):
        with TABS_LOCK:
            t = TABS.get(tid)
        if not t or t.duenio != self._quien():
            # Mismo criterio que el POST: la pestaña de otro no existe. Leer el stream es ver
            # la terminal ajena en vivo, así que este chequeo pesa igual que aquel.
            return self._json({"error": "no existe"}, 404)
        # "desde": cuántos bytes de esta pestaña ya tiene el navegador. Si todavía
        # los tenemos en el buffer, se le manda SOLO lo que se perdió y no borra lo
        # que ya pintó — volver a una pestaña pasa a costar casi nada.
        try:
            desde = int(parse_qs(urlparse(self.path).query).get("desde", ["-1"])[0])
        except ValueError:
            desde = -1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = Queue()
        with t.lock:
            snapshot, limpiar, pos = arranque_stream(t.buf, t.escritos, desde)
            t.subs.append(q)
        try:
            # el navegador necesita saber si tiene que borrar lo que ya pintó y en
            # qué byte arranca lo que viene, para poder pedir "desde acá" la próxima
            self.wfile.write(b"event: base\ndata: "
                             + json.dumps({"pos": pos, "limpiar": limpiar,
                                           "bytes": len(snapshot)}).encode()
                             + b"\n\n")
            self.wfile.flush()
            # en pedazos: si va todo junto, el navegador arma un texto gigante, lo
            # decodifica y lo pinta de una sola vez — y mientras tanto la ventana
            # entera queda congelada. En pedazos empieza a verse enseguida.
            for i in range(0, len(snapshot), SSE_TROZO):
                self._sse(snapshot[i:i + SSE_TROZO])
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
        siguiente se lee corrompida: `501 Unsupported method ('pin=NNNNGET')`, y desde ahí
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
            perfil, quien = _quien_es(datos.get("pin", [""])[0])
            if perfil:
                _acierto_pin(self._ip)
                if quien:
                    _log_seguridad("entró %s (perfil %s)" % (quien, perfil))
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", self._COOKIE_SESION.format(
                    tok=_sesion_nueva(perfil, quien)))
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
            if "area" in d:
                # Corregir a mano a qué área es una sesión. Vacío = devolvérsela al
                # clasificador, que es lo que hace que esto no se vuelva un archivo manual:
                # la máquina propone, el usuario corrige, y siempre se puede volver atrás.
                a = areas.valida(d["area"]) if d["area"] else None
                if d["area"] and not a:
                    return self._json({"error": "área que no existe"}, 400)
                campos["area"] = a
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
            elif self._quien() == "duenio":
                permitidos = {p["cwd"] for p in proyectos()}
                if cwd not in permitidos:
                    return self._json({"error": "proyecto inválido"}, 400)
            # Tope de pestañas: cada una es un zsh + un `claude` + un pty. Sin
            # techo, un cliente en bucle (un script con un error, no hace falta
            # mala intención) deja la máquina sin procesos ni memoria.
            #
            # Antes de medir el tope, COSECHAR (29-ago-2026). La cosecha de muertas
            # corría solo desde estado_general(), o sea cuando alguien MIRA la barra.
            # De madrugada nadie mira Cacho: las pestañas de las rutinas nocturnas
            # morían sin cosechar, el tope se medía contra una lista llena de
            # cadáveres, y a las 6:05 les rebotaba un 429 a enriquecer-supervisores
            # y monitor-base-local con la máquina casi vacía. Si tras la cosecha
            # normal sigue lleno, segunda pasada sin gracia (los 15 min de gracia
            # son cosmética de la barra; un 429 a una rutina de producción pesa
            # más). Recién si hay MAX_TABS realmente VIVAS, el 429 es legítimo.
            _cosechar_tabs(time.time())
            with TABS_LOCK:
                lleno = len(TABS) >= MAX_TABS
            if lleno:
                _cosechar_tabs(time.time(), gracia=0)
            with TABS_LOCK:
                if len(TABS) >= MAX_TABS:
                    return self._json(
                        {"error": f"ya hay {MAX_TABS} pestañas abiertas; "
                                  "cerrá alguna con la ✕"}, 429)
            # modelo explícito para esta pestaña (lo usa citas_cacho para las premium).
            # Se valida acá con la misma vara que ~/.cacho_modelo: termina en un comando.
            modelo = q.get("modelo", [""])[0]
            if modelo and not _MODELO_OK.match(modelo):
                return self._json({"error": "modelo inválido"}, 400)
            # ── El ÁREA se DECLARA al nacer (29-ago-2026) ────────────────────────────
            # Antes TODA sesión nacía pelada y `areas.de_texto()` le adivinaba el área
            # contando palabras del transcript, cada 4 s. El problema no es que a veces
            # se equivoque: es que cuando se equivoca NO LO DICE — lo que no reconoce
            # cae en Cacho por descarte, así que Cacho parecía lleno de cosas que no
            # eran suyas y la tira mentía sin un solo error a la vista.
            # RAÍZ: se adivinaba un dato que quien abre la pestaña YA SABE. el usuario parado
            # en una cara lo sabe; una rutina que corre para una sola área también. Es
            # la misma regla del feeder: a la máquina no se le pide lo que ya sabe.
            # Ojo con el alcance: esto sólo tiene dónde anotarse si la pestaña nace con
            # `--session-id` (la meta va por sid). En una máquina con un `claude` viejo
            # la pestaña sale igual y el clasificador sigue trabajando como siempre —
            # se pierde la precisión, no la sesión.
            area = q.get("area", [""])[0]
            if area and not areas.valida(area):
                return self._json({"error": "área inválida"}, 400)
            # La jaula la pone el SERVER según quién entró, no el cliente. Que el cwd y el
            # área vengan por la URL está bien para el usuario; para Administración serían un
            # pedido de la parte que justamente estamos encerrando.
            quien = self._quien()
            jaula = cacho_perfiles.jaula(quien) if (quien != "duenio" and cacho_perfiles) else None
            if jaula:
                cwd, area = jaula["cwd"], jaula["area"]
            t = TermSession(cwd, resume_id=resume, modelo=modelo, area=area, jaula=jaula)
            if area and t.transcript_id:
                _meta_set(t.transcript_id, {"area": area})
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
            # Una pestaña ajena no existe para vos: 404, no 403. Un 403 confirmaría que el id
            # es bueno, y el id es lo único que hace falta para escribir en la terminal de otro.
            if t.duenio != self._quien():
                return self._json({"error": "no existe"}, 404)
            if accion == "input":
                try:
                    d = base64.b64decode(json.loads(self._body())["d"])
                except (ValueError, KeyError, TypeError):
                    # cuerpo que no es {"d": base64}: antes era traceback en stderr y
                    # conexión cortada sin respuesta
                    return self._json({"error": "cuerpo inválido"}, 400)
                try:
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

        if ruta == "/api/frente":
            # Traer al frente la ventana de Cacho. Lo pide el front DESPUÉS de soltar un
            # archivo y SOLO si la ventana no tiene el foco del sistema
            # (`document.hasFocus()` en falso). RAÍZ del problema: arrastrás desde el
            # Finder —que es la app del frente— y macOS NO le da el foco de teclado a la
            # ventana que recibe el drop. Ningún focus() de JavaScript puede arreglar eso:
            # el foco de VENTANA lo maneja el sistema, no la página. Por eso escribías y
            # las teclas se las llevaba el Finder, y había que dar un clic.
            if self._ip not in ("127.0.0.1", "::1"):
                # desde el teléfono/Tailscale no hay ventana que levantar
                return self._json({"ok": False, "msg": "no es local"})
            guion = ("""
tell application "Google Chrome"
  set encontradas to {}
  repeat with w in windows
    try
      if URL of active tab of w contains "127.0.0.1:%d" then set end of encontradas to (id of w)
    end try
  end repeat
  if (count of encontradas) is 0 then return "no"
  set p to window id (item 1 of encontradas)
  try
    set minimized of p to false
  end try
  activate
  set index of p to 1
  return "si"
end tell""" % PORT)
            try:
                # Nunca cierra ni reordena nada más: sube la ventana y listo (el lanzador
                # de Cacho.app sí cierra duplicados; acá no, esto corre con el usuario mirando).
                r = subprocess.run(["osascript", "-e", guion], capture_output=True,
                                   text=True, timeout=6)
                ok = r.stdout.strip() == "si"
                if not ok:
                    print(f"⚠️ /api/frente no pudo levantar la ventana: "
                          f"{r.stdout.strip()!r} {r.stderr.strip()!r}", file=sys.stderr)
                return self._json({"ok": ok, "msg": r.stdout.strip() or r.stderr.strip()})
            except Exception as e:
                print(f"⚠️ /api/frente falló: {e!r}", file=sys.stderr)
                return self._json({"ok": False, "msg": str(e)})

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
  /* misma paleta clara que la app (23-ago-2026): crema + coral */
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#FAF9F5;color:#1F1E1B;font-family:-apple-system,system-ui,sans-serif}
  .caja{text-align:center;padding:32px}
  /* Hija DIRECTA: es la cara de Cacho. Un `.caja img` a secas le cae también al logo de
     la firma y se lo redondea como si fuera un avatar. */
  .caja>img{width:96px;height:96px;border-radius:22px}
  .firma{margin:0 0 20px;padding-bottom:16px;border-bottom:1px solid #E3E0D5}
  h1{font-size:22px;margin:14px 0 2px}
  p{color:#6E6C64;margin:4px 0 18px;font-size:14px}
  input{font-size:26px;letter-spacing:8px;text-align:center;width:190px;padding:10px;
        border-radius:12px;border:1px solid #E3E0D5;background:#FFFFFF;color:#1F1E1B}
  button{display:block;margin:16px auto 0;font-size:16px;padding:10px 34px;border:0;
         border-radius:12px;background:#C96442;color:#fff;font-weight:600}
  .err{color:#B4331B;margin-top:12px;font-size:14px}
</style>
</head>
<body>
  <form class="caja" method="post" action="/pin">
    {{MARCA}}
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
/* PALETA — la de claude.ai: crema de fondo y coral de acento (pedido del usuario
   23-ago-2026: "no veo nada así"). Cacho es CLARO SIEMPRE, no sigue el modo
   del sistema. La raíz del problema era esa: había una paleta clara acá y una
   oscura colgada de `prefers-color-scheme`, y con la Mac en modo oscuro el usuario
   nunca veía la clara — encima la barra y la ⌘K se forzaban oscuras a mano, así
   que ni cambiando el modo del sistema se aclaraban. Un solo juego de colores,
   sin bifurcación: lo que se ve acá es lo que hay.
   ÚNICA excepción: --term-bg. La terminal va oscura a propósito (decisión del usuario
   23-ago-2026) porque el texto de Claude Code viene con colores ANSI pensados
   para fondo oscuro; aclararla obliga a cambiarle el tema al CLI también. */
:root{
  --bg:#FAF9F5; --panel:#F0EEE6; --card:#FFFFFF; --borde:#E3E0D5;
  --tinta:#1F1E1B; --gris:#6E6C64; --acento:#C96442; --ocre:#B8860B;
  /* coral en rgb, para los tintes y los brillos que necesitan alfa */
  --acento-rgb:201,100,66;
  /* AZUL de "estás acá" (26-ago-2026, pedido del usuario). Antes la sesión abierta se
     marcaba con el coral, que en esta barra ya dice otras tres cosas (el logo, el
     botón de nueva, el punto de "trabajando"): el mismo color diciendo cuatro cosas
     no señala ninguna. El azul no lo usa nada más, así que dónde estás parado se
     encuentra de un vistazo sin leer. */
  --aca:#2563EB; --aca-rgb:37,99,235;
  --term-bg:#1E1D1B;
  /* letra de display: el nombre "Cacho" y los títulos de sesión. Futura,
     elegida el 16-ago-2026 sobre otras 11 candidatas (antes era Georgia).
     Un solo lugar para cambiarla. */
  --display: Futura, "Century Gothic", system-ui, sans-serif;
}
*{box-sizing:border-box; margin:0; padding:0}
html,body{height:100%}
body{
  display:flex; background:var(--bg); color:var(--tinta); overflow:hidden;
  font:15px/1.4 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
}
/* ---------- LA TIRA DE ÁREAS (23-ago-2026) ----------
   Cinco caras, siempre a la vista, a la izquierda de todo. Es la que hace posible el
   «ah, esto es de publicidad — pin, y hablo con Jaime».

   POR QUÉ LA CARA Y NO UN COLOR SOLO: una cara se reconoce sin leer y sin tener que
   acordarse de qué significaba el verde. El color la refuerza; nunca va solo. Eso es
   además lo que hace que esto siga andando con sol de frente o para alguien que no
   distingue rojo de verde.

   POR QUÉ SÓLO EL BORDE (pedido del usuario): pintar el fondo de cada renglón de color
   convierte la barra en un semáforo y tapa lo único que importa, que es el título. */
#tira{
  width:56px; flex:none; background:var(--panel); border-right:1px solid var(--borde);
  display:flex; flex-direction:column; align-items:center;
  padding:12px 0 8px;
}
/* Las caras arriba, y el pie de la tira (Cacho trabajando) abajo del todo. Van en dos
   cajas porque `pintarTira()` reescribe el innerHTML de las caras cada vez que cambia
   la cuenta: si el dibujo fuera hermano de los botones, se borraría en cada repintado. */
#tira-caras{
  flex:1; min-height:0; width:100%;
  display:flex; flex-direction:column; align-items:center; gap:9px;
  overflow-y:auto; overscroll-behavior:contain;
}
#tira .ar{
  border:0; background:none; padding:2px; border-radius:50%; cursor:pointer;
  line-height:0; position:relative; opacity:.62; transition:opacity .12s;
}
#tira .ar:hover{opacity:.9}
#tira .ar.on{opacity:1}
#tira .ar img{width:36px; height:36px; border-radius:50%; display:block}
/* El aro de color sólo en la elegida: cinco aros prendidos a la vez es ruido y deja de
   señalar cuál está activa, que es todo lo que tiene que decir. */
#tira .ar.on{box-shadow:0 0 0 2.5px var(--ar-col)}
/* Cuántas sesiones hay en esa área. Es lo que evita el cajón: se ve que Xara tiene 3
   cosas esperando aunque estés metido en Jaime. */
#tira .ar b{
  position:absolute; right:-2px; bottom:-1px; min-width:15px; height:15px;
  border-radius:8px; background:var(--ar-col); color:#fff; font-size:9px;
  font-weight:700; line-height:15px; text-align:center; padding:0 3px;
  border:1.5px solid var(--panel);
}
#tira .ar.vacia b{display:none}
@media (max-width:700px){ #tira{display:none} }

/* El marco del área alrededor de la pantalla donde TIPEÁS, no sólo en la lista. Es la
   lección vieja de sistemas («producción es rojo»): el color sirve para no hacer en un
   contexto lo que ibas a hacer en otro, y para eso tiene que estar donde mirás. el usuario pidió
   sólo el borde, así que es un marco de 2 px y no un fondo. */
#terms.con-area{box-shadow:inset 0 0 0 2px var(--ar-col); border-radius:12px}

/* ---------- barra lateral ---------- */
#side{
  /* la barra iba SIEMPRE oscura (pedido del usuario 14-ago-2026) redefiniéndose la
     paleta acá adentro. Desde el 23-ago-2026 la app es clara entera, así que
     usa los colores de :root y no se pisa nada: un solo juego de colores en
     todo Cacho. (De aquella época queda la lección: si algún día se vuelve a
     redefinir --tinta en un bloque, hay que declarar TAMBIÉN `color`, porque
     el color se hereda ya resuelto desde <body> y los títulos se vuelven
     invisibles.) */
  width:300px; flex:none; background:var(--panel); border-right:1px solid var(--borde);
  display:flex; flex-direction:column; padding:14px 10px 10px;
  color:var(--tinta);
}
#side h1{
  font-family:var(--display); font-weight:500; font-size:19px;
  display:flex; align-items:center; gap:8px; padding:2px 8px 12px;
}
#side h1 .logo{color:var(--acento); font-size:17px}
/* Con un área elegida, el nombre de arriba es el botón para salir. Sin esto, en el celular
   —donde la tira no entra— quedabas filtrado y sin forma de volver a ver todo. */
#tit-area.en-area{cursor:pointer; color:var(--ar-col, var(--acento))}
#tit-area.en-area::after{content:" ✕"; font-size:12px; opacity:.55}
.logo-foto{width:32px; height:32px; border-radius:50%; flex:none}
/* El «＋ Nueva sesión» salió el 27-ago-2026 (pedido del usuario): ocupaba el lugar más caro de
   la barra —arriba de todo— y él nunca lo usaba, porque el trabajo es siempre sobre
   Gestión. Crear sesión en cualquier otro proyecto sigue estando en la paleta (⌘K en la
   compu, 🔍 en el teléfono): «nueva sesión en <proyecto>». */
/* (29-ago-2026) El botón se mudó ADENTRO del <h1>, al lado del nombre. Dos motivos, y
   el segundo es el que manda: ocupaba una FILA ENTERA de una barra donde lo caro es el
   alto —cada fila que se va es una sesión más a la vista—, y sobre todo NO DECÍA LO QUE
   HACE. Decía "📈 Gestión", que es el proyecto, y el proyecto es siempre el mismo. Ahora
   abre una sesión CON QUIEN ESTÁS PARADO, así que su significado se lo da la cara de al
   lado y no le hace falta texto propio: es un ＋ y nada más. Se tiñe del color del área
   para que la cara, el nombre y el ＋ se lean como una sola cosa.
   (La caja no cambia de tamaño con el nombre: `flex:none` y ancho fijo, así "Waldemar"
   —el más largo— no le come el botón en los 300 px de la barra.) */
#btn-nueva{
  flex:none; width:26px; height:26px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:var(--card); color:var(--ar-col, var(--acento));
  border:1px solid var(--borde); font-size:14px; line-height:1; cursor:pointer;
  font-family:inherit; padding:0;
}
#btn-nueva:hover{
  background:var(--ar-col, var(--acento)); color:#fff; border-color:transparent;
}
/* El 🔔 de al lado del ＋ (10-set-2026, pedido del usuario). Con 17 charlas abiertas, «¿qué
   tengo para contestar?» se leía punto por punto: el ocre de cada ítem dice "quieta" pero
   hay que recorrer la barra entera, y el (11) del título de la pestaña dice CUÁNTAS pero
   no CUÁLES. Este botón filtra la lista a las que ya terminaron su trabajo y están
   quietas esperando respuesta — ni las que están trabajando, ni las terminadas de hoy.
   Habla el mismo idioma que la campanita del ítem (terminó y te espera) y adentro del
   filtro las avisadas —las que terminaron desde la última vez que las miraste— van
   arriba. El número vive en el botón porque es la misma pregunta: cuántas y cuáles.
   Lleva el margin-left:auto que antes tenía el ＋: es el primero de los dos. */
#btn-espera{
  margin-left:auto; flex:none; position:relative;
  width:26px; height:26px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  background:var(--card); color:var(--ar-col, var(--acento));
  border:1px solid var(--borde); font-size:12px; line-height:1; cursor:pointer;
  font-family:inherit; padding:0;
}
#btn-espera:hover{border-color:var(--ar-col, var(--acento))}
#btn-espera.on{
  background:var(--ar-col, var(--acento)); border-color:transparent;
}
/* Prendido, el badge ocre sobre el terracota del área no se leía: se da vuelta. */
#btn-espera.on b{background:#fff; color:var(--ar-col, var(--acento))}
#btn-espera.vacio{opacity:.4}
#btn-espera b{
  position:absolute; top:-4px; right:-4px; min-width:14px; height:14px;
  border-radius:7px; padding:0 3px; box-sizing:border-box;
  background:var(--ocre); color:#fff; font-size:9.5px; font-weight:700;
  line-height:14px; text-align:center; font-family:var(--display, inherit);
}
#btn-espera.vacio b{display:none}
/* Buscador de la barra (21-ago-2026). El ⌘K sigue estando —abre cualquier cosa
   desde el teclado, incluso una sesión nueva—, pero se acuerda quien lo sabe.
   Este está a la vista y hace otra cosa: filtra la lista SIN taparla, así que
   las secciones y el estado de cada charla se siguen viendo mientras se busca.
   Vive FUERA de #listas a propósito: la lista se repinta cada 4 s y si el input
   estuviera adentro se perdería el foco y lo tipeado a mitad de palabra. */
#filtro-caja{
  display:flex; align-items:center; gap:6px; margin-top:12px;
  background:var(--card); border:1px solid var(--borde); border-radius:10px;
  padding:6px 9px;
}
#filtro-caja .lupa{font-size:11px; opacity:.6; flex:none}
#filtro{
  flex:1; min-width:0; background:none; border:0; outline:none;
  color:var(--tinta); font-size:12.5px; font-family:inherit;
}
#filtro::placeholder{color:var(--gris)}
#filtro-caja:focus-within{border-color:var(--acento)}
#filtro-x{
  border:0; background:none; color:var(--gris); cursor:pointer; font-size:11px;
  padding:0 2px; display:none; flex:none;
}
body.buscando #filtro-x{display:block}
#filtro-x:hover{color:var(--tinta)}
#listas{flex:1; overflow-y:auto; margin-top:14px}
/* ── Lo terminado va PLEGADO (26-ago-2026, pedido del usuario) ──────────────────
   Listadas igual que las vivas, las de hoy que ya terminaron hacían que la barra
   pareciera una lista de pendientes que nunca baja: "miro todo lo que tengo
   pendiente y parece que no termino nunca". Se pliegan, no se esconden — están a
   un click "por las dudas y preciso revisar algo". Se abren solas cuando estás
   buscando (a una vieja se llega buscándola) o cuando la sesión abierta es una de
   ellas: plegarla sería esconder justo dónde estás parado. */
details.plegable > summary{
  list-style:none; cursor:pointer; display:flex; align-items:center; gap:6px;
  user-select:none;
}
details.plegable > summary::-webkit-details-marker{display:none}
details.plegable > summary:hover{color:var(--tinta)}
details.plegable .flecha{font-size:8px; width:8px; flex:none}
details.plegable .cuantas{
  margin-left:auto; background:var(--borde); color:var(--gris); border-radius:8px;
  padding:0 6px; font-size:9.5px; line-height:15px; font-weight:700;
}
details.plegable .mas{color:var(--gris); font-size:10.5px; padding:4px 8px 8px}
/* (27-ago-2026) el perro se mudó al pie de la TIRA, así que ya no le come lugar a la
   lista y no hay razón para esconderlo mientras se busca. */
.nada-con-eso{color:var(--gris); font-size:11.5px; padding:14px 8px; line-height:1.5}
.seccion{
  color:var(--gris); font-size:10px; font-weight:700; letter-spacing:.08em;
  text-transform:uppercase; padding:10px 8px 6px;
}
.item{
  border-radius:10px; padding:8px 10px; cursor:pointer; margin-bottom:2px;
  position:relative;
}
.item:hover{background:var(--card)}
/* La barrita de color del área, a la izquierda del renglón. 3 px: se ve de reojo y no
   le roba lugar al título. */
.item{border-left:3px solid transparent}
/* La fijada conserva el color de SU área en la barrita (antes se le ponía ocre, que además
   es el color de Xara: el mismo color diciendo dos cosas). Que está fijada ya lo dicen el
   fondo, la negrita y el 📌 — no hace falta gastarle el único canal que tiene el área. */
.item .cara-ar{width:15px; height:15px; border-radius:50%; flex:none; opacity:.9}
/* Con un área elegida, la cara de cada renglón sobra: son todas la misma. */
body.area-fija .item .cara-ar{display:none}
.item .fila{display:flex; align-items:center; gap:8px; margin-top:4px}
.dot{width:8px; height:8px; border-radius:50%; flex:none}
.dot.trabajando{background:var(--acento); animation:lat 1.4s ease-in-out infinite}
.dot.esperando{background:var(--ocre)}
.dot.terminada{background:var(--gris); opacity:.5}
@keyframes lat{0%,100%{box-shadow:0 0 0 0 rgba(var(--acento-rgb),.5)}50%{box-shadow:0 0 0 5px rgba(var(--acento-rgb),0)}}
/* El título va solo en su renglón y ENTERO (25-ago-2026, pedido del usuario): antes
   compartía la fila con hora+iconitos y quedaba «Brief Reu…». Si es largo, envuelve. */
.item .tit{
  font-size:12.5px; font-weight:500; line-height:1.3;
  white-space:normal; overflow-wrap:anywhere;
}
/* De qué va la charla: el ícono del tema al lado del título, "→ tema" cuando
   se fue para otro lado, y abajo qué se está haciendo ahora. */
.item .tema-ico{font-size:11.5px; opacity:.9}
/* Peso de la charla (24-ago-2026). Se midió que el 99% del gasto son las charlas largas:
   en cada turno se relee todo lo anterior, así que el costo crece al cuadrado. Esto lo
   hace VISIBLE donde el usuario ya mira, en vez de pedirle que se acuerde de cortar. */
.item .peso{font-size:10px; cursor:help; margin-left:2px}
.item .peso-aviso{margin:4px 0 1px 0; padding:5px 7px; border-radius:6px; font-size:10.5px;
  line-height:1.35; background:rgba(220,80,60,.12); color:#e9a08f;
  display:flex; gap:6px; align-items:center; justify-content:space-between}
.item .peso-aviso.amarillo{background:rgba(220,170,60,.10); color:#d8bd83}
.item .peso-aviso button{flex:0 0 auto; font-size:10px; padding:3px 8px; border-radius:5px;
  border:1px solid currentColor; background:transparent; color:inherit; cursor:pointer}
.item .peso-aviso button:hover{background:rgba(255,255,255,.08)}
.item .muto{color:var(--ocre); font-weight:600; font-size:11px}
/* Las líneas chicas de contexto (.ctx) y pedido (.snippet) se sacaron el 25-ago-2026:
   el usuario no las llegaba a leer y no sumaban. Esa info vive en el tooltip del renglón. */
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
.item.fijada{background:var(--card)}
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
/* DÓNDE ESTÁS PARADO (21-ago-2026). Iba una línea fina del color del acento y
   el mismo fondo que el hover y que las fijadas: con la lista llena no se sabía
   cuál estaba abierta, y eso importa justo cuando se va a cerrar una. Va último
   a propósito: gana sobre fijada y avisada, que también pintan el fondo. La
   barra de la izquierda late — despacio y sin mover nada de lugar, para que se
   encuentre de un vistazo sin que moleste mientras se lee. */
.item.activo, .item.activo:hover{
  background:rgba(var(--aca-rgb),.13);
  box-shadow:inset 0 0 0 1px rgba(var(--aca-rgb),.38);
}
.item.activo::before{
  content:""; position:absolute; left:0; top:5px; bottom:5px; width:3px;
  border-radius:0 3px 3px 0; background:var(--aca);
  animation:aca 2.2s ease-in-out infinite;
}
@keyframes aca{
  0%,100%{opacity:1; box-shadow:0 0 7px rgba(var(--aca-rgb),.55)}
  50%    {opacity:.35; box-shadow:0 0 0 rgba(var(--aca-rgb),0)}
}
/* Y con todas las letras, que es lo que se pidió: un cartelito que dice dónde estás
   parado. El color solo lo sabe quien ya aprendió qué significa el color. */
.item.activo .aca-estas{
  background:var(--aca); color:#fff; border-radius:6px; padding:1px 5px; flex:none;
  font-size:8.5px; font-weight:700; letter-spacing:.03em; text-transform:uppercase;
}
/* Hace cuánto. `nowrap` a propósito: con el cartel de "acá estás" en la misma fila,
   "2 min" se partía en dos renglones y el renglón activo crecía de alto. */
.item .fila .hs{color:var(--gris); font-size:10px; white-space:nowrap; flex:none}
/* OJO: acá iba `color:#fff` (blanco), que servía cuando la barra era oscura y
   sobre la crema desaparece. Sobre fondo claro el "estás acá" lo dan el tinte
   coral y la negrita, no un color de letra propio (23-ago-2026). */
.item.activo .tit{font-weight:700; color:var(--aca)}
@media (prefers-reduced-motion:reduce){ .item.activo::before{animation:none} }
/* ---------- Cacho ----------
   Mientras alguna sesión de la app está trabajando, Cacho se sienta al PIE DE LA TIRA,
   abajo del todo de la columna de las caras (27-ago-2026, pedido del usuario: donde estaba
   —abajo de la lista de sesiones— se comía el alto que hace falta para ver sesiones).
   Es el dibujo de él, y está SENTADO: respira, nada más. Nada de recortarle la cabeza o
   las patas para animarlas por separado — la imagen se escala entera y no se deforma
   nunca. Cuando no hay trabajo, desaparece y la tira no cambia de alto. */
#corriendo{
  display:none; position:relative; flex:none; height:44px; width:100%;
  margin-top:6px; padding-top:6px; border-top:1px solid var(--borde);
  align-items:flex-end; justify-content:center;
  contain:layout style;   /* el respirar no toca el layout de la tira */
}
#corriendo.ver{display:flex}
#corriendo img{
  height:38px; display:block; transform-origin:bottom center;
  animation:respira 3.4s ease-in-out infinite;
}
@keyframes respira{
  0%,100%{transform:scale(1)}
  50%    {transform:scale(1.035)}
}
#pie{color:var(--gris); font-size:10.5px; padding:10px 8px 2px}
/* ── Uso del plan (25-ago-2026) ──────────────────────────────────────────────
   El % consumido del límite semanal, en el lenguaje de la casa (tubo + marca):
   el relleno es lo USADO y la rayita es dónde DEBERÍAS ir a esta altura de la
   semana. Relleno más allá de la rayita = gastando de más. El color lo dice
   solo: verde a ritmo, ocre pasado, rojo camino a quedarte sin cupo antes del
   lunes. El detalle (ritmo, cuándo se acaba, cuándo resetea) vive en el title. */
#uso{padding:8px 8px 0; display:flex; flex-direction:column; gap:5px}
#uso .u-lin{display:flex; align-items:center; gap:7px; font-size:10px;
  color:var(--gris); cursor:help}
#uso .u-eti{flex:none; width:46px; text-transform:uppercase; letter-spacing:.05em;
  font-weight:700; font-size:9px}
#uso .u-tubo{display:block; flex:1; height:7px; border-radius:4px; background:var(--card);
  border:1px solid var(--borde); position:relative}
#uso .u-fill{display:block; height:100%; border-radius:4px; background:#5a8a5e; max-width:100%}
#uso .u-fill.ocre{background:var(--ocre)}
#uso .u-fill.rojo{background:#c0453a}
#uso .u-marca{position:absolute; top:-2px; bottom:-2px; width:2px;
  background:var(--tinta); opacity:.55; border-radius:1px}
#uso .u-pct{flex:none; width:34px; text-align:right; font-variant-numeric:tabular-nums}
#uso .u-pct.rojo{color:#c0453a; font-weight:700}
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
  /* misma paleta que el resto (23-ago-2026): antes se forzaba oscura acá */
  width:min(560px, 92vw); max-height:66vh; display:flex; flex-direction:column;
  background:var(--card); color:var(--tinta); border:1px solid var(--borde);
  border-radius:14px; overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,.28);
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
#paleta .op.sel{background:rgba(var(--acento-rgb),.14)}
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
/* mientras la pestaña se arma. Antes el panel quedaba negro y mudo y no había
   forma de saber si estaba cargando o si se había roto algo (19-ago-2026) */
.term-box.cargando.ver::after{
  content:"cargando la sesión…"; position:absolute; inset:0; display:flex;
  align-items:center; justify-content:center; color:#8E8C84;
  font:13px/1 var(--display), system-ui, sans-serif; letter-spacing:.02em;
  background:var(--term-bg); border-radius:12px; animation:latir 1.2s ease-in-out infinite;
}
@keyframes latir{0%,100%{opacity:.45} 50%{opacity:.9}}
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
/* ---------- la BANDEJA de archivos de la sesión (11-set-2026) ----------
   Una tira al pie con todo lo que pasó por la charla (lo que subiste y lo que
   Claude produjo o mostró). Aparece sola cuando hay algo; tocar → se abre grande. */
#bandeja{
  display:none; flex:none; align-items:center; gap:8px; padding:7px 14px 8px;
  border-top:1px solid var(--borde); background:var(--panel); overflow-x:auto;
  overscroll-behavior-x:contain; -webkit-overflow-scrolling:touch;
}
#bandeja.ver{display:flex}
#bandeja .bl{flex:none; font-size:11px; color:var(--gris); writing-mode:vertical-rl;
  transform:rotate(180deg); letter-spacing:.06em; text-transform:uppercase; height:62px;
  display:flex; align-items:center}
.arch{
  flex:none; width:62px; height:62px; border-radius:11px; position:relative; cursor:pointer;
  background:var(--card); border:1.5px solid var(--borde); overflow:hidden;
  display:flex; align-items:center; justify-content:center; font-size:26px;
  transition:transform .12s ease-out;
}
.arch:hover{transform:translateY(-2px)}
.arch img{width:100%; height:100%; object-fit:cover; display:block}
.arch.vos{border-color:var(--acento)}
.arch .qn{
  position:absolute; left:0; right:0; bottom:0; font-size:9px; line-height:14px;
  text-align:center; color:#fff; background:rgba(31,30,27,.62); letter-spacing:.03em;
}
.arch.vos .qn{background:rgba(201,100,66,.85)}
.arch .ext{font-size:10px; font-weight:600; color:var(--gris); position:absolute; top:5px; right:6px;
  font-family:var(--display)}
/* el visor grande: velo + tarjeta; cabecera con nombre · quién · hora · nota */
#visor-arch{display:none; position:fixed; inset:0; z-index:60; background:rgba(31,30,27,.78);
  align-items:center; justify-content:center; padding:18px}
#visor-arch.ver{display:flex}
#visor-arch .va{
  background:var(--card); border-radius:16px; width:min(1100px,100%); height:min(92vh,100%);
  display:flex; flex-direction:column; overflow:hidden; box-shadow:0 20px 60px rgba(0,0,0,.4);
}
#visor-arch .vah{flex:none; display:flex; align-items:center; gap:10px; padding:10px 14px;
  border-bottom:1px solid var(--borde); background:var(--panel); min-width:0}
#visor-arch .vah .nom{flex:1; min-width:0; font-family:var(--display); font-size:14px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
#visor-arch .vah .nom small{display:block; font-family:inherit; font-size:11px; color:var(--gris);
  white-space:normal}
#visor-arch .vah button{border:1px solid var(--borde); background:var(--card); color:var(--tinta);
  border-radius:9px; padding:6px 10px; font-size:13px; cursor:pointer; flex:none}
#visor-arch .vah button.x{font-size:16px; line-height:1; padding:5px 9px}
#visor-arch .vac{flex:1; min-height:0; display:flex; align-items:center; justify-content:center;
  background:#1E1D1B; position:relative}
#visor-arch .vac img{max-width:100%; max-height:100%; object-fit:contain}
#visor-arch .vac iframe{width:100%; height:100%; border:0; background:#fff}
#visor-arch .vac video{max-width:100%; max-height:100%}
#visor-arch .vac pre{width:100%; height:100%; margin:0; padding:16px 20px; overflow:auto;
  background:var(--card); color:var(--tinta); font-size:13px; white-space:pre-wrap}
#visor-arch .vac .nada{color:#E8E6DC; text-align:center; font-size:15px; padding:30px}
#visor-arch .vac .nada b{display:block; font-size:54px; margin-bottom:12px}
@media (max-width:700px){
  #visor-arch{padding:0}
  #visor-arch .va{border-radius:0; width:100%; height:100%}
  #visor-arch .vah button span{display:none}
  #bandeja .bl{display:none}   /* en el teléfono cada píxel de ancho es una miniatura */
}
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
  #btn-nueva{width:32px; height:32px; font-size:17px}   /* el dedo, no el mouse */
  #btn-espera{width:32px; height:32px; font-size:15px}
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
  <img class="logo-foto" id="cara-m" src="/static/cacho.png" alt="">
  <span class="tit-m" id="tit-m">Cacho</span>
  <button id="btn-buscar" title="Buscar sesión">🔍</button>
</div>
<div id="velo"></div>
<div id="aviso-m"></div>
<div id="tira">
  <div id="tira-caras"></div>
  <div id="corriendo"><img src="/static/cacho-dibujo.png" alt="Cacho"></div>
</div>
<div id="side">
  <h1><img class="logo-foto" id="cara-area" src="/static/cacho.png" alt="">
      <span id="tit-area" title="">Cacho</span>
      <button id="btn-espera" title="Ver sólo las que terminaron y te esperan"
              aria-label="Ver sólo las que terminaron y te esperan">🔔<b></b></button>
      <button id="btn-nueva" title="Nueva sesión con Cacho"
              aria-label="Nueva sesión con Cacho">＋</button></h1>
  <div id="filtro-caja">
    <span class="lupa">🔍</span>
    <input id="filtro" placeholder="Buscar en las sesiones…" autocomplete="off"
           autocorrect="off" spellcheck="false">
    <button id="filtro-x" title="Limpiar (esc)">✕</button>
  </div>
  <div id="listas"></div>
  <button id="btn-avisos">🔔 Avisarme cuando una sesión termine</button>
  <div id="uso"></div>
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
  <div id="bandeja"><span class="bl">archivos</span></div>
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
<div id="visor-arch">
  <div class="va">
    <div class="vah">
      <div class="nom"><span id="va-nom"></span><small id="va-sub"></small></div>
      <button id="va-ruta" title="Pegar la ruta en la sesión activa">📎 <span>Ruta a la sesión</span></button>
      <button id="va-abrir" title="Abrir en otra pestaña del navegador">↗ <span>Abrir</span></button>
      <button class="x" id="va-x" title="Cerrar (esc)">✕</button>
    </div>
    <div class="vac" id="va-cuerpo"></div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
// teléfono: la barra lateral pasa a ser un cajón (ver CSS @media ≤700px)
const MQ_MOVIL = matchMedia("(max-width:700px)");
const MOVIL = MQ_MOVIL.matches;
/* RAÍZ (23-ago-2026): esto se congelaba al cargar la página y el CSS seguía
   midiendo en vivo. Si la ventana cargaba angosta (≤700px) y DESPUÉS se
   agrandaba, los dos quedaban en desacuerdo para siempre:
     · el JS creía "teléfono" → nunca prendía el 🎤 ni el ➤ del escritorio
       (micVisible() arranca con !MOVIL);
     · el CSS medía la ventana real y creía "escritorio" → dejaba escondida
       la barra de abajo del teléfono, que tiene su propio 🎤 y su ➤.
   Resultado: los dos botones desaparecidos, sin un solo error en la consola.
   Se ve en el pie de la lista: si dice "…terminadas hoy" SIN "· ⌘K buscar",
   el JS se cree teléfono. Como la página es un visor sin estado (las sesiones
   viven en el server, no acá), cruzar el umbral se arregla recargando: así
   JS y CSS no pueden discrepar nunca más. */
MQ_MOVIL.addEventListener("change", () => location.reload());
function menu(abrir){ document.body.classList.toggle("menu-abierto", abrir); }
let estado = {tabs:[], afuera:[], proyectos:[], casa:""};
let abiertas = {};        // id -> {term, fit, es, box}
let activa = null;
// Si el cajón de "Terminadas hoy" quedó abierto. Vive acá y no en el DOM porque
// render() reescribe #listas entero cada 4 s: sin esto, el cajón se cerraría solo
// a los cuatro segundos de abrirlo. Se recuerda entre recargas.
let terminadasAbiertas = false;
// try/catch: es el único localStorage de toda la app y esto corre en el tope del
// script — si el navegador lo tiene bloqueado, un throw acá deja a Cacho sin JS.
try{ terminadasAbiertas = localStorage.getItem("cacho_terminadas") === "1"; }catch(_){}
let filtroTxt = "";       // lo escrito en el buscador de la barra
/* El filtro del 🔔: mostrar SÓLO lo que terminó y espera respuesta. No se recuerda
   entre recargas, por lo mismo que el área: Cacho tiene que abrir mostrando todo. */
let soloEspera = false;
/* ─── ÁREAS (23-ago-2026) ───────────────────────────────────────────────────────────
   `areaActiva` null = ver TODO, que es como arranca siempre. Eso no es un detalle: la
   investigación decía que un cajón que esconde cosas se abandona, así que el estado por
   defecto muestra el panorama entero y el foco es algo que se pide, no algo que se sufre.
   Tampoco se recuerda entre recargas a propósito: si Cacho abriera filtrado en Jaime,
   el día que algo urgente esté en Xara no lo ves y no sabés por qué. */
let areaActiva = null;
const areaDe = o => (o && o.area) || estado.area_defecto || (estado.areas||[{}])[0].clave;
const areaInfo = k => (estado.areas || []).find(a => a.clave === k) || null;
const areaColor = k => (areaInfo(k) || {}).color || "var(--gris)";

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
    // scrollback: era 8000 (8× el default de xterm). Con 19 pestañas abiertas eso
    // es memoria del navegador que nadie mira: la charla completa se lee en el
    // visor de la sesión, acá alcanza con poder subir un rato (19-ago-2026).
    cursorBlink:true, scrollback:3000, allowProposedApi:true,
    theme:{
      // la terminal queda OSCURA a propósito (ver la nota de la paleta arriba):
      // los colores ANSI de Claude Code están pensados para fondo oscuro. El
      // cursor y la selección sí van en el coral de la casa (23-ago-2026).
      background:"#1E1D1B", foreground:"#E8E6DC", cursor:"#D97757",
      cursorAccent:"#1E1D1B", selectionBackground:"rgba(217,119,87,.38)"
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
  abiertas[id] = {term, fit, es:null, box, pos:null};   // pos: hasta qué byte tenemos
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
  // se le dice al server hasta qué byte tenemos: si lo tiene guardado nos manda
  // solo lo nuevo y la pestaña aparece al toque, sin repintarla entera (19-ago-2026)
  const desde = (a.pos == null) ? -1 : a.pos;
  a.box.classList.add("cargando");
  const es = new EventSource(`/api/term/${id}/stream?desde=${desde}`);
  es.addEventListener("base", e => {
    const b = JSON.parse(e.data);
    if(b.limpiar) a.term.reset();
    a.pos = b.pos;
    if(!b.bytes) a.box.classList.remove("cargando");   // no viene nada: no hay qué esperar
  });
  es.onmessage = e => {
    const bytes = b64bytes(e.data);
    a.pos = (a.pos || 0) + bytes.length;
    a.box.classList.remove("cargando");
    a.term.write(bytes);
  };
  es.addEventListener("fin", () => {
    a.box.classList.remove("cargando");
    a.term.write("\r\n\x1b[38;2;45;156;219m— sesión terminada —\x1b[0m\r\n");
    es.close(); a.es = null;
  });
  es.onerror = () => {
    // readyState 2 = CLOSED: el navegador NO va a reintentar (error HTTP o
    // corte definitivo). Sin esto la terminal quedaba congelada muda.
    if(es.readyState === 2){
      a.es = null;
      a.box.classList.remove("cargando");
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
  bandejaFirma = "";           // otra sesión: la tira se repinta sí o sí
  pintarBandeja();
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

/* Un disparo a la vez (21-ago-2026). Aparecieron TRES "Nueva sesión" vacías seguidas
   —cuatro pestañas entre las 10:44:20 y las 10:44:28, dos de ellas en el MISMO segundo—
   y parecía que Cacho las creaba solo. No: el arranque automático de más abajo sólo crea
   cuando no hay ninguna pestaña viva, y en ese momento había cuatro trabajando. O sea que
   sí o sí salieron de un click, repetido: `nueva()` tarda ~1 s en contestar y hasta
   entonces el botón seguía aceptando clicks, cada uno con su zsh + su claude + su pty.
   Con la mini al 100% de RAM eso no es cosmético.
   Dos disparos que no son "hacer click de nuevo a propósito" y que esto también tapa:
   el doble click, y el botón que quedó CON FOCO y se vuelve a activar con Enter o
   barra espaciadora (por eso además se le saca el foco al usarlo, más abajo). */
let creandoSesion = false;
let ultimaSesionCreada = 0;
const ESPERA_NUEVA = 1500;   // ms entre creaciones desde la interfaz

// `area`: con quién se abre la charla. Vacío = que la clasifique la máquina, como
// siempre. Va en la URL y no como cuerpo porque el server ya lee todo de la query.
async function nueva(cwd, area){
  if(creandoSesion || Date.now() - ultimaSesionCreada < ESPERA_NUEVA){
    // decirlo, no ignorar en silencio: si de verdad querías dos, en un segundo podés
    aviso("Esperá, ya estoy creando una sesión…");
    return;
  }
  creandoSesion = true;
  // feedback visible: antes fallaba MUDA (si el fetch se colgaba o el server
  // devolvía error, en el teléfono parecía que el botón no hacía nada)
  aviso("Creando sesión…");
  try{
    const r = await fetch("/api/term/new?cwd=" + encodeURIComponent(cwd)
                          + (area ? "&area=" + encodeURIComponent(area) : ""),
                          {method:"POST"});
    const j = await r.json();
    if(!j.id) throw new Error(j.error || ("HTTP " + r.status));
    await refrescar(); abrirTab(j.id); aviso("");
  }catch(err){
    aviso("No pude crear la sesión: " + err.message, true);
  }finally{
    creandoSesion = false;
    ultimaSesionCreada = Date.now();
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
// El renglón de una charla, en DOS alturas (25-ago-2026, pedido del usuario):
//   1) el título ENTERO, con su ícono de tema — sin cortar: si es largo, envuelve
//   2) abajo los iconitos: cara del área, estado, hora, peso, campanita, acciones
// Las líneas chicas de contexto/pedido se sacaron ("no me suman nada, no las
// llego a leer"): esa info sigue viva en el tooltip del renglón y en el buscador.
function itemHTML(o, attrs, opts){
  opts = opts || {};
  const sid = o.sid || "";
  const acciones = sid ? `
      <button class="acc fijar ${o.fija?"on":""}" data-fijar="${esc(sid)}"
        title="${o.fija?"Soltar de arriba":"Fijar arriba"}">${o.fija?"📌":"📍"}</button>
      <button class="acc" data-renombrar="${esc(sid)}" data-nombre="${esc(o.titulo)}"
        title="Ponerle un nombre">✏️</button>
      <button class="acc" data-area-de="${esc(sid)}" data-area-hoy="${esc(areaDe(o))}"
        title="Cambiarla de área">🏷️</button>` : "";
  const espera = esperan.has(claveDe(o));
  // el título no se toca nunca si tiene nombre propio; el ícono es de al lado
  const mutó = o.tema && o.tema_ini && o.tema !== o.tema_ini;
  // Lo que antes iba en letras chicas abajo (contexto, pedido, proyecto) vive acá:
  // se lee posando el mouse, y el buscador lo sigue encontrando (textoDe).
  const tip = [o.titulo, o.tema ? "tema: "+o.tema+(mutó?" (arrancó en "+o.tema_ini+")":"") : "",
               o.contexto, o.pedido ? "⟶ " + o.pedido : "", o.proyecto]
              .filter(Boolean).join("\n");
  const ar = areaDe(o), arI = areaInfo(ar);
  // La cara del área en el renglón: es lo que te dice de quién es cada cosa AUNQUE estés
  // viendo todo. Sin esto, el panorama vuelve a ser una lista plana.
  const caraAr = arI
    ? `<img class="cara-ar" src="/static/${esc(arI.cara)}" alt=""
            title="${esc(arI.nombre)} · ${esc(arI.rol)}${o.area_propia?" (se lo pusiste vos)":""}">`
    : "";
  return `
      <div class="item ${opts.activo?"activo":""} ${opts.fijada?"fijada":""} ${espera?"avisada":""}" ${attrs}
           data-sid="${esc(sid)}" data-ar="${esc(ar)}"
           ${opts.fijada?'draggable="true"':""} title="${esc(tip)}"
           style="border-left-color:${esc(areaColor(ar))}">
        <div class="tit ${o.renombrada?"propio":""}"
          >${o.icono?'<span class="tema-ico">'+o.icono+'</span> ':""}${esc(o.titulo)}${
            mutó?'<span class="muto"> → '+esc(o.tema)+'</span>':""}</div>
        <div class="fila">${caraAr}<span class="dot ${o.estado}"
            title="${o.estado==="trabajando"?"trabajando":o.estado==="esperando"?"quieta, te espera":"terminada"}"></span>
          ${espera?'<span class="campanita" title="terminó y te espera">🔔</span>':""}
          ${opts.activo?'<span class="aca-estas">acá estás</span>':""}
          <span class="hs">${hace(o.hace_seg)}</span>
          ${o.peso&&o.peso!=="verde"?'<span class="peso" title="'+esc(o.peso_nota)+'">'
             +(o.peso==="rojo"?"🔴":"🟡")+'</span>':""}
          <span style="flex:1"></span>${acciones}${opts.botonX||""}</div>
        ${o.peso==="rojo"&&opts.activo?'<div class="peso-aviso"><span>'+esc(o.peso_nota)
          +'</span><button onclick="event.stopPropagation();nueva('+JSON.stringify(o.cwd||"")
          +')">Nueva</button></div>':""}
      </div>`;
}

/* Buscar: lo usan el campo de la barra y el ⌘K, para que los dos entiendan lo
   mismo. Por PALABRAS y no por frase seguida: los títulos arrancan con emoji y
   traen cosas en el medio, así el orden y lo que haya entremedio no importan.
   Sin tildes: "meta ads" tiene que encontrar "Metá". */
function buscador(q){
  const norm = s => String(s || "").toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "");
  const palabras = norm(q).trim().split(/\s+/).filter(Boolean);
  return txt => { const s = norm(txt); return palabras.every(w => s.includes(w)); };
}
// Todo lo que se puede escribir de una charla para encontrarla: cómo se llama,
// de qué va, qué está tocando y qué le pediste.
function textoDe(o){
  return [o.titulo, o.tema, o.tema_ini, o.contexto, o.pedido, o.proyecto,
          o.ultimo_txt].filter(Boolean).join(" ");
}

// Los data-* de cada tipo de sesión, para poder dibujarla igual en su sección o arriba.
function attrsDe(o){
  if(o.__tab) return `data-tab="${o.id}"`;
  if(o.tipo === "terminal" && o.viva) return `data-tty="${esc(o.tty)}"`;
  return `data-ver="${esc(o.id)}" data-vtit="${esc(o.titulo)}" data-viva="${o.viva?1:0}"`;
}

let tiraFirma = "";      // cómo estaba la tira la última vez que se dibujó
function pintarTira(){
  const t = $("#tira-caras");   // sólo las caras: el pie (Cacho trabajando) no se repinta
  if(!t || !estado.areas) return;
  // Cuántas sesiones vivas tiene cada área. Se cuenta sobre TODO lo vivo (pestañas de la
  // app + lo de afuera), sin el buscador: el número tiene que decir cuánto hay, no cuánto
  // hay de lo que estás buscando.
  const vivas = estado.tabs.concat(estado.afuera.filter(s => s.viva));
  const cuenta = {};
  vivas.forEach(o => { const a = areaDe(o); cuenta[a] = (cuenta[a] || 0) + 1; });
  // Sólo se redibuja si CAMBIÓ algo. `render()` corre cada 4 s y rearmar el innerHTML
  // recreaba las cinco <img> cada vez: no se vuelven a bajar (están en caché) pero es
  // trabajo de DOM al pedo en un bucle permanente, y en el celular se notaba el parpadeo.
  const firma = estado.areas.map(a => a.clave + ":" + (cuenta[a.clave] || 0)).join("|")
                + "|" + (areaActiva || "");
  if(firma === tiraFirma) return;
  tiraFirma = firma;
  t.innerHTML = estado.areas.map(a => {
    const n = cuenta[a.clave] || 0;
    const on = areaActiva === a.clave;
    return `<button class="ar ${on?"on":""} ${n?"":"vacia"}" data-area="${esc(a.clave)}"
              style="--ar-col:${esc(a.color)}"
              title="${esc(a.nombre)} · ${esc(a.rol)} — le habla a ${esc(a.gente)}${
                n ? "\n" + n + (n===1?" sesión":" sesiones") : "\nsin sesiones vivas"}${
                on ? "\n\n(tocá de nuevo para ver todo)" : ""}">
              <img src="/static/${esc(a.cara)}" alt="${esc(a.nombre)}"><b>${n}</b></button>`;
  }).join("");
}

/* El marco de área alrededor de la terminal abierta. Se recalcula al cambiar de pestaña y
   al repintar: si la sesión cambia de área sola (porque cambió de tema), el marco la sigue. */
function pintarMarcoArea(){
  const t = $("#terms");
  if(!t) return;
  let k = null;
  if(activa){
    const o = estado.tabs.find(x => x.id === activa)
           || (estado.afuera || []).find(x => ("ver:"+x.id) === activa);
    if(o) k = areaDe(o);
  }
  // EL MARCO es de la terminal: dice en qué contexto estás TIPEANDO, así que lo manda la
  // sesión abierta y sólo cae al filtro cuando no hay ninguna («producción es rojo»).
  const kMarco = k || areaActiva;
  t.classList.toggle("con-area", !!kMarco);
  if(kMarco) t.style.setProperty("--ar-col", areaColor(kMarco));
  document.body.classList.toggle("area-fija", !!areaActiva);
  // EL NOMBRE DE ARRIBA es OTRA cosa: es CON QUIÉN ESTÁS HABLANDO, y eso lo decide el usuario
  // tocando una cara en la tira. Por eso el filtro le gana a la sesión abierta y no al
  // revés (27-ago-2026): elegir a Carla es una decisión explícita; el área de la sesión
  // es una inferencia de la máquina, y una inferencia no puede pisar una decisión. Con la
  // tira en Carla y esta misma terminal abierta (que es de Cacho), la barra decía "Cacho".
  // Sin nada elegido sigue mandando la sesión abierta, que es lo que ya andaba bien.
  // El ✕ para salir del filtro va SÓLO cuando hay filtro: sin él no hay nada que salir.
  const tit = $("#tit-area");
  const ai = areaInfo(areaActiva || k || "");
  if(tit){
    tit.textContent = ai ? ai.nombre : "Cacho";
    tit.title = ai ? (ai.rol + " — le habla a " + ai.gente
                      + (areaActiva ? " · tocá para ver todo" : "")) : "";
    tit.classList.toggle("en-area", !!areaActiva);
    if(ai) tit.style.setProperty("--ar-col", ai.color);
  }
  // El ＋ hace EXACTAMENTE lo que dice el nombre de al lado, y por eso lee la misma `ai`
  // que el título en vez de `areaActiva` a secas. Con la tira sin filtro y una sesión de
  // Jaime abierta, arriba dice "Jaime": si el ＋ abriera una sesión sin área, el botón
  // estaría diciendo una cosa y haciendo otra — que es peor que no tenerlo. El área
  // viaja en el dataset porque el click se atiende en otro scope, y así no hace falta
  // una variable global más que mantener sincronizada.
  // El color se setea en el <h1> (el padre) y no en el título: `--ar-col` se hereda hacia
  // abajo, así que puesto acá lo agarran el nombre Y el botón; puesto en el <span> el
  // botón —que es su hermano— se quedaba sin él y salía siempre del color de la casa.
  const h1 = $("#side h1"), bn = $("#btn-nueva");
  if(h1 && ai) h1.style.setProperty("--ar-col", ai.color);
  if(bn){
    const con = "Nueva sesión con " + (ai ? ai.nombre : "Cacho");
    // Sin cara elegida el botón dice «con Cacho», y hasta el 11-set-2026 mandaba área
    // vacía: la pestaña nacía PELADA (sin el arranque de Cacho, sin MEMORY-cacho.md) y la
    // tira la mostraba como Cacho igual — decía una cosa y hacía otra. Cacho es el área
    // por defecto en todos lados; también al nacer.
    bn.dataset.nuevaEn = ai ? ai.clave : (estado.area_defecto || "");   // NO `data-area`: ése es de la tira
    bn.title = con;
    bn.setAttribute("aria-label", con);
  }
  // La cara va con el nombre, siempre. Estaba clavada en cacho.png y decía "Carla" con la
  // cara de Cacho al lado: en un sistema donde LA CARA IDENTIFICA (ver areas.py), una cara
  // que no corresponde no es un detalle estético, es el identificador mintiendo.
  ponerCara("#cara-area", ai ? ai.cara : null);
  // Arriba en el celular el título es el de la SESIÓN abierta, así que ahí la cara es la del
  // área de esa sesión (`k`), no la del filtro elegido.
  ponerCara("#cara-m", (areaInfo(k || areaActiva || "") || {}).cara || null);
}

/* Cambia una cara sólo si de verdad cambió: `render()` corre cada 4 s y reescribir el src
   idéntico reinicia la animación de "respira" del logo cuando hay trabajo. */
function ponerCara(sel, cara){
  const img = $(sel);
  if(!img) return;
  const src = "/static/" + (cara || "cacho.png");
  if(img.getAttribute("src") !== src) img.setAttribute("src", src);
}

/* El botón 🔔: cuántas terminaron y te esperan. Cuenta lo mismo que va a mostrar —
   respeta el área elegida en la tira, no el buscador— así que el número nunca puede
   discrepar con la lista que aparece al tocarlo. Las automáticas no cuentan (no esperan
   nada de nadie) y las terminadas tampoco (ya no esperan). En cero queda tenue y sin
   número: sigue estando, pero no grita.
   `te_espera` lo decide el server leyendo el transcript (11-set-2026): «quieta» no es
   «te espera» — una pestaña nueva sin nada escrito está quieta y no espera nada. */
function pintarBotonEspera(){
  const b = $("#btn-espera");
  if(!b) return;
  const vivas = estado.tabs.map(t => Object.assign({__tab:true}, t))
      .concat((estado.afuera || []).filter(s => s.viva && s.tipo === "terminal"));
  const n = vivas.filter(o => o.te_espera
                          && (!areaActiva || areaDe(o) === areaActiva)).length;
  b.classList.toggle("on", soloEspera);
  b.classList.toggle("vacio", n === 0);
  const bb = b.querySelector("b");
  if(bb && bb.textContent !== String(n)) bb.textContent = String(n);
  const tit = soloEspera ? "Ver todas de nuevo"
    : n ? n + (n === 1 ? " terminó y te espera" : " terminaron y te esperan")
        : "Nada esperándote ahora";
  if(b.title !== tit){ b.title = tit; b.setAttribute("aria-label", tit); }
}

function render(){
  // barra lateral. Con el buscador escrito, TODAS las secciones quedan filtradas
  // (las fijadas incluidas: si buscás algo, buscás en todo) y se muestran más
  // terminadas de lo habitual — a una vieja se llega buscándola, no bajando.
  const q = (filtroTxt || "").trim();
  const casaCon = buscador(q);
  // El área filtra ANTES que el buscador. Con un área elegida, buscar busca adentro de esa
  // área — que es lo que espera cualquiera que acaba de decir «estoy en publicidad».
  // Con el 🔔 prendido queda sólo lo que TERMINÓ SU TRABAJO y está quieto esperando que
  // el usuario conteste: lo que está trabajando todavía no espera nada, y lo terminado ya no
  // espera nunca más (esas caen solas: su estado es "terminada").
  const filtrar = a => {
    let r = areaActiva ? a.filter(o => areaDe(o) === areaActiva) : a;
    if(soloEspera) r = r.filter(o => o.te_espera);
    return q ? r.filter(o => casaCon(textoDe(o))) : r;
  };
  const tabs = filtrar(estado.tabs.map(t => Object.assign({__tab:true}, t)));
  const vivasAfuera = filtrar(estado.afuera.filter(s => s.viva));
  const term = vivasAfuera.filter(s => s.tipo === "terminal");
  const autos = vivasAfuera.filter(s => s.tipo !== "terminal");
  const muertas = estado.afuera.filter(s => !s.viva).length;
  const esActiva = o => o.__tab ? o.id===activa : ("ver:"+o.id)===activa;
  const botonX = t => `<button class="x" data-x="${t.id}"
            title="Cerrar esta sesión">✕</button>`;
  let h = "";

  // ── FIJADAS: salen de su sección y suben al tope, en el orden que las dejaste ──
  const fijadas = tabs.concat(filtrar(estado.afuera)).filter(o => o.fija);
  fijadas.sort((a,b) => (a.orden==null?9999:a.orden) - (b.orden==null?9999:b.orden));
  const fijos = new Set(fijadas.map(o => o.sid));
  if(fijadas.length){
    h += '<div class="seccion">📌 Fijadas</div>' + fijadas.map(o =>
      itemHTML(o, attrsDe(o), {activo:esActiva(o), fijada:true,
                               botonX: o.__tab ? botonX(o) : ""})).join("");
  }
  const libres = a => a.filter(o => !fijos.has(o.sid));

  const tabsL = libres(tabs);
  // Adentro del filtro, arriba las que terminaron DESDE QUE LAS MIRASTE (las del 🔔 del
  // ítem). El sort de JS es estable, así que el resto conserva el orden del server.
  if(soloEspera) tabsL.sort((a, b) =>
    (esperan.has(claveDe(a)) ? 0 : 1) - (esperan.has(claveDe(b)) ? 0 : 1));
  if(tabsL.length){
    h += '<div class="seccion">En esta app</div>' +
      tabsL.map(t => itemHTML(t, attrsDe(t), {activo:esActiva(t), botonX:botonX(t)})).join("");
  }
  const termL = libres(term);
  if(termL.length){
    h += '<div class="seccion">En Terminal (afuera)</div>' +
      termL.map(s => itemHTML(s, attrsDe(s), {})).join("");
  }
  // Las automáticas corren solas y no esperan que nadie les conteste (mismo motivo por el
  // que no avisan al terminar, ver revisarAvisos): adentro del filtro no van.
  const autosL = soloEspera ? [] : libres(autos);
  if(autosL.length){
    h += '<div class="seccion">Automáticas</div>' +
      autosL.map(s => itemHTML(s, attrsDe(s), {activo:esActiva(s)})).join("");
  }
  const terminadas = soloEspera ? []
                  : libres(filtrar(estado.afuera.filter(s => !s.viva)));
  const term12 = terminadas.slice(0, q ? 40 : 12);
  if(term12.length){
    // Plegadas salvo que estés buscando o que la sesión abierta sea una de ellas.
    const abierto = !!q || terminadasAbiertas || term12.some(esActiva);
    const sobran = terminadas.length - term12.length;
    h += '<details class="plegable"' + (abierto ? " open" : "") + '>' +
      '<summary class="seccion"><span class="flecha">' + (abierto ? "▼" : "▶") +
        '</span> Terminadas hoy<span class="cuantas">' + terminadas.length +
        '</span></summary>' +
      term12.map(s => itemHTML(s, attrsDe(s), {activo:esActiva(s)})).join("") +
      (sobran > 0 ? '<div class="mas">y ' + sobran + ' más — buscala por su nombre</div>' : "") +
      '</details>';
  }
  if(!h && q){
    h = '<div class="nada-con-eso">Nada con «' + esc(q) + '»' +
        (areaActiva ? ' en ' + esc((areaInfo(areaActiva)||{}).nombre || "") : "") + '.<br>' +
        'Se busca en el título, el tema, lo que se está tocando y lo que pediste.</div>';
  }
  if(!h && soloEspera){
    h = '<div class="nada-con-eso">Nada esperándote' +
        (areaActiva ? ' en ' + esc((areaInfo(areaActiva)||{}).nombre || "") : "") +
        '.<br>Tocá el 🔔 de arriba para ver todo.</div>';
  }
  if(!h && areaActiva && !q){
    const ai = areaInfo(areaActiva) || {};
    h = '<div class="nada-con-eso">Nada en ' + esc(ai.nombre || "") + ' ahora mismo.<br>' +
        'Tocá su cara de nuevo para ver todo.</div>';
  }
  $("#listas").innerHTML = h || '<div class="seccion">Sin sesiones vivas</div>';
  pintarTira();
  pintarMarcoArea();
  // Cacho corre mientras haya trabajo adentro de la app (en el teléfono, donde
  // no se ve la barra, es el logo de arriba el que trota)
  const hayTrabajo = estado.tabs.some(t => t.estado === "trabajando");
  $("#corriendo").classList.toggle("ver", hayTrabajo);
  document.body.classList.toggle("hay-trabajo", hayTrabajo);
  const cuantas = tabs.length + vivasAfuera.length + term12.length;
  $("#pie").textContent = q
    ? cuantas + (cuantas === 1 ? " charla" : " charlas") + " con «" + q + "» · esc para limpiar"
    : soloEspera
    ? cuantas + (cuantas === 1 ? " terminó y te espera" : " terminaron y te esperan")
    : estado.tabs.length + " en la app · " + vivasAfuera.length + " afuera · " +
      muertas + " terminadas hoy" + (MOVIL ? "" : " · ⌘K buscar");
  pintarBotonEspera();
  if(MOVIL){
    const t = estado.tabs.find(t => t.id === activa);
    $("#tit-m").textContent = t ? ((t.icono ? t.icono + " " : "") + t.titulo) :
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

/* ---------------- buscador de la barra ---------------- */
function buscar(txt){
  filtroTxt = txt || "";
  const caja = $("#filtro");
  if(caja && caja.value !== filtroTxt) caja.value = filtroTxt;
  document.body.classList.toggle("buscando", !!filtroTxt.trim());
  render();
}
$("#filtro").addEventListener("input", e => buscar(e.target.value));
$("#filtro").addEventListener("keydown", e => {
  if(e.key === "Escape"){ buscar(""); e.target.blur(); }
  // ⏎ abre la primera de la lista: buscar y entrar sin soltar el teclado
  if(e.key === "Enter"){
    const primera = $("#listas").querySelector(".item");
    if(primera) primera.click();
  }
});

/* ---------------- eventos ---------------- */
document.addEventListener("click", e => {
  if(e.target.closest("#filtro-x")){ buscar(""); $("#filtro").focus(); return; }
  if(e.target.closest("#btn-buscar")){ menu(false); abrirPaleta(); return; }
  if(e.target.closest("#btn-menu")){
    menu(!document.body.classList.contains("menu-abierto"));
    return;
  }
  if(e.target.id === "velo"){ menu(false); return; }
  // El cajón de las terminadas: lo abrimos nosotros (preventDefault) para que el
  // repintado de cada 4 s lo vuelva a dibujar como lo dejaste.
  const pleg = e.target.closest("summary");
  if(pleg){
    e.preventDefault();
    terminadasAbiertas = !terminadasAbiertas;
    try{ localStorage.setItem("cacho_terminadas", terminadasAbiertas ? "1" : "0"); }catch(_){}
    render();
    return;
  }
  // Salir del área desde el nombre de arriba: es el único camino cuando la tira no está.
  if(e.target.closest("#tit-area.en-area")){
    e.stopPropagation();
    areaActiva = null;
    pintarTira(); render(); pintarMarcoArea();
    return;
  }
  // ── La tira: entrar a un área, o volver a ver todo tocando la misma otra vez ──
  // ACOTADO A LA TIRA (29-ago-2026). Decía `closest("[data-area]")` a secas, o sea que
  // reclamaba el click de CUALQUIER elemento de la página con ese atributo. Se lo comió
  // al ＋ de arriba el mismo día que nació —le habíamos puesto `data-area` para saber con
  // quién abrir la sesión—: el botón, en vez de crear nada, apagaba el filtro. Y falla
  // así de callado: el click "funciona", hace otra cosa. Un atributo no puede ser un
  // contrato global de toda la app; el handler dice ahora DÓNDE vive lo que atiende.
  const ar = e.target.closest("#tira-caras [data-area]");
  if(ar){
    e.stopPropagation();
    const k = ar.dataset.area;
    areaActiva = (areaActiva === k) ? null : k;
    pintarTira(); render(); pintarMarcoArea();
    // El buscador se limpia al cambiar de área: quedaba filtrando sobre la nueva y daba
    // «nada con eso» en un área que sí tenía cosas.
    if(filtroTxt) buscar("");
    return;
  }
  // ── Corregir a qué área es una sesión (la máquina propone, el usuario decide) ──
  const cam = e.target.closest("[data-area-de]");
  if(cam){
    e.stopPropagation();
    const sid = cam.dataset.areaDe, hoy = cam.dataset.areaHoy;
    const ops = (estado.areas || []);
    const lista = ops.map((a,i) => (i+1) + ") " + a.nombre + " — " + a.rol +
                                   (a.clave===hoy ? "  ← ahora" : "")).join("\n");
    const r = prompt("¿De qué área es esta sesión?\n\n" + lista +
                     "\n\n0) que la elija sola\n\nNúmero:", "");
    if(r === null) return;
    const n = parseInt(r.trim(), 10);
    if(r.trim() === "0"){ guardarMeta(sid, {area: ""}); return; }
    if(n >= 1 && n <= ops.length) guardarMeta(sid, {area: ops[n-1].clave});
    return;
  }
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
  // El 🔔 de arriba: prende y apaga el filtro de "terminó y te espera". blur() por lo
  // mismo que el ＋: pegado al buscador, con el foco puesto cualquier Enter lo redispara.
  const be = e.target.closest("#btn-espera");
  if(be){
    be.blur();
    soloEspera = !soloEspera;
    document.body.classList.toggle("solo-espera", soloEspera);
    render();
    return;
  }
  const bg = e.target.closest("#btn-nueva");
  if(bg){
    // Nueva sesión CON QUIEN ESTÁS PARADO. El proyecto es el primero de la lista,
    // salvo que el área declare el suyo (`proyecto` en areas.py). Sin nadie elegido
    // en la tira sale igual que antes:
    // sesión pelada que clasifica sola.
    // blur() a propósito: es el único botón que crea una sesión de UN click, está
    // pegado arriba del buscador, y si se queda con el foco cualquier Enter o barra
    // espaciadora posterior lo vuelve a disparar sin que nadie lo haya tocado.
    bg.blur();
    const ar = areaInfo(bg.dataset.nuevaEn || "");
    const quiero = (ar && ar.proyecto) || (estado.proyectos[0] || {}).nombre || "";
    const p = estado.proyectos.find(p => p.nombre.includes(quiero));
    if(!p){ aviso("No encuentro la carpeta «" + quiero + "» en ~/Claude/Projects"); return; }
    nueva(p.cwd, bg.dataset.nuevaEn || "");
    return;
  }
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
// Después de soltar un archivo hay DOS focos distintos, y confundirlos es por qué
// esto seguía sin andar (queja del usuario, 23-ago-2026: "tengo que volver a clickear
// adentro de la pantalla"):
//   1) el foco de VENTANA, que lo maneja macOS. Arrastrás desde el Finder —que es
//      la app del frente— y soltar NO le pasa el foco de teclado a la ventana que
//      recibe el archivo: lo que escribías se lo llevaba el Finder. Ningún focus()
//      de JavaScript puede arreglar eso, la página no se puede traer al frente
//      sola. Por eso se lo pedimos al server: /api/frente levanta la ventana con
//      AppleScript. Se pide UNA vez y sólo si la ventana no tiene el foco.
//   2) el foco DENTRO de la página, que sí es nuestro: va al textarea de xterm. Un
//      solo focus() no alcanza (el navegador termina de digerir el drop DESPUÉS de
//      este handler y lo pisa), así que se insiste hasta VERIFICAR que quedó donde
//      tiene que estar, no hasta que se acaben unos disparos a ciegas.
// Si en ~1,7 s no lo logró, lo dice en pantalla: quedarse callado es dejar a el usuario
// tecleando contra la nada, que es exactamente lo que se viene a arreglar.
let focoTimers = [];
let focoDesde = 0;
function enfocarSesion(){
  // En el teléfono NO se enfoca xterm: su textarea oculto rompe el teclado de
  // iOS (por eso queda inputmode="none"). Ahí el foco va al cajón de abajo.
  if(MOVIL){ const ta = $("#texto-m"); if(ta) ta.focus(); return; }
  const a = abiertas[activa];
  if(!a) return;
  focoTimers.forEach(clearTimeout); focoTimers = [];
  focoDesde = Date.now();
  const listo = () => {
    if(!document.hasFocus()) return false;          // el teclado lo tiene otra app
    const ta = (a.term && a.term.textarea) || null; // xterm 5.5 lo expone
    if(ta) return document.activeElement === ta;
    const el = a.term && a.term.element;            // por si algún día no lo expone
    return !!(el && el.contains(document.activeElement));
  };
  let pedidoFrente = false, intentos = 0;
  const paso = () => {
    if(listo()) return;
    if(!document.hasFocus() && !pedidoFrente){
      pedidoFrente = true;
      fetch("/api/frente", {method:"POST"}).catch(()=>{});
    }
    try{ window.focus(); a.term.focus(); }catch(e){}
    if(++intentos < 24) focoTimers.push(setTimeout(paso, 70));
    else aviso("No pude devolverte el foco: dale un clic a la sesión", true);
  };
  paso();
}
// Si en medio de la insistencia tocás otra cosa (la barra, un botón), mandás vos:
// se cortan los reintentos para no robarte el foco de vuelta. Los primeros 300 ms
// no cuentan: al soltar, el propio arrastre deja caer eventos de puntero sobre la
// página y con eso se cancelaba la insistencia antes de que empezara.
document.addEventListener("pointerdown", () => {
  if(Date.now() - focoDesde < 300) return;
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
    } else if(estadoPrev[k] === "trabajando" && o.te_espera){
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
  const casaCon = buscador(q);
  const casa = o => casaCon(textoDe(o));
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
/* ---------- la BANDEJA de archivos (11-set-2026) ----------
   Lo que pasó por la charla activa, según SU transcript (/api/sesion/<sid>/archivos):
   lo que subiste y lo que Claude produjo o mostró. Se repinta con el refresco de 4 s
   y al cambiar de sesión; si la lista no cambió no se toca el DOM (la tira scrollea). */
const ICONO_ARCH = {pdf:"📄", html:"🌐", texto:"📝", doc:"📊", video:"🎬", audio:"🎧", imagen:"🖼"};
let bandejaFirma = "", bandejaItems = [], bandejaSid = "";
function sidActiva(){
  if(!activa) return "";
  if(activa.startsWith("ver:")) return activa.slice(4);
  const t = estado.tabs.find(x => x.id === activa);
  return (t && t.sid) || "";
}
function urlArch(it, mini){
  return "/api/archivo?sid=" + encodeURIComponent(bandejaSid) +
         "&ruta=" + encodeURIComponent(it.ruta) + (mini ? "&mini=1" : "") +
         "&v=" + it.mtime;   // el mtime en la URL: si el archivo cambia, la miniatura también
}
function mostrarBandeja(ver){
  const b = $("#bandeja");
  if(b.classList.contains("ver") === ver) return;
  b.classList.toggle("ver", ver);
  // la tira le saca alto a la terminal: xterm tiene que volver a medir
  const a = abiertas[activa];
  if(a) requestAnimationFrame(() => { a.fit.fit(); ubicarBotones(); });
}
async function pintarBandeja(){
  const sid = sidActiva();
  if(!sid){ bandejaSid = ""; bandejaFirma = ""; mostrarBandeja(false); return; }
  try{
    const r = await fetch(`/api/sesion/${sid}/archivos`);
    const j = await r.json();
    if(j.error) throw new Error(j.error);
    if(sid !== sidActiva()) return;   // cambiaste de sesión mientras cargaba
    const items = j.items || [];
    const firma = sid + "|" + items.map(i => i.ruta + "@" + i.mtime).join("|");
    if(firma === bandejaFirma) return;
    const nuevos = bandejaSid === sid && items.length > bandejaItems.length;
    bandejaFirma = firma; bandejaItems = items; bandejaSid = sid;
    const b = $("#bandeja");
    b.querySelectorAll(".arch").forEach(e => e.remove());
    items.forEach((it, i) => {
      const d = document.createElement("div");
      d.className = "arch " + it.quien; d.dataset.i = i;
      d.title = it.nombre + " · " + (it.quien === "vos" ? "lo subiste vos" : "de Claude") +
                (it.hora ? " · " + it.hora : "") + (it.nota ? "\n" + it.nota : "");
      const conMini = !["audio", "texto"].includes(it.tipo);
      d.innerHTML = (conMini
          ? `<img loading="lazy" alt="" src="${urlArch(it, true)}"
                  onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${ICONO_ARCH[it.tipo] || "📎"}'}))">`
          : `<span>${ICONO_ARCH[it.tipo] || "📎"}</span>`) +
        `<span class="ext">${esc(it.ext.slice(1).toUpperCase())}</span>` +
        `<span class="qn">${it.quien === "vos" ? "vos" : "Claude"}</span>`;
      d.addEventListener("click", () => abrirArchivo(i));
      b.appendChild(d);
    });
    mostrarBandeja(items.length > 0);
    if(nuevos || !b.dataset.pos) requestAnimationFrame(() => { b.scrollLeft = b.scrollWidth; b.dataset.pos = "1"; });
  }catch(err){
    // sin bandeja no se cae nada: la sesión sigue igual
    console.warn("bandeja:", err.message);
  }
}
let archAbierto = null;
function abrirArchivo(i){
  const it = bandejaItems[i];
  if(!it) return;
  archAbierto = it;
  const url = urlArch(it, false);
  $("#va-nom").textContent = it.nombre;
  $("#va-sub").textContent = (it.quien === "vos" ? "lo subiste vos" : "de Claude") +
      (it.hora ? " · " + it.hora : "") + " · " + tamanio(it.bytes) + (it.nota ? " · " + it.nota : "");
  const c = $("#va-cuerpo");
  c.innerHTML = "";
  if(it.tipo === "imagen" && it.ext !== ".heic"){
    c.innerHTML = `<img src="${url}" alt="">`;
  } else if(it.tipo === "pdf" || it.tipo === "html"){
    // sandbox SOLO al .html: un archivo que se mira no corre con el origen de Cacho (el
    // server ya lo manda con CSP sandbox; acá se repite por si un navegador lo ignora).
    // Al PDF no: el visor de PDF de Chrome adentro de un iframe sandboxeado queda en blanco.
    // En iPhone el iframe muestra la primera página nomás: para el resto está ↗ Abrir.
    const f = document.createElement("iframe");
    f.src = url; if(it.tipo === "html") f.setAttribute("sandbox", ""); c.appendChild(f);
  } else if(it.tipo === "video"){
    c.innerHTML = `<video controls playsinline autoplay src="${url}"></video>`;
  } else if(it.tipo === "audio"){
    c.innerHTML = `<div class="nada"><b>🎧</b>${esc(it.nombre)}<br><br><audio controls autoplay src="${url}"></audio></div>`;
  } else if(it.tipo === "texto"){
    c.innerHTML = `<pre>cargando…</pre>`;
    fetch(url).then(r => r.text()).then(t => { if(archAbierto === it) c.querySelector("pre").textContent = t; })
      .catch(err => { c.querySelector("pre").textContent = "No pude leerlo: " + err.message; });
  } else {
    // Excel, Word, HEIC…: el navegador no lo dibuja adentro; ↗ lo abre o lo baja
    c.innerHTML = `<div class="nada"><b>${ICONO_ARCH[it.tipo] || "📎"}</b>${esc(it.nombre)}<br>
      <small>Este tipo no se ve acá adentro: tocá <b style="display:inline;font-size:inherit">↗ Abrir</b> y lo abre el navegador (o lo baja).</small></div>`;
  }
  $("#va-ruta").style.display = (activa && abiertas[activa]) ? "" : "none";
  $("#visor-arch").classList.add("ver");
}
function cerrarArchivo(){
  $("#visor-arch").classList.remove("ver");
  $("#va-cuerpo").innerHTML = "";   // corta el video/audio que estuviera sonando
  archAbierto = null;
}
function tamanio(b){
  if(b < 1024) return b + " b";
  if(b < 1024 * 1024) return (b / 1024).toFixed(0) + " KB";
  return (b / 1048576).toFixed(1).replace(".", ",") + " MB";
}
$("#va-x").addEventListener("click", cerrarArchivo);
$("#visor-arch").addEventListener("click", e => { if(e.target === $("#visor-arch")) cerrarArchivo(); });
$("#va-abrir").addEventListener("click", () => { if(archAbierto) window.open(urlArch(archAbierto, false), "_blank"); });
$("#va-ruta").addEventListener("click", () => {
  if(!archAbierto || !activa || !abiertas[activa]) return;
  mandar("\x1b[200~\"" + archAbierto.ruta + "\" \x1b[201~");
  cerrarArchivo(); enfocarSesion();
  aviso("La ruta quedó en la sesión — agregá texto si querés y mandá ⏎");
});
document.addEventListener("keydown", e => {
  if(e.key === "Escape" && $("#visor-arch").classList.contains("ver")){
    e.preventDefault(); e.stopPropagation(); cerrarArchivo();
  }
}, true);

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

/* ── Uso del plan: el tubo de abajo de la barra ──────────────────────────────
   Relleno = % usado; rayita = % que corresponde a esta altura de la ventana.
   Se muestran la semana general y Fable siempre; la sesión de 5 h sólo cuando
   pica (≥50%), que es cuando importa. "Sin dato" se dice, no se adivina. */
function pintarUsoHTML(j){
  const el = $("#uso");
  if(!el) return;
  if(!j || !j.ok){
    el.innerHTML = '<div class="u-lin" title="' + esc((j && j.error) || "") +
                   '">uso del plan: sin dato</div>';
    return;
  }
  const coma = n => String(n).replace(".", ",");
  el.innerHTML = j.topes.filter(t =>
      t.nombre !== "sesión 5 h" || t.pct >= 50
    ).map(t => {
      const ritmo = t.ritmo || 0;
      const clase = ritmo <= 1.15 ? "" : (ritmo <= 1.6 && !t.se_acaba ? "ocre"
                    : (t.se_acaba ? "rojo" : "ocre"));
      const tip = [
        t.pct + "% usado",
        t.esperado != null ? "a esta altura corresponde " + Math.round(t.esperado) + "%" : "",
        ritmo ? "ritmo " + coma(ritmo.toFixed(1)) + "× de lo que da el cupo" : "",
        t.se_acaba ? "así como venís se acaba el " + t.se_acaba : "",
        t.resetea ? "resetea " + t.resetea : "",
      ].filter(Boolean).join("\n");
      return '<div class="u-lin" title="' + esc(tip) + '">' +
        '<span class="u-eti">' + esc(t.nombre === "sesión 5 h" ? "5 h" : t.nombre) + '</span>' +
        '<span class="u-tubo"><span class="u-fill ' + clase + '" style="width:' +
          Math.min(100, t.pct) + '%"></span>' +
        (t.esperado != null ? '<span class="u-marca" style="left:' +
          Math.min(100, t.esperado) + '%"></span>' : "") +
        '</span><span class="u-pct ' + (clase === "rojo" ? "rojo" : "") + '">' +
        Math.round(t.pct) + '%</span></div>';
    }).join("");
}
async function pintarUso(){
  try{
    const r = await fetch("/api/uso");
    pintarUsoHTML(await r.json());
  }catch(err){ pintarUsoHTML(null); }
}

(async () => {
  await refrescar();
  // arranque: si no hay ninguna pestaña viva, abrir una en el proyecto principal
  if(!estado.tabs.some(t => t.viva)){
    const pref = estado.proyectos[0];
    // con área declarada (Cacho): la pestaña del arranque no nace pelada
    if(pref) await nueva(pref.cwd, estado.area_defecto || "");
  } else {
    abrirTab(estado.tabs.filter(t => t.viva).slice(-1)[0].id);
  }
  setInterval(() => { refrescar(); pintarVer(); pintarBandeja(); }, 4000);
  pintarUso();
  setInterval(pintarUso, 5 * 60 * 1000);
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
    # se pregunta acá y no en la primera pestaña, para que abrir una no espere al
    # `claude --help` (y para que el aviso de CLI viejo salga al arrancar, no después)
    threading.Thread(target=_soporta_session_id, daemon=True).start()
    # El SIGTERM de launchd (`kickstart -k`, el reinicio) mataba el proceso EN SECO: el
    # `finally` de abajo nunca corría y las pestañas morían con el pty, sin que nadie las
    # anotara. Ahora el SIGTERM pide un cierre ordenado (shutdown() tiene que llamarse desde
    # OTRO hilo que serve_forever, si no se traba) y el cierre las anota antes de matarlas.
    signal.signal(signal.SIGTERM,
                  lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    _restaurar_pestanas()
    print(f"Cacho en http://{BIND}:{PORT}  (Ctrl-C para cortar)")
    try:
        server.serve_forever()
    finally:
        # PRIMERO anotar, DESPUÉS matar: matar espera hasta 1 s por pestaña y launchd
        # manda SIGKILL a los 20 s — si hubiera 30 pestañas, la anotación tiene que estar.
        _guardar_pestanas_vivas()
        with TABS_LOCK:
            for t in TABS.values():
                t.matar()

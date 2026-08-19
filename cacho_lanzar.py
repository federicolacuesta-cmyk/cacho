#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cacho_lanzar.py — abre una TAREA como pestaña DENTRO de Cacho.

La idea: todo lo que corre solo (autopilots, auditorías, tareas programadas) se mira
ADENTRO de Cacho — pestaña en la barra lateral — no en ventanas de Terminal sueltas.

Qué hace:
  1) Si el server de Cacho (127.0.0.1:8811) está caído, lo levanta (las pestañas viven
     en el SERVER; la ventana es solo la vista, así que una pestaña creada a las 4 AM
     te queda esperando cuando abrís Cacho a la mañana).
  2) Crea una pestaña nueva en el proyecto (POST /api/term/new, con el token de
     ~/.cacho_token). La pestaña arranca sola una sesión interactiva de `claude`.
  3) Espera que `claude` levante y le tipea el prompt.

Por qué usa un TOKEN y no el PIN en cada request: el login de Cacho está frenado —8 PIN
equivocados y esa IP queda afuera hasta una hora— y con Tailscale TODOS los dispositivos
llegan con la misma IP que localhost. O sea que un sondeo desde el tailnet, o un lanzador
con el PIN viejo, dejaba fuera de servicio a las tareas automáticas: contestaba 429 aun con
el PIN correcto. El token no se adivina, así que su camino no se frena nunca. Se cambia el
PIN por token UNA vez y queda guardado en ~/.cacho_token (0600): un solo lugar de los 20
dispositivos que Cacho recuerda.

Uso:
  python3 cacho_lanzar.py "<prompt para la sesión>"          # proyecto = dir actual
  python3 cacho_lanzar.py "<prompt>" /ruta/al/proyecto        # o el que le pases
Salida: exit 0 si la pestaña quedó creada con el prompt tipeado; exit != 0 si no se pudo
(el caller decide el fallback, p. ej. una ventana de Terminal común).
"""
from __future__ import annotations
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import quote, urlencode

PUERTO = os.environ.get("CACHO_PORT", "8811")
BASE = "http://127.0.0.1:" + PUERTO
AQUI = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(AQUI, "serve_sesiones.py")
PIN_PATH = os.path.expanduser("~/.cacho_pin")
TOKEN_PATH = os.path.expanduser("~/.cacho_token")
LOG = os.path.expanduser("~/Library/Logs/cacho-server.log")
ESPERA_CLAUDE_S = 30  # el tab tipea `claude` solo a los ~0.9s; esto cubre el arranque del CLI


def _get(path, timeout=3):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read()


class _SinRedirect(urllib.request.HTTPRedirectHandler):
    """El login contesta 303 y la cookie viene EN ESE 303. Si se sigue el redirect,
    urllib se trae la página entera y la cabecera que importa se pierde de vista."""

    def redirect_request(self, *a, **k):
        return None


def _guardar_token(tok):
    # 0600 desde el os.open: este archivo es la llave de una terminal con control
    # total de la máquina, igual que ~/.cacho_pin.
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")


def _token(renovar=False):
    """Token de sesión de Cacho, del archivo o pidiéndolo con el PIN una sola vez."""
    if not renovar:
        try:
            with open(TOKEN_PATH) as fh:
                guardado = fh.read().strip()
            if guardado:
                return guardado
        except OSError:
            pass
    datos = urlencode({"pin": pin()}).encode()
    op = urllib.request.build_opener(_SinRedirect)
    try:
        r = op.open(urllib.request.Request(BASE + "/pin", data=datos, method="POST"),
                    timeout=10)
        cabeceras = r.headers
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise SystemExit(f"Cacho rechazó el PIN al pedir token: HTTP {e.code}")
        cabeceras = e.headers
    for c in cabeceras.get_all("Set-Cookie") or []:
        if c.startswith("cacho_sesion="):
            tok = c.split("=", 1)[1].split(";")[0]
            _guardar_token(tok)
            return tok
    raise SystemExit("Cacho no entregó token de sesión (¿el PIN de ~/.cacho_pin es el bueno?)")


def _post(path, body=b"", timeout=10):
    # Dos intentos: si el token guardado ya no vale (se borró sesiones_web.json, o
    # se cayó de la lista de 20 dispositivos), se cambia por uno nuevo y se reintenta.
    for intento in (1, 2):
        req = urllib.request.Request(BASE + path, data=body, method="POST")
        req.add_header("Cookie", "cacho_sesion=" + _token(renovar=(intento == 2)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and intento == 1:
                continue
            raise


def ping(silencioso=False):
    try:
        _get("/api/ping")
        return True
    except Exception as e:
        # "no contesta" es la respuesta normal cuando el server no arrancó todavía:
        # el sondeo de levantar_server pregunta 30 veces y no tiene que escupir 30 avisos
        if not silencioso:
            print(f"(ping a Cacho sin respuesta: {str(e)[:80]})", file=sys.stderr)
        return False


def levantar_server():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "ab") as fh:
        subprocess.Popen(["/usr/bin/python3", SERVER], stdout=fh, stderr=fh,
                         start_new_session=True, cwd=AQUI)
    for _ in range(30):
        if ping(silencioso=True):
            return True
        time.sleep(0.5)
    print("(el server de Cacho no contestó el ping tras 15 s de arranque)", file=sys.stderr)
    return False


def pin():
    # el server lo genera solo al arrancar; si recién lo levantamos puede tardar un toque
    for _ in range(10):
        try:
            p = open(PIN_PATH).read().strip()
            if p:
                return p
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("Sin PIN de Cacho (~/.cacho_pin no apareció)")


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        raise SystemExit('Uso: cacho_lanzar.py "<prompt>" [/ruta/al/proyecto]')
    prompt = sys.argv[1].strip()
    proyecto = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.getcwd()

    if not ping(silencioso=True) and not levantar_server():
        raise SystemExit("El server de Cacho no levanta (ver %s)" % LOG)

    r = _post("/api/term/new?cwd=%s" % quote(proyecto))
    tid = r.get("id")
    if not tid:
        raise SystemExit("Cacho no devolvió id de pestaña: %r" % r)
    print(f"pestaña {tid} creada; esperando que levante claude ({ESPERA_CLAUDE_S}s)…")
    time.sleep(ESPERA_CLAUDE_S)

    # pegar el prompt con BRACKETED PASTE (entra atómico: tipearlo rápido en el TUI
    # de claude COME espacios) y el Enter va en un write aparte, un toque después,
    # para que el TUI ya haya digerido el texto.
    texto = prompt.replace("\n", " ").strip()
    d = base64.b64encode(("\x1b[200~" + texto + "\x1b[201~").encode()).decode()
    rr = _post("/api/term/%s/input" % tid, body=json.dumps({"d": d}).encode())
    if not rr.get("ok"):
        raise SystemExit("No pude tipear el prompt en la pestaña: %r" % rr)
    time.sleep(1.5)
    rr = _post("/api/term/%s/input" % tid,
               body=json.dumps({"d": base64.b64encode(b"\r").decode()}).encode())
    if not rr.get("ok"):
        raise SystemExit("No pude mandar el Enter en la pestaña: %r" % rr)
    print(f"OK — tarea corriendo en Cacho (pestaña {tid}).")


if __name__ == "__main__":
    main()

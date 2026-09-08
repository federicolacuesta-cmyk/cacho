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
  python3 cacho_lanzar.py "<prompt para la sesión>" [area]    # proyecto = dir actual
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
            raise SystemExit(f"Cacho rechazó el PIN al pedir token: HTTP {e.code}") from e
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


# Cuánto se espera cuando Cacho está lleno. Corto a propósito: si en un minuto nadie cerró
# una pestaña, es que de verdad está lleno y seguir esperando no cambia nada.
REINTENTOS_LLENO = 3
ESPERA_LLENO_S = 20


def crear_pestana(modelo="", area=""):
    """Levanta Cacho si hace falta, crea la pestaña y devuelve su id.

    `modelo`: pedido explícito para esta pestaña (p. ej. una cita premium que exige
    Fable). Vacío = el default de la casa (~/.cacho_modelo, hoy Opus).

    `area`: de quién es esta corrida (`areas.py`: cacho/carla/jaime/eterna/waldemar/xara).
    Vale la pena declararla SIEMPRE que el que llama lo sepa, y acá lo sabe casi siempre:
    la rutina de novedades de Google es de Jaime y el vigía de chats es de Carla, no hay
    nada que deducir. Sin esto, Cacho le adivinaba el área contando palabras del prompt
    y lo que no reconocía caía en Cacho por descarte — o sea que la tira mostraba de
    Cacho trabajo que era de otro, y sin ningún error a la vista. Vacío = que la
    clasifique la máquina, como siempre (queda para el que de verdad no sepa).

    Está separado de `tipear()` porque hay dos ritmos distintos: crear la pestaña
    es instantáneo, pero después hay que esperar ~30 s a que el CLI de `claude`
    levante para poder escribirle. Quien abre una tarea desde una PANTALLA (el
    panel de pendientes del monitor) necesita contestarle al navegador ya mismo y
    dejar el tipeo para un hilo de fondo; si esperara los 30 s, el clic parece
    colgado y el usuario lo toca de nuevo.
    """
    if not ping(silencioso=True) and not levantar_server():
        raise RuntimeError("El server de Cacho no levanta (ver %s)" % LOG)
    ruta = "/api/term/new?cwd=%s" % quote(os.getcwd())
    if modelo:
        ruta += "&modelo=%s" % quote(modelo)
    if area:
        ruta += "&area=%s" % quote(area)
    # El 429 ("ya hay N pestañas abiertas") es TRANSITORIO por definición: alguien cierra
    # una y hay lugar. Hasta el 29-ago-2026 se propagaba como cualquier otro error y la
    # rutina moría con un traceback de urllib — el trabajo NO se hacía y nadie se enteraba
    # hasta que `chequear_launchd` lo cantaba a las 11:40, sin decir la causa. Pasó ese
    # mismo día con TRES jobs: enriquecer-supervisores (06:05), monitor-base-local (06:08)
    # y vigia-cotizador. Se arregla acá, en el lanzador, y no en cada uno de los 14
    # llamadores. El reintento va SÓLO en esta llamada, nunca en `_post`: el otro 429 del
    # server es el castigo por PIN equivocado, y ahí insistir es exactamente lo contrario
    # de lo que hay que hacer.
    ultimo = None
    for intento in range(1, REINTENTOS_LLENO + 1):
        try:
            r = _post(ruta)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            ultimo = e
            if intento < REINTENTOS_LLENO:
                print("(Cacho lleno, reintento %d/%d en %ds)"
                      % (intento, REINTENTOS_LLENO, ESPERA_LLENO_S), file=sys.stderr)
                time.sleep(ESPERA_LLENO_S)
    else:
        # en criollo: el que lee esto es el que mira el log de un job que no corrió
        raise RuntimeError(
            "Cacho está lleno de pestañas y no se liberó ninguna en %d s: la rutina no "
            "pudo abrir la suya. Cerrá pestañas con la ✕ y volvé a correrla. (%s)"
            % (REINTENTOS_LLENO * ESPERA_LLENO_S, ultimo))
    tid = r.get("id")
    if not tid:
        raise RuntimeError("Cacho no devolvió id de pestaña: %r" % r)
    return tid


def tipear(tid, prompt, esperar=ESPERA_CLAUDE_S):
    """Le pega el prompt a la pestaña `tid` y manda el Enter."""
    time.sleep(esperar)
    # pegar el prompt con BRACKETED PASTE (entra atómico: tipearlo rápido en el TUI
    # de claude COME espacios) y el Enter va en un write aparte, un toque después,
    # para que el TUI ya haya digerido el texto.
    texto = prompt.replace("\n", " ").strip()
    d = base64.b64encode(("\x1b[200~" + texto + "\x1b[201~").encode()).decode()
    rr = _post("/api/term/%s/input" % tid, body=json.dumps({"d": d}).encode())
    if not rr.get("ok"):
        raise RuntimeError("No pude tipear el prompt en la pestaña: %r" % rr)
    time.sleep(1.5)
    rr = _post("/api/term/%s/input" % tid,
               body=json.dumps({"d": base64.b64encode(b"\r").decode()}).encode())
    if not rr.get("ok"):
        raise RuntimeError("No pude mandar el Enter en la pestaña: %r" % rr)
    return True


def con_memoria_del_area(prompt, area=""):
    """Antepone al prompt la lectura de la memoria del agente, si existe.

    Desde el 3-set-2026 la memoria está partida por agente: MEMORY.md (precargado) lleva lo
    transversal y las 🚨, y lo ⭐/⚠️ de cada área vive en .claude/memory/MEMORY-<area>.md.
    Una pestaña que ya declara su área sabe qué índice le toca: se lo pide en la primera
    línea del prompt, que es el único mecanismo que el harness respeta (no hay hook de
    "precargá esto también"). Sin área, o sin archivo, el prompt sale como vino.
    """
    if not area:
        return prompt
    # Nivel 2 de ARQUITECTURA.md (5-set-2026): la memoria del área (⭐/⚠️) y la SKILL del área
    # (la crónica de sus circuitos, que salió de CLAUDE.md para que el nivel 1 entre en su tope).
    # Van las dos en la primera línea: es el único mecanismo de precarga que el harness respeta.
    # Sin área, la skill se dispara sola por su description; con área, no se deja al azar.
    # Una sola puerta decide qué lee cada área: la misma que usa el server de Cacho para la
    # pestaña de la interfaz (`agente_arranque`). Acá va antepuesto al prompt porque la
    # pestaña automática SÍ tiene prompt; allá va por system prompt porque no lo tiene.
    import agente_arranque
    return agente_arranque.linea_de_prompt(area) + prompt


def lanzar(prompt, area=""):
    """Crea la pestaña y le deja el prompt corriendo. Devuelve el id de pestaña."""
    tid = crear_pestana(area=area)
    tipear(tid, con_memoria_del_area(prompt, area))
    return tid


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        raise SystemExit('Uso: cacho_lanzar.py "<prompt>" [area]')
    # El área va como 2º argumento POSICIONAL y opcional: los seis llamadores de hoy le
    # pasan un solo argumento y tienen que seguir andando sin tocarlos. Un área que el
    # server no conoce lo hace fallar con 400 en vez de crear la pestaña — es lo que se
    # quiere: un typo en un plist tiene que gritar, no clasificar mal en silencio.
    area = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    try:
        tid = crear_pestana(area=area)
        print(f"pestaña {tid} creada; esperando que levante claude ({ESPERA_CLAUDE_S}s)…")
        tipear(tid, con_memoria_del_area(sys.argv[1].strip(), area))
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    print(f"OK — tarea corriendo en Cacho (pestaña {tid}).")


if __name__ == "__main__":
    main()

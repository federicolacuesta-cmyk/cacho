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
  python3 cacho_lanzar.py "<prompt para la sesión>" <area>          # el área es obligatoria
  python3 cacho_lanzar.py --conector Notion "<prompt>" <area>       # un conector MCP extra
Salida: exit 0 si la pestaña quedó creada Y EL CLI TOMÓ EL PROMPT (se verifica donde el CLI
lo registra — el transcript de claude o ~/.codex/history.jsonl de Codex; «lo escribí en el
pty» no alcanza); exit != 0 si no se pudo (el caller decide el fallback, p. ej. una ventana
de Terminal común).
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

# El puerto se puede correr (CACHO_PORT, el mismo que lee el server): así una prueba habla
# con un server de prueba y no con el de producción.
PUERTO = os.environ.get("CACHO_PORT", "8811")
BASE = "http://127.0.0.1:" + PUERTO
AQUI = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(AQUI, "serve_sesiones.py")
PIN_PATH = os.path.expanduser("~/.cacho_pin")
TOKEN_PATH = os.path.expanduser("~/.cacho_token")
LOG = os.path.expanduser("~/Library/Logs/cacho-server.log")
ESPERA_CLAUDE_S = 30  # el tab tipea `claude` solo a los ~0.9s; esto cubre el arranque del CLI
# Dónde registra cada CLI lo que el usuario le mandó: es la única evidencia de que el prompt
# ENTRÓ (un pty acepta cualquier byte, también los que se come un diálogo).
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")      # claude: <slug>/<sid>.jsonl
CODEX_HISTORY = os.path.expanduser("~/.codex/history.jsonl")  # codex: {"session_id","ts","text"}
ESPERA_LLEGADA_S = 25   # cuánto se espera ver el prompt registrado después del Enter
CLAVE_PANEL = "cacho-lanzar"


def quien_lanza():
    """`script:<nombre>` — QUÉ script está abriendo la pestaña, para que Cacho lo anote al
    nacer (21-set-2026, el usuario: «se disparan sesiones solas»). Si este módulo se importó, el
    script es el programa principal (`sys.argv[0]`); si se corrió como comando, es el
    proceso PADRE (el .py o el job de launchd que lo llamó). Nunca lanza: es trazabilidad."""
    nombre = ""
    try:
        principal = os.path.basename(sys.argv[0] or "")
        if principal.endswith((".py", ".sh")) and principal != os.path.basename(__file__):
            nombre = principal
        else:
            padre = subprocess.run(["ps", "-o", "command=", "-p", str(os.getppid())],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            partes = [x for x in padre.split() if not x.startswith("-")]
            # «python3 tools/x.py …» → x.py; «/bin/zsh -c …» → zsh
            for x in partes[1:] or partes[:1]:
                nombre = os.path.basename(x)
                if nombre.endswith((".py", ".sh")):
                    break
            if nombre == "launchd":
                # El padre es launchd: el JOB es la identidad, no el supervisor (Policía
                # 21-set-2026: enriquecer-diario, blog-salud-visual, auditoria-mensual y
                # enriquecer-mensual ejecutan este archivo y los cuatro quedaban «launchd»).
                # launchd no pone el label en el entorno (XPC_SERVICE_NAME=0, probado), pero
                # `launchctl list` lista PID y label de lo que está corriendo.
                nombre = _job_launchd() or nombre
    except Exception:
        nombre = ""
    return "script:" + (nombre or "?")[:60]


def _job_launchd():
    """El label del job de launchd que corre este proceso (o su padre), por `launchctl list`."""
    pids = {str(os.getpid()), str(os.getppid())}
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    for lin in out.splitlines():
        partes = lin.split("\t")
        if len(partes) >= 3 and partes[0] in pids:
            return partes[2].strip()
    return ""


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


def _get_auth(path, timeout=10):
    """GET con la cookie de sesión (mismo par de intentos que `_post`)."""
    for intento in (1, 2):
        req = urllib.request.Request(BASE + path)
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


def exigir_area(area):
    """El área limpia, o ValueError. La vara es la del server (`areas.valida`): así el error
    sale ACÁ, con el nombre del script en el traceback del job, y no como un 400 pelado."""
    a = (area or "").strip()
    if not a:
        raise ValueError("cacho_lanzar: falta el ÁREA de la pestaña (2º argumento: cacho, carla, "
                         "jaime, eterna, waldemar, xara, ferguson…). Desde el 18-set-2026 es "
                         "obligatoria: el que abre la pestaña sabe de quién es el trabajo.")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import areas
    v = areas.valida(a)
    if not v:
        raise ValueError("cacho_lanzar: área desconocida %r (las válidas: %s)"
                         % (a[:40], ", ".join(sorted(areas.POR_CLAVE))))
    return v


def crear_pestana(modelo="", area="", conectores=()):
    """Levanta Cacho si hace falta, crea la pestaña y devuelve su id.

    `modelo`: pedido explícito para esta pestaña (p. ej. una cita premium que exige
    Fable). Vacío = el default de la casa (~/.cacho_modelo, hoy Opus).

    `area`: de quién es esta corrida (`areas.py`: cacho/carla/jaime/eterna/waldemar/xara/ferguson).
    **OBLIGATORIA desde el 18-set-2026** (plan «optimización del contexto», tanda 1, D9):
    el que llama lo sabe siempre —la rutina de novedades de Google es de Jaime y el vigía
    de chats es de Carla, no hay nada que deducir— y lo que nace sin área no se puede
    medir ni optimizar (la semana del 14-set: 64 sesiones, el 13% del cupo, «sin área»).
    Hasta hoy, vacío = «que la clasifique la máquina» contando palabras del prompt, y lo
    que no reconocía caía en Cacho por descarte: la tira mostraba de Cacho trabajo que era
    de otro, sin ningún error a la vista. Ahora sin área se TIRA acá (ValueError, antes de
    tocar el server) y el server además contesta 400: un typo en un plist grita, no
    clasifica mal en silencio. El clasificador queda sólo para las charlas viejas.

    Está separado de `tipear()` porque hay dos ritmos distintos: crear la pestaña
    es instantáneo, pero después hay que esperar ~30 s a que el CLI de `claude`
    levante para poder escribirle. Quien abre una tarea desde una PANTALLA (el
    panel de pendientes del monitor) necesita contestarle al navegador ya mismo y
    dejar el tipeo para un hilo de fondo; si esperara los 30 s, el clic parece
    colgado y el usuario lo toca de nuevo.
    """
    area = exigir_area(area)
    if not ping(silencioso=True) and not levantar_server():
        raise RuntimeError("El server de Cacho no levanta (ver %s)" % LOG)
    ruta = "/api/term/new?cwd=%s&area=%s&por=%s" % (quote(os.getcwd()), quote(area), quote(quien_lanza()))
    if modelo:
        ruta += "&modelo=%s" % quote(modelo)
    if conectores:
        # conectores MCP EXTRA para esta pestaña, por fuera de la tabla de su área
        # (inputs/conectores_areas.json, 18-set-2026): permiso puntual, el server lo deja en su log
        ruta += "&conectores=%s" % quote(",".join(conectores))
        print("conectores extra para esta pestaña (%s): %s" % (area, ", ".join(conectores)), file=sys.stderr)
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


def tipear(tid, prompt, esperar=ESPERA_CLAUDE_S, verificar=True):
    """Le pega el prompt a la pestaña `tid`, manda el Enter y VERIFICA que el CLI lo tomó
    (`confirmar_llegada`; `verificar=False` sólo para quien ya verifica por su cuenta)."""
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
    desde = time.time()
    rr = _post("/api/term/%s/input" % tid,
               body=json.dumps({"d": base64.b64encode(b"\r").decode()}).encode())
    if not rr.get("ok"):
        raise RuntimeError("No pude mandar el Enter en la pestaña: %r" % rr)
    if verificar:
        confirmar_llegada(tid, texto, desde)
    return True


# ── El candado: «OK» es «el CLI lo tomó», no «lo escribí en el pty» (13-set-2026) ─────────
# RAÍZ del 12-set: la pestaña nueva de Gpto arrancó en el diálogo «SessionStart hooks — press t
# to trust» (el server vivo corría la orden vieja, sin --dangerously-bypass-hook-trust), el
# paste se lo comió el diálogo y esto contestó «OK — tarea corriendo». El pty acepta cualquier
# byte; la única evidencia de que el prompt ENTRÓ es que el CLI lo haya registrado donde
# registra lo que le manda el usuario: claude en su transcript (`~/.claude/projects/<slug>/
# <sid>.jsonl`, línea `type: user`), Codex en `~/.codex/history.jsonl`. Se mira en LOS DOS
# (así no hay que saber acá qué motor corre cada área) y si en ESPERA_LLEGADA_S no aparece,
# tira Y deja la alerta en el panel: la pestaña quedó abierta con el pedido perdido, y eso no
# se resuelve solo en el día — el usuario lo ve, retipea, y cierra con ✓.

def _huella(texto, largo=60):
    """Los primeros `largo` caracteres con el blanco normalizado: lo que se busca."""
    return " ".join(str(texto).split())[:largo]


def _textos(obj):
    """Todas las cadenas de un JSON (el `content` de claude es str o lista de bloques)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _textos(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _textos(v)


def _lineas_json(ruta, cola=0):
    """Un .jsonl parseado (lo que no es JSON se saltea); con `cola` sólo sus últimos bytes.

    El transcript de claude se lee ENTERO: el pedido es la primera línea `user` y al
    confirmar el archivo tiene unos KB. El history de Codex es de toda la vida (crece sin
    tope) y lo que importa está al final: ése va con cola.
    """
    try:
        with open(ruta, "rb") as fh:
            if cola:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - cola))
            crudo = fh.read().decode("utf-8", "replace")
    except OSError:
        return
    for linea in crudo.splitlines():
        try:
            yield json.loads(linea)
        except ValueError:
            continue


def _llego_a_claude(sid, huella):
    """¿El transcript de la sesión `sid` tiene una línea `user` con la huella?"""
    if not sid:
        return False
    try:
        carpetas = os.listdir(PROJECTS_DIR)
    except OSError:
        return False
    for carpeta in carpetas:
        ruta = os.path.join(PROJECTS_DIR, carpeta, sid + ".jsonl")
        if not os.path.isfile(ruta):
            continue
        for d in _lineas_json(ruta):
            if d.get("type") != "user":
                continue
            if any(huella in _huella(t, 10**6) for t in _textos(d.get("message"))):
                return True
    return False


def _llego_a_codex(huella, desde):
    """¿history.jsonl de Codex tiene, desde `desde`, un pedido con la huella?"""
    for d in _lineas_json(CODEX_HISTORY, cola=200_000):
        if float(d.get("ts") or 0) >= desde - 2 and huella in _huella(d.get("text", ""), 10**6):
            return True
    return False


def _pestana(tid):
    """La pestaña `tid` según el server (`sid` = su charla, `area`)."""
    est = _get_auth("/api/estado")
    for t in est.get("tabs") or []:
        if t.get("id") == tid:
            return t
    raise RuntimeError("Cacho no tiene la pestaña %s (¿se cerró mientras tipeaba?)" % tid)


def confirmar_llegada(tid, texto, desde, esperar=ESPERA_LLEGADA_S):
    """Espera hasta `esperar` s a ver el prompt registrado por el CLI de la pestaña.

    Si aparece, devuelve dónde ("claude" | "codex"). Si no, deja la alerta en el panel y TIRA.
    """
    huella = _huella(texto)
    if not huella:
        raise RuntimeError("prompt vacío: no hay nada que confirmar")
    p = _pestana(tid)
    sid, area = p.get("sid") or "", p.get("area") or ""
    limite = time.time() + esperar
    while True:
        if _llego_a_claude(sid, huella):
            return "claude"
        if _llego_a_codex(huella, desde):
            return "codex"
        if time.time() >= limite:
            break
        time.sleep(1)
        # el sid de una pestaña de Codex lo vincula el server en vida: releer por si cambió
        try:
            p = _pestana(tid)
            sid = p.get("sid") or sid
        except Exception:  # noqa: BLE001 — si el server no contesta, se sigue con lo que había
            pass
    # «QUEDÓ EN LA COLA» NO ES UN FALLO (22-set-2026). Una pestaña que está TRABAJANDO no
    # registra el prompt en su transcript hasta que termina el turno: el CLI lo tiene en la
    # cola de entrada y lo toma después. Los 25 s alcanzan para una pestaña quieta y no para
    # una ocupada, así que esto gritaba «NO llegó» sobre pedidos que habían llegado —pasó con
    # el complemento del encargo de Waldemar (c1b4c3b4), verificado a mano en el transcript—.
    # Antes de dar un pedido por perdido se le pregunta al server si la pestaña está ocupada.
    try:
        if (_pestana(tid).get("estado") or "") == "trabajando":
            print("• La pestaña %s está trabajando: el pedido le queda en la cola y lo toma al "
                  "terminar el turno." % tid, file=sys.stderr)
            return "cola"
    except Exception:  # noqa: BLE001 — si el server no contesta, seguimos al camino de fallo
        pass
    detalle = ("Pestaña %s (%s). Ni el transcript de claude (%s) ni ~/.codex/history.jsonl "
               "registraron el pedido en %d s después del Enter. Casi siempre es un diálogo "
               "del CLI (trust hook, permisos, «press t») que se comió el paste.\n"
               "Pedido: «%s…»" % (tid, area or "sin área", sid or "sin sid", int(esperar),
                                   _huella(texto, 160)))
    try:
        if PROJ not in sys.path:      # casa_alertas vive en la raíz; sys.path[0] es tools/
            sys.path.insert(0, PROJ)
        import casa_alertas as al
        al.avisar_panel(CLAVE_PANEL, "Un pedido a Cacho NO llegó a su pestaña", detalle,
                        nivel="alerta", origen="tools/cacho_lanzar.py",
                        accion="Abrí la pestaña %s en Cacho, destrabá el diálogo y retipeá el "
                               "pedido (si el que la lanzó ya la cerró, relanzalo); después ✓ "
                               "acá." % tid,
                        asunto="perdido:%s" % tid, para="casa")
    except Exception as e:  # noqa: BLE001 — el panel no puede tapar el error de verdad
        print("(no pude dejar la alerta en el panel: %r)" % e, file=sys.stderr)
    raise RuntimeError("El prompt NO llegó a la pestaña %s: %s" % (tid, detalle.split("\n")[0]))



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


ENCARGOS = os.path.join(PROJ, "logs", "sesiones", "encargos")
ENCARGO_VENCE_H = 6      # tomado hace más de esto: la pestaña quedó abierta pero el encargo se re-entrega


def _tabs_vivas():
    """Los ids de pestaña que el server tiene AHORA, o None si no pude preguntarle.

    None no es lo mismo que «ninguna» (regla de la casa: «no lo vi» nunca es un valor).
    """
    try:
        est = _get_auth("/api/estado")
        return {t.get("id") for t in (est.get("tabs") or [])}
    except Exception:                                # noqa: BLE001
        return None


def quien_tiene(encargo):
    """El id de la pestaña VIVA que ya está con este encargo, o '' si no lo tiene nadie.

    RAÍZ (22-set-2026): el vigía de memoria entregó el MISMO mantenimiento a dos pestañas, que
    lo hicieron en paralelo y casi se pisan los commits. El prompt le pedía al agente «no abras
    otra pestaña», pero nada frenaba al VIGÍA, que vuelve a entregar en cuanto el cuadro cambia
    (y el cuadro cambia justo cuando el que está trabajando arregla o agrega un hallazgo).

    Si no se puede preguntar al server, devuelve '' A PROPÓSITO: duplicar un encargo es molesto,
    perderlo es peor.
    """
    try:
        with open(os.path.join(ENCARGOS, "%s.json" % encargo), encoding="utf-8") as fh:
            reg = json.load(fh)
    except (OSError, ValueError):
        return ""
    if time.time() - reg.get("cuando", 0) > ENCARGO_VENCE_H * 3600:
        return ""
    vivas = _tabs_vivas()
    if vivas is None or reg.get("tid") not in vivas:
        return ""
    return reg["tid"]


def _tomar(encargo, tid):
    try:
        os.makedirs(ENCARGOS, exist_ok=True)
        with open(os.path.join(ENCARGOS, "%s.json" % encargo), "w", encoding="utf-8") as fh:
            json.dump({"tid": tid, "cuando": time.time(), "por": quien_lanza()}, fh)
    except OSError as e:                             # noqa: BLE001
        print("⚠️ no pude anotar el encargo %s: %r" % (encargo, e), file=sys.stderr)


def lanzar(prompt, area="", conectores=(), encargo=""):
    """Crea la pestaña y le deja el prompt corriendo. Devuelve el id de pestaña.
    `area` es obligatoria (ver `crear_pestana`): sin ella, ValueError antes de tocar el server.
    `conectores`: extra puntual de conectores MCP por fuera de la tabla del área.
    `encargo`: clave del trabajo. Si una pestaña VIVA ya lo tiene, NO se abre otra y se devuelve
    la que lo tiene (ver `quien_tiene`)."""
    area = exigir_area(area)
    if encargo:
        ya = quien_tiene(encargo)
        if ya:
            return ya
    tid = crear_pestana(area=area, conectores=conectores)
    tipear(tid, con_memoria_del_area(prompt, area))
    if encargo:
        _tomar(encargo, tid)
    return tid


def main():
    argv = sys.argv[1:]
    # `--en <tid>`: el pedido va a una pestaña que YA está abierta, no a una nueva (14-set-2026,
    # el ➤ del tablero cuando el dueño tiene su pestaña viva). Sin espera de arranque y sin
    # la línea de memoria del área: esa pestaña ya la tiene. La verificación de llegada es la
    # misma: «OK» sigue siendo «el CLI lo tomó».
    en = ""
    # `--conector <nombre>` (repetible, antes del prompt): un conector MCP extra para esta
    # pestaña, por fuera de la tabla de su área (18-set-2026). Queda en el log del server.
    conectores = []
    # `--encargo <clave>`: si una pestaña VIVA ya tiene este mismo trabajo, NO se abre otra
    # (22-set-2026: el vigía de memoria entregó el mantenimiento a dos pestañas a la vez).
    encargo = ""
    while argv[:2] and argv[0] == "--encargo":
        if not argv[1].strip():
            raise SystemExit('Uso: cacho_lanzar.py [--encargo <clave>] "<prompt>" <area>')
        encargo, argv = argv[1].strip(), argv[2:]
    while argv[:1] == ["--conector"]:
        if len(argv) < 2 or not argv[1].strip():
            raise SystemExit('Uso: cacho_lanzar.py [--conector <nombre>]… "<prompt>" <area>')
        conectores.append(argv[1].strip())
        argv = argv[2:]
    if argv[:1] == ["--en"]:
        if len(argv) < 2 or not argv[1].strip():
            raise SystemExit('Uso: cacho_lanzar.py --en <pestaña> "<prompt>" [area]')
        en, argv = argv[1].strip(), argv[2:]
    if not argv or not argv[0].strip():
        raise SystemExit('Uso: cacho_lanzar.py [--encargo <clave>] [--en <pestaña>] "<prompt>" <area>')
    # Un flag donde va el prompt (`--help`, `-h`, un `--area` inventado) NO es un pedido:
    # sin esto, `cacho_lanzar.py --help` abría una pestaña de Opus con «--help» como
    # encargo (pasó tres veces, 12/13/16-set-2026: sesiones 1f42f733, 6ad650ae y una más).
    # El prompt es texto de una persona o de una rutina; nunca empieza con guion.
    if argv[0].lstrip().startswith("-"):
        raise SystemExit('Uso: cacho_lanzar.py [--encargo <clave>] [--en <pestaña>] "<prompt>" <area>\n'
                         "El prompt no puede empezar con «-» (recibí %r)." % argv[0][:40])
    if en:
        try:
            tipear(en, argv[0].strip(), esperar=0)
        except RuntimeError as e:
            raise SystemExit(str(e)) from e
        print(f"OK — pedido entregado en la pestaña {en} que ya estaba abierta.")
        return
    sys.argv = [sys.argv[0]] + argv
    # El área va como 2º argumento POSICIONAL y es OBLIGATORIO (18-set-2026; antes opcional
    # «para no tocar a los seis llamadores», y cuatro plists y un script seguían sin
    # declararla dos meses después). Un área que el server no conoce falla acá con el
    # nombre del script a la vista — un typo en un plist tiene que gritar, no clasificar mal
    # en silencio.
    if len(sys.argv) < 3 or not sys.argv[2].strip():
        raise SystemExit('Uso: cacho_lanzar.py [--encargo <clave>] [--en <pestaña>] "<prompt>" <area>\n'
                         "Falta el ÁREA (cacho, carla, jaime, eterna, waldemar, xara, ferguson…): "
                         "desde el 18-set-2026 es obligatoria, el que abre la pestaña sabe de "
                         "quién es el trabajo.")
    try:
        area = exigir_area(sys.argv[2])
        if encargo:
            ya = quien_tiene(encargo)
            if ya:
                print(f"OK — el encargo «{encargo}» ya lo tiene la pestaña {ya}; no abro otra.")
                return
        tid = crear_pestana(area=area, conectores=tuple(conectores))
        print(f"pestaña {tid} creada; esperando que levante claude ({ESPERA_CLAUDE_S}s)…")
        tipear(tid, con_memoria_del_area(sys.argv[1].strip(), area))
        if encargo:
            _tomar(encargo, tid)
    except (RuntimeError, ValueError) as e:
        raise SystemExit(str(e)) from e
    print(f"OK — tarea corriendo en Cacho (pestaña {tid}).")


if __name__ == "__main__":
    main()

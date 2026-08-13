<p align="center"><img src="static/cacho.png" width="120" alt="Cacho"></p>

# Cacho 🐕

**Gestor de sesiones de Claude Code para macOS.** Todas tus terminales de `claude` en una
sola ventana — qué hace cada una, cuál está trabajando, cuál te espera — y también desde
el teléfono.

*(English: a local session manager for Claude Code on macOS — one window with every
`claude` terminal, grouped by project, resumable, reachable from your phone over
Tailscale. Spanish-first project, the code comments are in Spanish.)*

## El problema que resuelve

Si usás Claude Code en serio, esto te suena:

- Terminás con **seis ventanas de Terminal abiertas**, cada una con un `claude` adentro,
  y ninguna te dice cuál está trabajando, cuál terminó y cuál está esperando que le
  contestes desde hace dos horas.
- Las **tareas automáticas** (cron, launchd, autopilots) corren en ventanas sueltas que
  aparecen a cualquier hora y se pierden entre las demás.
- Una charla que terminó **cuesta retomarla**: hay que acordarse del proyecto, buscar el
  ID de la sesión, tipear `claude --resume ...`.
- Te fuiste de casa y querés **mirar desde el teléfono** si aquella corrida larga terminó
  — no se puede, vive en una ventana de tu Mac.

Cacho junta todo eso en una sola app:

- **Barra lateral con todas las sesiones**, agrupadas por proyecto, con un resumen vivo de
  qué está haciendo cada una (lee los transcripts de `~/.claude/projects`). Las que corren
  fuera de la app también aparecen.
- **Terminales reales** (PTY + zsh) en pestañas, con la estética de la app de Claude.
  Cerrar la ventana no mata nada: las pestañas viven en el server, la ventana es solo la
  vista. Una tarea lanzada a las 4 AM te queda esperando a la mañana.
- **Retomar con un click**: las charlas terminadas se reabren en una pestaña nueva con
  `claude --resume`, siguiendo la conversación donde quedó.
- **Desde el teléfono**: si tenés Tailscale, la misma interfaz anda en el celular
  (adaptada: un solo stream SSE, porque iOS corta a ~6 conexiones por host; adjuntar
  archivos con ＋).
- **API mínima para automatizar**: `cacho_lanzar.py "<prompt>"` crea una pestaña, espera
  que `claude` levante y le tipea el prompt — ideal para que tus rutinas programadas corran
  *adentro* de Cacho en vez de en ventanas sueltas.

Sin dependencias: Python 3 puro (stdlib), [xterm.js](https://github.com/xtermjs/xterm.js)
vendoreado en `static/`.

## Instalación

Requisitos: macOS, Python 3.9+, [Claude Code](https://claude.com/claude-code), y Google
Chrome para la ventana tipo app (opcional: sin Chrome, abrís la URL en cualquier navegador).

```bash
git clone https://github.com/federicolacuesta-cmyk/cacho.git
cd cacho
bash instalar_cacho_app.sh   # genera ~/Applications/Cacho.app (idempotente)
open ~/Applications/Cacho.app
```

La app levanta el server (si no estaba) y abre la ventana. También podés correrlo a mano:

```bash
python3 serve_sesiones.py                 # http://127.0.0.1:8811
CACHO_PORT=9000 python3 serve_sesiones.py # otro puerto
```

La primera vez macOS va a re-pedir permisos para `python3` (Documentos/Escritorio): es
normal, las terminales ahora viven dentro de Cacho.

### Desde el teléfono (opcional)

Con [Tailscale](https://tailscale.com) en la Mac y en el teléfono, entrá a
`http://<ip-tailscale-de-tu-mac>:8811`. Te va a pedir el PIN (está en `~/.cacho_pin` de
la Mac); se tipea una vez y queda la cookie un año.

### Lanzar tareas desde scripts

```bash
python3 cacho_lanzar.py "revisá los logs de anoche y resumime qué pasó" ~/mi/proyecto
```

Crea la pestaña, espera que `claude` arranque y le pega el prompt. Exit 0 si quedó
corriendo; distinto de 0 si no se pudo (tu script decide el fallback).

## Seguridad

Una terminal accesible por red es control total de la máquina, así que Cacho viene
cerrado por defecto, en capas:

1. **Escucha solo en `127.0.0.1`** — nunca `0.0.0.0`.
2. **PIN obligatorio** en toda request (salvo assets y el healthcheck). Se genera solo en
   `~/.cacho_pin` (600). Importa incluso siendo localhost: si usás Tailscale en modo
   userspace, tus otros dispositivos llegan a localhost aunque el bind sea local.
3. **Anti DNS-rebinding**: se valida `Host` y `Origin` en cada request. IPs literales
   pasan (una IP no se puede rebindear); nombres solo `localhost`, sin punto, o
   `*.ts.net` (MagicDNS). Cualquier dominio ajeno → 403, antes incluso de mirar el PIN.

Lo que NO hay que hacer: cambiar el bind a `0.0.0.0`, exponer el puerto con un
port-forward, o compartir el PIN.

## Cómo está hecho

- `serve_sesiones.py` — todo el server: HTTP + SSE, PTYs, parseo de transcripts, y la
  página (HTML/CSS/JS inline). Un solo archivo a propósito: se lee de punta a punta.
- `cacho_lanzar.py` — abre una tarea como pestaña dentro de Cacho (para automatizaciones).
- `instalar_cacho_app.sh` — genera `Cacho.app` en `~/Applications` con la ruta real del clon.
- `static/` — xterm.js (MIT) + el logo.

## Créditos

- [xterm.js](https://github.com/xtermjs/xterm.js) (MIT) — la terminal en el navegador.
- La validación de Host/Origin salió de una revisión de seguridad de una amiga. Las
  sugerencias son bienvenidas: issues y PRs abiertos.

Licencia [MIT](LICENSE).

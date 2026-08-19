#!/usr/bin/env bash
# Instala Cacho.app (el gestor de sesiones de Claude Code) en ESTA máquina.
#
# Por qué existe: la app del Dock (~/Applications/Cacho.app) vive fuera del repo.
# Este script la genera en cualquier máquina a partir de lo que SÍ está en el repo
# (serve_sesiones.py + icono.icns), horneando la ruta real del clon en el lanzador.
#
# Correr UNA vez por máquina (idempotente, re-correr la regenera):
#   bash instalar_cacho_app.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVE="$DIR/serve_sesiones.py"
ICONO="$DIR/icono.icns"
APP="$HOME/Applications/Cacho.app"

[[ -f "$SERVE" ]] || { echo "ERROR: no encuentro $SERVE" >&2; exit 1; }
[[ -f "$ICONO" ]] || { echo "ERROR: no encuentro $ICONO" >&2; exit 1; }

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Cacho</string>
  <key>CFBundleDisplayName</key><string>Cacho</string>
  <key>CFBundleIdentifier</key><string>ar.cacho.sesiones</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>lanzador</string>
  <key>CFBundleIconFile</key><string>icono</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# El lanzador se genera con la ruta REAL del server en esta máquina (SERVE),
# así funciona aunque el repo esté clonado en otro lado o con otro usuario.
cat > "$APP/Contents/MacOS/lanzador" <<LANZADOR
#!/bin/bash
URL="http://127.0.0.1:8811"
SCRIPT="$SERVE"

# 1. server arriba (si no, levantarlo)
if ! curl -s -m 1 "\$URL/api/ping" > /dev/null; then
  nohup /usr/bin/env python3 "\$SCRIPT" >> "\$HOME/Library/Logs/cacho-server.log" 2>&1 &
  for i in \$(seq 1 30); do
    curl -s -m 1 "\$URL/api/ping" > /dev/null && break
    sleep 0.3
  done
fi

# El server exige PIN (~/.cacho_pin, lo genera él al arrancar) y la ventana local
# tiene que entrar sola. OJO con CÓMO entra:
#   · El PIN NO viaja como argumento. Se le pasa al server por la ENTRADA ESTÁNDAR
#     de curl, porque todo lo que va en una línea de comandos lo lee cualquier
#     proceso de la máquina con un `ps`.
#   · Lo que sí termina en la URL de Chrome es un TICKET de un solo uso que vence
#     en 60 s. Antes iba el PIN ahí, y quedaba a la vista en `ps` todas las horas
#     que la ventana estuviera abierta.
# Server viejo (sin /api/ticket): se cae al PIN en la URL, como antes.
PIN=\$(cat "\$HOME/.cacho_pin" 2>/dev/null || true)
if [ -n "\$PIN" ]; then
  TICKET=\$(printf 'pin=%s' "\$PIN" | curl -s -m 3 -X POST --data-binary @- \\
            "\$URL/api/ticket" | sed -n 's/.*"t": *"\\([^"]*\\)".*/\\1/p')
  if [ -n "\$TICKET" ]; then URL="\$URL/?t=\$TICKET"; else URL="\$URL/?pin=\$PIN"; fi
fi

# 2. si ya hay una ventana de la app, traerla al frente EN SERIO; si no, abrirla.
#    (15-ago-2026) La versión vieja matcheaba por título "Cacho" (cualquier
#    pestaña con ese nombre la engañaba) y hacía solo "set index to 1", que en
#    Chrome NO levanta la ventana si está minimizada o detrás de otra →
#    apretabas el ícono y "no pasaba nada". Ahora: identidad por URL, se
#    desminimiza, se sube y se VERIFICA que quedó al frente; si no quedó, se
#    cierra y se abre de nuevo (la página es un visor sin estado: las sesiones
#    viven en el server, cerrar la ventana no pierde nada).
if pgrep -x "Google Chrome" > /dev/null; then
  YA=\$(osascript 2>/dev/null <<'OSA'
tell application "Google Chrome"
  -- OJO: operar SIEMPRE por "window id": las referencias por posicion
  -- (item N of windows) se corren al mover/cerrar ventanas y terminan
  -- cerrando la ventana equivocada (paso el 15-ago: cerro un Gemini ajeno).
  set ids to {}
  repeat with w in windows
    try
      if URL of active tab of w contains "127.0.0.1:8811" then set end of ids to (id of w)
    end try
  end repeat
  if (count of ids) is 0 then return "no"
  repeat with i from 2 to (count of ids)
    try
      close (window id (item i of ids))
    end try
  end repeat
  set principal to window id (item 1 of ids)
  try
    set minimized of principal to false
  end try
  activate
  set index of principal to 1
  delay 0.3
  try
    if URL of active tab of front window contains "127.0.0.1:8811" then return "si"
  end try
  try
    close principal
  end try
  return "reabrir"
end tell
OSA
)
  if [ "\$YA" = "si" ]; then exit 0; fi
fi
open -na "Google Chrome" --args --app="\$URL"
LANZADOR
chmod +x "$APP/Contents/MacOS/lanzador"

cp "$ICONO" "$APP/Contents/Resources/icono.icns"
touch "$APP"   # que Finder/Dock refresquen el ícono

echo "OK: Cacho.app instalada en $APP"
echo "    server: $SERVE"
echo "Abrila desde ~/Applications (y arrastrala al Dock si querés)."

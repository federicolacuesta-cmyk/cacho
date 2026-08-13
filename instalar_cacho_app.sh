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

# El server exige PIN (~/.cacho_pin, lo genera él al arrancar); acá va en la
# URL para que la ventana local entre sola. Server viejo sin PIN: lo ignora.
PIN=\$(cat "\$HOME/.cacho_pin" 2>/dev/null || true)
[ -n "\$PIN" ] && URL="\$URL/?pin=\$PIN"

# 2. si ya hay una ventana de la app, traerla al frente; si no, abrirla
if pgrep -x "Google Chrome" > /dev/null; then
  YA=\$(osascript -e 'tell application "Google Chrome"
    repeat with w in windows
      if title of w contains "Cacho" then
        set index of w to 1
        return "si"
      end if
    end repeat
    return "no"
  end tell' 2>/dev/null)
  if [ "\$YA" = "si" ]; then
    osascript -e 'tell application "Google Chrome" to activate'
    exit 0
  fi
fi
open -na "Google Chrome" --args --app="\$URL"
LANZADOR
chmod +x "$APP/Contents/MacOS/lanzador"

cp "$ICONO" "$APP/Contents/Resources/icono.icns"
touch "$APP"   # que Finder/Dock refresquen el ícono

echo "OK: Cacho.app instalada en $APP"
echo "    server: $SERVE"
echo "Abrila desde ~/Applications (y arrastrala al Dock si querés)."

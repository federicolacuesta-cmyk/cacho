#!/usr/bin/env bash
# instalar.sh — deja Cacho andando en esta Mac, de punta a punta.
#
# Qué hace, en orden:
#   1. Chequea que estén las tres cosas que hacen falta (macOS, Python 3, Claude Code).
#   2. Se copia a ~/Claude/cacho — un lugar estable. Si el programa vive en el Escritorio
#      o en un pendrive, el día que se borra esa carpeta Cacho deja de abrir y nadie
#      entiende por qué.
#   3. Te pregunta los sectores de tu empresa y escribe los agentes (configurar.py).
#   4. Instala Cacho.app en ~/Applications y la abre.
#
# Se puede correr de nuevo cuando quieras: no rompe nada de lo que ya está.
set -euo pipefail

ORIGEN="$(cd "$(dirname "$0")" && pwd)"
DESTINO="$HOME/Claude/cacho"
azul() { printf "\033[1;34m%s\033[0m\n" "$1"; }
mal()  { printf "\033[1;31m✗ %s\033[0m\n" "$1" >&2; }
bien() { printf "\033[1;32m✓\033[0m %s\n" "$1"; }

echo ""
azul "════════════════════════════════════════════════════════════"
azul "  Cacho — instalación"
azul "════════════════════════════════════════════════════════════"
echo ""

# ── 1. Lo que tiene que estar ────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || { mal "Esto es para Mac."; exit 1; }
bien "macOS"

command -v python3 >/dev/null || {
  mal "Falta Python 3."
  echo "  Instalalo con:  xcode-select --install"
  exit 1
}
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' || {
  mal "Python $PYV es muy viejo (hace falta 3.9 o más nuevo)."; exit 1; }
bien "Python $PYV"

if command -v claude >/dev/null; then
  bien "Claude Code"
else
  mal "No encuentro el comando «claude» en esta Mac."
  echo "  Cacho es la ventana: lo que trabaja adentro es Claude Code."
  echo "  Instalalo desde https://claude.com/claude-code y volvé a correr esto."
  exit 1
fi

# Claude Code guarda cada charla en ~/.claude/projects. Si todavía no corrió NUNCA en
# esta Mac, esa carpeta no existe y Cacho abre con un error en vez de la lista de
# sesiones. Crearla vacía cuesta nada y saca de una el peor primer día posible.
mkdir -p "$HOME/.claude/projects"

# ── 2. A un lugar estable ────────────────────────────────────────────────────
if [[ "$ORIGEN" != "$DESTINO" ]]; then
  mkdir -p "$HOME/Claude"
  if [[ -d "$DESTINO" ]]; then
    # Ya había una instalación: se respeta lo configurado (los agentes y las caras) y
    # se actualiza el programa. Pisar empresa.json sería borrarle el trabajo a quien
    # ya configuró sus sectores.
    echo "  Ya había un Cacho en $DESTINO — actualizo el programa y dejo tus agentes."
    rsync -a --exclude 'empresa.json' --exclude 'areas.py' --exclude 'static/' \
          --exclude '__pycache__' "$ORIGEN/" "$DESTINO/"
    # static/ SÍ se pisa: ahí vive el programa (el CSS de la interfaz, xterm). Con
    # --ignore-existing una actualización dejaba el CSS viejo para siempre. rsync sin
    # --delete no borra lo que sólo está en destino, así que las caras que generó
    # configurar.py quedan donde están.
    rsync -a "$ORIGEN/static/" "$DESTINO/static/"
  else
    rsync -a --exclude '.git' --exclude '__pycache__' --exclude '.DS_Store' \
          "$ORIGEN/" "$DESTINO/"
  fi
  bien "Programa en $DESTINO"
fi
cd "$DESTINO"

# ── 3. Los agentes ───────────────────────────────────────────────────────────
echo ""
if [[ -f "$DESTINO/empresa.json" ]]; then
  azul "── Tus agentes ya estaban configurados ──"
  read -r -p "  ¿Querés cambiarlos ahora? (s/N) " R || R=""
  [[ "${R:-}" == s* || "${R:-}" == S* ]] && python3 configurar.py
else
  azul "── Ahora armamos los agentes de tu empresa ──"
  python3 configurar.py
fi

# ── 4. La app ────────────────────────────────────────────────────────────────
echo ""
bash instalar_cacho_app.sh
echo ""
azul "════════════════════════════════════════════════════════════"
azul "  Listo. Cacho está instalado."
azul "════════════════════════════════════════════════════════════"
echo "  · La app:        ~/Applications/Cacho.app  (arrastrala al Dock)"
echo "  · El programa:   $DESTINO"
echo "  · Cambiar los agentes:   cd $DESTINO && python3 configurar.py"
echo ""
echo "  Leé GUIA.md — está escrita para usarlo, no para programarlo."
echo ""
read -r -p "  ¿Lo abro ahora? (S/n) " R || R="n"
if [[ "${R:-s}" != n* && "${R:-s}" != N* ]]; then
  open "$HOME/Applications/Cacho.app"
fi

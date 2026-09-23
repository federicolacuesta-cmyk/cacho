#!/usr/bin/env bash
# instalar.sh — deja Cacho andando en esta Mac, de punta a punta.
#
# Qué hace, en orden:
#   1. Chequea que estén las tres cosas que hacen falta (macOS, Python 3, Claude Code) y,
#      si algo no está, DIAGNOSTICA por qué: el caso más común no es «falta», es que las
#      Command Line Tools instaladas son de otra arquitectura y `git` y `python3` existen
#      pero no corren.
#   2. Se copia a ~/Claude/cacho — un lugar estable. Si el programa vive en el Escritorio
#      o en un pendrive, el día que se borra esa carpeta Cacho deja de abrir y nadie
#      entiende por qué.
#   3. Arma los agentes de la empresa (configurar.py).
#   4. Instala Cacho.app en ~/Applications y la abre.
#
# Se puede correr de nuevo cuando quieras: no rompe nada de lo que ya está.
#
# SIN PREGUNTAS (para que lo corra un asistente y no una persona):
#   bash instalar.sh --sin-preguntar \
#        --empresa "Nombre SA" --rubro "a qué se dedica" \
#        --sectores "Administración,Logística,Ventas"
#   ...y en vez de --rubro (una línea) se puede pasar --perfil ~/Downloads/empresa.md,
#   un archivo que entra ENTERO al CLAUDE.md que leen todos los agentes.
set -euo pipefail

ORIGEN="$(cd "$(dirname "$0")" && pwd)"
DESTINO="$HOME/Claude/cacho"
azul() { printf "\033[1;34m%s\033[0m\n" "$1"; }
mal()  { printf "\033[1;31m✗ %s\033[0m\n" "$1" >&2; }
bien() { printf "\033[1;32m✓\033[0m %s\n" "$1"; }

# ── los flags ────────────────────────────────────────────────────────────────
SIN_PREGUNTAR=0; ABRIR=""; PASAR=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sin-preguntar) SIN_PREGUNTAR=1; PASAR+=("--sin-preguntar"); shift ;;
    --abrir)         ABRIR=1; shift ;;
    --no-abrir)      ABRIR=0; shift ;;
    --empresa|--rubro|--sectores|--perfil)
      [[ $# -ge 2 ]] || { mal "$1 necesita un valor."; exit 2; }
      PASAR+=("$1" "$2"); shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) mal "no conozco el parámetro «$1». Probá --help."; exit 2 ;;
  esac
done
# Sin preguntas, no se abre nada solo salvo que lo pidan: una app que salta en la cara
# de alguien que está mirando otra cosa es peor que una app que no se abrió.
[[ -z "$ABRIR" ]] && { [[ $SIN_PREGUNTAR -eq 1 ]] && ABRIR=0 || ABRIR=""; }

echo ""
azul "════════════════════════════════════════════════════════════"
azul "  Cacho — instalación"
azul "════════════════════════════════════════════════════════════"
echo ""

# ── 1. Lo que tiene que estar ────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || { mal "Esto es para Mac."; exit 1; }
bien "macOS $(sw_vers -productVersion 2>/dev/null || echo "") ($(uname -m))"

# Las herramientas de Apple son el cuello de botella real. «No está instalado» y «está
# instalado para el procesador equivocado» se arreglan con comandos DISTINTOS, y el
# segundo caso, si no se nombra, parece una máquina embrujada: el comando existe, se
# tipea, y contesta un error que no menciona la arquitectura por ningún lado.
clt_rotas() {
  local clt bin archs
  clt="$(xcode-select -p 2>/dev/null || true)"
  [[ -n "$clt" ]] || return 1
  for bin in git python3 clang; do
    [[ -x "$clt/usr/bin/$bin" ]] || continue
    archs="$(lipo -archs "$clt/usr/bin/$bin" 2>/dev/null || true)"
    [[ -n "$archs" ]] || continue
    if [[ "$archs" != *"$(uname -m)"* ]]; then
      echo "$clt|$bin|$archs"
      return 0
    fi
  done
  return 1
}

decir_como_arreglar_clt() {
  local d="$1" clt="${1%%|*}"
  echo ""
  mal "Las herramientas de Apple (Command Line Tools) son del procesador equivocado."
  echo "     Instaladas:  $(echo "$d" | cut -d'|' -f3)      Esta Mac:  $(uname -m)"
  echo "     Por eso «git» y «python3» están pero no corren."
  echo ""
  echo "  Se arregla borrándolas e instalándolas de nuevo (te va a pedir la contraseña"
  echo "  de la Mac, y la segunda parte abre una ventana de Apple que tarda un rato):"
  echo ""
  echo "      sudo rm -rf $clt && xcode-select --install"
  echo ""
  echo "  Cuando termine esa ventana, volvé a correr esto mismo."
}

# python3 puede EXISTIR como comando y no arrancar (ver arriba): no alcanza con
# `command -v`, hay que correrlo.
if ! python3 -c 'pass' >/dev/null 2>&1; then
  if D="$(clt_rotas)"; then decir_como_arreglar_clt "$D"; else
    mal "Falta Python 3."
    echo "  Instalalo con:  xcode-select --install"
  fi
  exit 1
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' || {
  mal "Python $PYV es muy viejo (hace falta 3.9 o más nuevo)."; exit 1; }
bien "Python $PYV"

# git no lo usa Cacho para andar, pero sí para actualizarse. Si está roto se avisa y se
# sigue: no tiene sentido frenar una instalación que va a funcionar igual.
if ! git --version >/dev/null 2>&1; then
  if D="$(clt_rotas)"; then decir_como_arreglar_clt "$D"
  else echo "  ⚠️  git no anda en esta Mac. Cacho funciona igual; para actualizarlo vas a"
       echo "      tener que bajar el .zip a mano."; fi
fi

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
if [[ -f "$DESTINO/empresa.json" && $SIN_PREGUNTAR -eq 0 ]]; then
  azul "── Tus agentes ya estaban configurados ──"
  read -r -p "  ¿Querés cambiarlos ahora? (s/N) " R || R=""
  [[ "${R:-}" == s* || "${R:-}" == S* ]] && python3 configurar.py
else
  [[ $SIN_PREGUNTAR -eq 0 ]] && azul "── Ahora armamos los agentes de tu empresa ──"
  python3 configurar.py "${PASAR[@]+"${PASAR[@]}"}"
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
if [[ "$ABRIR" == "1" ]]; then
  open "$HOME/Applications/Cacho.app"
elif [[ -z "$ABRIR" ]]; then
  read -r -p "  ¿Lo abro ahora? (S/n) " R || R="n"
  [[ "${R:-s}" != n* && "${R:-s}" != N* ]] && open "$HOME/Applications/Cacho.app"
fi

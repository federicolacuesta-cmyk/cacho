#!/usr/bin/env python3
"""El policía: una segunda lectura de lo que se cambió, antes de que se use.

    python3 revisar.py                  lo que cambió en esta carpeta (git)
    python3 revisar.py archivo.py ...   esos archivos
    python3 revisar.py --commit abc123  ese commit

La regla, que es toda la gracia: **el que revisa NO escribe**. Este script no toca
ningún archivo. Lee, le pide a Claude que lo mire con ojo de auditor y te muestra lo
que encontró. Arreglarlo lo decidís vos.

No necesita nada instalado además del `claude` que ya usás en la terminal: corre
`claude -p` con tu propia cuenta.

Devuelve 1 si hay algo 🔴 (así se puede enganchar a un `git commit` el día que quieran).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

TOPE = 120_000   # lo que se le manda de una; más que esto no lo lee bien nadie

PROMPT = """Sos el policía del código de esta empresa. Revisás y NO escribís: no
propongas parches largos ni reescribas el archivo, señalá.

Mirá el material de abajo y buscá, en este orden:

1. 🔴 Lo que está MAL y se va a notar: cuentas equivocadas, un caso que rompe, plata o
   datos que se pierden, algo que le pega a producción sin querer, un secreto adentro
   del código.
2. 🔴 Lo que falla EN SILENCIO: un except que se traga el error, un valor por defecto
   que tapa que el dato no vino, un "no lo vi" que se escribe como un 0.
3. 🟡 Lo que va a confundir a quien lo lea en tres meses.

Reglas de la revisión:
- Si no encontrás nada, decí «sin hallazgos» y listo. No inventes para justificar.
- Cada hallazgo: una línea de título, el archivo y la línea, y en criollo QUÉ pasa
  concretamente (con qué dato entra y qué sale mal). Sin sermones.
- No pidas tests ni renombres por gusto: esto son scripts de trabajo, no un producto.

Material:
"""


def corriendo(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def material(args):
    """Qué se le da a leer. Nunca vacío: si no hay nada que revisar, se dice y se sale,
    porque mandarle la nada a un revisor devuelve un «todo bien» que no vale nada."""
    if args.archivos:
        partes = []
        for f in args.archivos:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    partes.append("===== %s =====\n%s" % (f, fh.read()))
            except OSError as e:
                print("No pude leer %s: %s" % (f, e), file=sys.stderr)
                sys.exit(2)
        return "\n\n".join(partes), "%d archivo(s)" % len(args.archivos)

    if args.commit:
        r = corriendo(["git", "show", args.commit])
        if r.returncode:
            print(r.stderr.strip() or "no pude leer ese commit", file=sys.stderr)
            sys.exit(2)
        return r.stdout, "el commit %s" % args.commit

    r = corriendo(["git", "rev-parse", "--is-inside-work-tree"])
    if r.returncode:
        print("Acá no hay un repo de git. Pasame los archivos:\n"
              "   python3 revisar.py archivo1.py archivo2.py", file=sys.stderr)
        sys.exit(2)
    diff = corriendo(["git", "diff", "HEAD"]).stdout
    nuevos = corriendo(["git", "ls-files", "--others", "--exclude-standard"]).stdout.split()
    for f in nuevos[:20]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                diff += "\n===== %s (archivo nuevo) =====\n%s" % (f, fh.read())
        except OSError:
            pass
    if not diff.strip():
        print("No hay nada cambiado para revisar.")
        sys.exit(0)
    return diff, "lo que cambió y todavía no está commiteado"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archivos", nargs="*")
    ap.add_argument("--commit", help="revisar un commit ya hecho")
    args = ap.parse_args()

    claude = shutil.which("claude")
    if not claude:
        print("No encuentro el comando `claude` en esta computadora.\n"
              "El policía lo usa para leer con tu propia cuenta.", file=sys.stderr)
        sys.exit(2)

    texto, de_que = material(args)
    if len(texto) > TOPE:
        # Cortar callado es lo peor que puede hacer un revisor: revisa la mitad y dice
        # que está todo bien. Se corta, pero se avisa en la cara.
        print("⚠️  Son %d caracteres: reviso los primeros %d. Lo de más abajo NO lo miré."
              % (len(texto), TOPE))
        texto = texto[:TOPE]

    print("👮 Revisando %s…\n" % de_que)
    r = subprocess.run([claude, "-p", PROMPT + texto], capture_output=True, text=True)
    salida = (r.stdout or "").strip()
    if r.returncode and not salida:
        print(r.stderr.strip() or "el revisor no contestó", file=sys.stderr)
        sys.exit(2)
    if not salida:
        # Un revisor que contesta vacío no es un «está todo bien».
        print("El revisor contestó vacío. No lo tomes como aprobado.", file=sys.stderr)
        sys.exit(2)
    print(salida)
    sys.exit(1 if "🔴" in salida else 0)


if __name__ == "__main__":
    main()

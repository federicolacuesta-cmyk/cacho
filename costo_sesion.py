#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""costo_sesion.py — cuánto pesa arrastrar una charla, y cuándo conviene cortarla.

POR QUÉ EXISTE (24-ago-2026). el usuario preguntó cómo gastar menos. Se midieron los 973
transcripts y el resultado dio vuelta la intuición:

    charlas con el usuario   96% del output ·  99% del volumen
    rutinas headless    4% del output ·   1% del volumen
    Opus + Fable      99,4%   |   Sonnet + Haiku  0,5%

O sea que ajustar el modelo de las rutinas —que es lo que uno haría primero— toca el 0,5%
del gasto. El gasto ES la conversación. Y adentro de eso el culpable tiene nombre: las
sesiones larguísimas. La peor medida llevaba **1.410 turnos** y 0,73 G de relectura.

LA RAÍZ, y por qué no se arregla bajando de modelo: en cada turno se relee la charla
entera. El costo de una conversación no crece con los turnos, crece con **turnos ×
contexto** — o sea al cuadrado. En el turno 1.400 se releen 1.399 mensajes para contestar
uno. Bajar de modelo ahí es trabajar peor con el usuario para tapar un problema que es de forma,
no de motor: la MISMA charla partida en cinco cuesta como una quinta parte, mismo modelo y
misma calidad.

QUÉ MIDE, y por qué ese número y no el acumulado. El acumulado histórico es interesante
para un informe pero no sirve para decidir: lo que importa es **cuánto va a costar el
PRÓXIMO mensaje**, que es el contexto que la sesión arrastra ahora. Ese número está en la
última línea con `usage` del transcript (`cache_read + cache_creation`), o sea en la COLA
del archivo — que es justo lo único que el panel ya lee. Medir el acumulado obligaría a
leer transcripts de cientos de MB en cada refresco del panel, y un vigía que hace lento el
panel se termina apagando.

UMBRALES (rehechos el 21-set-2026). Hasta ese día la ventana era de 1M y la máquina no
compactaba nunca (la primera vez que compacta es al borde de la ventana): en 7 días, 550
sesiones y 44.395 turnos, el 42% de los turnos corrió arrastrando MÁS de 200k de contexto y
esos turnos fueron el 63% de todo lo releído (9,2 G de tokens). el usuario: *«cuando una sesión se
vuelve demasiado pesada la apartás o arrancás una nueva con un compacto de lo viejo»*.
La RAÍZ se arregló en la puerta, no acá: `autoCompactWindow` en `.claude/settings.json`
(y en `~/.claude/settings.json` para los otros proyectos de la Mac) hace que TODA pestaña
compacte sola al llegar al tope —entonces 200k, hoy TOPE_AUTOCOMPACT— —el harness resume lo viejo y sigue en la misma pestaña, con
`compactacion_guard.py` guardando el crudo antes y revisando el resumen después—. Medido
sobre esa misma semana: -45% de relectura, -36% del gasto ponderado.

Con eso, este semáforo cambia de oficio: ya no le pide a el usuario que abra otra pestaña; VIGILA
que el tope rija. 🟡 = se pasó del tope (transitorio: está por compactar). 🔴 = el tope NO está
rigiendo (setting pisado, versión nueva que lo renombró, `DISABLE_AUTO_COMPACT` en el ambiente…)
y eso va al panel de la casa (`vigilar()`), porque el setting mal escrito falla EN SILENCIO
(`.catch(undefined)` en el esquema de Claude Code: vuelve al 1M sin avisar).

EL TOPE PASÓ DE 200k A 300k (22-set-2026). El de 200k rigió: el contexto medio por turno cayó
de 208k a 116k y el costo por turno un 35%. Pero compactar no sale gratis — la pestaña pierde
el detalle y REHACE trabajo. Medido dentro de las MISMAS pestañas, antes y después de su primer
compactado de ese día (43 pestañas, 206 compactados): 81 → 113 turnos por hora (+39%) y el
costo por HORA de pestaña subió 24%, aunque cada turno saliera 11% más barato. O sea: el tope
de 200k ganaba en su eje y perdía la cuenta. A 300k compacta sólo la charla de verdad pesada
(las de 400k+ eran el 3% de los turnos y el 11% de todo lo releído) y se rehace mucho menos.
La verificación es este mismo vigía: si el 🔴 aparece seguido, el tope no rige.

Y VOLVIÓ A 190k EL MISMO DÍA (22-set-2026, decisión del usuario), porque a la cuenta de arriba le
faltaba un factor: **arriba de 200k el precio es DOBLE** (long context) y la vara de la casa
—`uso_quien.py`: input 1 · caché creada 1,25 · caché leída 0,1 · output 5— no lo cobraba. Con esa
vara, compactar parecía costar +24% por hora; cobrando el doble, la misma comparación da −5% por
hora sobre las 45 pestañas de ese día (y −30% sobre las 72 del histórico). Medido sobre la semana
del 21-set: el 21,1% del peso ocurría arriba de 200k, o sea **~35% de la factura real**. Por eso
el tope no vuelve a 200.000 sino a **190.000**: el autocompactado dispara AL LLEGAR al tope, así
que un tope de 200k deja turnos justo en el filo (el del 21-set dejó 0,3% de turnos arriba);
190k da margen para que ninguno entre a la franja cara. La vara quedó corregida en `uso_quien.py`,
que desde hoy cobra el doble arriba de 200k: ninguna decisión futura sobre el tope se toma sin eso.
"""
from __future__ import annotations

import json

VERDE, AMARILLO, ROJO = "verde", "amarillo", "rojo"
TOPE_AUTOCOMPACT = 190_000        # = autoCompactWindow en .claude/settings.json
UMBRAL_AMARILLO = TOPE_AUTOCOMPACT
# El 🔴 dice «el tope NO rige», así que vive pegado al tope: con 190k, una charla en 300k ya no
# es «pesada», es que el setting no está rigiendo. (Era 450k cuando el tope era 300k.)
UMBRAL_ROJO = 300_000


def contexto_de_lineas(lineas) -> int:
    """Tokens que arrastra la charla, leyendo de atrás para adelante.

    De atrás porque el dato que vale es el del ÚLTIMO turno: el contexto sólo crece, así
    que el último `usage` es el estado actual. Buscar de adelante daría el de un turno
    viejo y subestimaría siempre.
    """
    for linea in reversed(lineas):
        if not linea or '"usage"' not in linea:
            continue
        try:
            u = (json.loads(linea).get("message") or {}).get("usage") or {}
        except (ValueError, AttributeError):
            continue          # una línea cortada por el chunk no es un error: se ignora
        if not u:
            continue
        ctx = ((u.get("cache_read_input_tokens") or 0)
               + (u.get("cache_creation_input_tokens") or 0)
               + (u.get("input_tokens") or 0))
        if ctx:
            return ctx
    return 0


def veredicto(ctx: int) -> str:
    if ctx >= UMBRAL_ROJO:
        return ROJO
    if ctx >= UMBRAL_AMARILLO:
        return AMARILLO
    return VERDE


def en_criollo(ctx: int) -> str:
    """La frase que ve el usuario. Sin jerga y con el porqué, o no significa nada."""
    if ctx < UMBRAL_AMARILLO:
        return ""
    mil = f"{round(ctx / 1000):,}".replace(",", ".")
    # El tope se dice desde TOPE_AUTOCOMPACT: un literal acá le miente a el usuario el día que se mueve
    # el tope (pasó el 22-set-2026, de 200k a 300k, y el cartel siguió diciendo «200 mil»).
    tope = f"{round(TOPE_AUTOCOMPACT / 1000):,}".replace(",", ".")
    if ctx >= UMBRAL_ROJO:
        return (f"Esta pestaña arrastra {mil} mil de contexto y la máquina tenía que haberla "
                f"compactado a {tope} mil: el tope NO está rigiendo. Cacho ya tiene el aviso.")
    return (f"Arrastra {mil} mil de contexto: pasó el tope de {tope} mil y está por compactar "
            f"sola. Si no baja en el próximo mensaje, se pone roja.")

# ------------------------------------------------------------------ el vigía del tope
_AVISADAS: set = set()


def vigilar(sid: str, ctx: int, viva: bool, mtime: float = 0.0) -> None:
    """Una pestaña VIVA en rojo = el tope de autocompactación no está rigiendo → panel de la casa.

    La levanta el server de Cacho en cada refresco (una sola vez por pestaña) y la retira él
    mismo cuando la pestaña baja del rojo o muere (el que levanta la alerta es el que la retira).
    Sólo cuenta un turno POSTERIOR al tope (`mtime` del transcript > mtime del settings.json):
    una charla que quedó pesada de ANTES no es el tope fallando, es una charla vieja.
    Nunca lanza: si no está `casa_alertas` (la copia publicada en una jaula), no hace nada.
    """
    clave = "sesion-pesada-" + (sid or "")[:8]
    try:
        import os, sys
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if raiz not in sys.path:
            sys.path.insert(0, raiz)
        import casa_alertas as al
        tope_desde = os.path.getmtime(os.path.join(raiz, ".claude", "settings.json"))
    except Exception:                                   # noqa: BLE001 — jaula sin la casa
        return
    try:
        if viva and ctx >= UMBRAL_ROJO and mtime > tope_desde:
            if clave in _AVISADAS:
                return
            mil = f"{round(ctx / 1000):,}".replace(",", ".")
            al.avisar_panel(clave, "Pestaña %s arrastra %s mil: el tope de compactación no rige" % (sid[:8], mil),
                            detalle=("`autoCompactWindow` = %s en .claude/settings.json tendría que haberla "
                                     "compactado. Revisar: el setting sigue ahí y es un entero (si no, Claude Code "
                                     "lo ignora en silencio), no hay DISABLE_AUTO_COMPACT en el ambiente, y la "
                                     "versión de `claude` sigue leyendo ese nombre "
                                     "(`strings ~/.local/bin/claude | grep autoCompactWindow`).") % TOPE_AUTOCOMPACT,
                            nivel="aviso", origen="costo_sesion", area="cacho", para="casa",
                            asunto="tope-no-rige")
            _AVISADAS.add(clave)
        elif clave in _AVISADAS:
            al.retirar(clave)
            _AVISADAS.discard(clave)
    except Exception:                                   # noqa: BLE001 — el panel no tumba el server
        return


def de_archivo(path: str, cola_bytes: int = 262_144) -> tuple[int, str]:
    """(contexto, veredicto) de un transcript, leyendo sólo la cola."""
    import os
    try:
        with open(path, "rb") as fh:
            tam = os.fstat(fh.fileno()).st_size
            if tam > cola_bytes:
                fh.seek(tam - cola_bytes)
                fh.readline()                 # descarto la primera, que viene cortada
            lineas = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0, VERDE
    ctx = contexto_de_lineas(lineas)
    return ctx, veredicto(ctx)


if __name__ == "__main__":
    import glob, os, time
    base = os.path.expanduser("~/.claude/projects")
    filas = []
    for f in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True):
        if os.path.basename(f).startswith("agent-"):
            continue
        ctx, v = de_archivo(f)
        if ctx:
            filas.append((ctx, v, os.path.basename(f)[:8], os.stat(f).st_mtime))
    filas.sort(reverse=True)
    hoy = [f for f in filas if f[3] > time.time() - 86400]
    print(f"{len(filas)} sesiones con datos · {len(hoy)} tocadas en las últimas 24 h · "
          f"tope de compactación {TOPE_AUTOCOMPACT:,} (🟡 pasó el tope · 🔴 el tope no rige)\n".replace(",", "."))
    ico = {ROJO: "🔴", AMARILLO: "🟡", VERDE: "🟢"}
    for ctx, v, sid, _ in (hoy or filas)[:15]:
        print(f"  {ico[v]} {ctx:>9,}".replace(",", ".") + f"  {sid}   {en_criollo(ctx)[:70]}")

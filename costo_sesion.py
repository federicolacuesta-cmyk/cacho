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

UMBRALES. La ventana es de 1M de tokens. Con 500k arrastrados, cada «dale» del usuario cuesta
medio millón de tokens de relectura antes de que conteste una palabra.
"""
from __future__ import annotations

import json

VERDE, AMARILLO, ROJO = "verde", "amarillo", "rojo"
UMBRAL_AMARILLO = 200_000
UMBRAL_ROJO = 500_000


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
    if ctx >= UMBRAL_ROJO:
        return (f"Charla pesada: arrastra {mil} mil de contexto, y eso se relee entero cada "
                f"vez que escribís. Conviene arrancar una nueva con el resumen.")
    return (f"Se está poniendo larga: {mil} mil de contexto por mensaje. Todavía va bien, "
            f"pero si el tema cambia, mejor abrir otra.")


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
    print(f"{len(filas)} sesiones con datos · {len(hoy)} tocadas en las últimas 24 h\n")
    ico = {ROJO: "🔴", AMARILLO: "🟡", VERDE: "🟢"}
    for ctx, v, sid, _ in (hoy or filas)[:15]:
        print(f"  {ico[v]} {ctx:>9,}".replace(",", ".") + f"  {sid}   {en_criollo(ctx)[:70]}")

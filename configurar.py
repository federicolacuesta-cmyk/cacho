#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""configurar.py — arma los agentes de TU empresa, sin tocar una línea de código.

Cacho agrupa las charlas por ÁREA: cada área es una persona del equipo, con su cara y su
color en la tira de la izquierda. Tocás una cara y ves sólo lo de esa área.

Este asistente te pregunta cómo se llama tu empresa y qué sectores tiene, y escribe solo:

  · `areas.py`                        las áreas que ve Cacho (cara, color, vocabulario)
  · `static/<sector>.png`             la cara de cada agente (círculo con sus iniciales)
  · `~/Claude/Projects/<Empresa>/`    la carpeta donde van a nacer las charlas
      · `CLAUDE.md`                   lo que TODOS los agentes saben de la empresa
      · `.claude/memory/<sector>/`    lo que cada agente va aprendiendo de lo suyo
  · `empresa.json`                    lo que contestaste (para poder volver a correrlo)

Se puede volver a correr todas las veces que quieras: agregar un sector, cambiarle el
color, borrar uno. Lo que ya contestaste queda, no hay que empezar de cero.

    python3 configurar.py

EL ÁREA NO ES EL TEMA: ES CON QUIÉN HABLÁS. Definidas así no se pisan y no hay que
discutir dos veces dónde va cada charla. «Administración» es con quien lleva la plata y
los papeles; «Logística» con quien mueve la mercadería; «Ventas» con quien atiende.

SIETE ES EL TECHO. Razonar con más de siete caras de color se cae: si te hace falta una
octava, conviene sacar otra antes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata

import caras

AQUI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(AQUI, "empresa.json")
STATIC = os.path.join(AQUI, "static")
CARPETA_PROYECTOS = os.path.expanduser("~/Claude/Projects")

# ── La paleta ──────────────────────────────────────────────────────────────────
# Ocho colores que se distinguen entre sí incluso para quien no separa rojo de verde
# (por eso además cada área lleva CARA: el color refuerza, no identifica).
PALETA = [
    ("teja",     "#C96442"), ("azul",     "#0072CE"), ("turquesa", "#00B8D4"),
    ("violeta",  "#9D1DFF"), ("amarillo", "#E6A800"), ("rojo",     "#E5173F"),
    ("fucsia",   "#FF1FA6"), ("verde",    "#2E9E5B"),
]

# ── El vocabulario que ya viene hecho ──────────────────────────────────────────
# Cacho adivina a qué área va cada charla CONTANDO PALABRAS (sin modelo: es instantáneo
# y no puede inventar). Para los sectores más comunes el vocabulario ya está escrito;
# para uno raro el asistente te pide 3 o 4 palabras. Siempre se puede editar después.
#   «propias»  → deciden solas (valen 2)
#   «palabras» → suman, pero necesitan compañía (valen 1)
OFICIOS = {
    "administracion": {
        "rol": "Administración", "gente": "proveedores y bancos", "color": "#E6A800",
        "propias": ["administracion", "administración", "bancari", "contab", "cobranza",
                    "flujo de caja", "estado de cuenta", "proveedor", "conciliacion",
                    "conciliación", "liquidacion", "liquidación"],
        "palabras": ["banco", "factura", "iva", "saldo", "balance", "boleta", "cheque",
                     "gasto", "pago", "cobro", "vencimiento", "impuesto", "recibo"],
    },
    "logistica": {
        "rol": "Logística", "gente": "la mercadería en movimiento", "color": "#0072CE",
        "propias": ["logistica", "logística", "despacho", "remito", "guia de carga",
                    "guía de carga", "transportista", "flete", "reparto", "cadete",
                    "deposito", "depósito", "ruta de entrega"],
        "palabras": ["envio", "envío", "entrega", "paquete", "pedido", "stock",
                     "recepcion", "recepción", "inventario", "camion", "camión",
                     "retiro", "devolucion", "devolución"],
    },
    "ventas": {
        "rol": "Ventas", "gente": "los clientes", "color": "#00B8D4",
        "propias": ["ventas", "vendedor", "vendedora", "cliente", "presupuesto",
                    "cotizacion", "cotización", "mostrador", "comision", "comisión",
                    "posventa", "postventa"],
        "palabras": ["venta", "precio", "descuento", "meta", "objetivo", "facturacion",
                     "facturación", "cierre de mes", "lista de precios", "atencion",
                     "atención", "reclamo"],
    },
    "compras": {
        "rol": "Compras", "gente": "los proveedores", "color": "#9D1DFF",
        "propias": ["compras", "reposicion", "reposición", "orden de compra",
                    "importacion", "importación", "surtido", "sobrestock"],
        "palabras": ["comprar", "catalogo", "catálogo", "temporada", "muestra",
                     "cotizar", "importar", "aduana", "rotacion", "rotación"],
    },
    "marketing": {
        "rol": "Marketing", "gente": "el mercado", "color": "#FF1FA6",
        "propias": ["marketing", "pauta", "creativ", "publicid", "campaña", "tiktok",
                    " ads", "seo", "reseña", "resena"],
        "palabras": ["anuncio", "posteo", "reel", "audiencia", "instagram", "blog",
                     "landing", "carrusel", "promocion", "promoción"],
    },
    "personal": {
        "rol": "Personal", "gente": "el equipo", "color": "#2E9E5B",
        "propias": ["personal", "recursos humanos", "rrhh", "sueldo", "nomina",
                    "nómina", "licencia", "ausentismo", "contratacion", "contratación"],
        "palabras": ["horario", "turno", "vacaciones", "certificado", "altas y bajas",
                     "capacitacion", "capacitación", "franco"],
    },
    "produccion": {
        "rol": "Producción", "gente": "el taller", "color": "#E5173F",
        "propias": ["produccion", "producción", "taller", "orden de trabajo",
                    "maquina", "máquina", "mantenimiento", "merma"],
        "palabras": ["lote", "insumo", "calidad", "retrabajo", "linea", "línea",
                     "capacidad", "turno de produccion", "turno de producción"],
    },
    "sistemas": {
        "rol": "Sistemas", "gente": "vos y la máquina", "color": "#C96442",
        "propias": ["sistemas", "servidor", "respaldo", "backup", "base de datos",
                    "usuario", "permiso", "licencia de software"],
        "palabras": ["computadora", "red", "impresora", "clave", "correo", "sincronizar",
                     "actualizar", "error"],
    },
}


# ── Herramientas chicas ────────────────────────────────────────────────────────
def _sin_tildes(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


# Palabras largas que tienen una abreviatura obvia. La clave sólo admite 12 letras, y
# «administracion» cortada a lo bruto daba «administraci», que después se veía en la URL
# y en el nombre de la carpeta de la memoria.
ABREVIATURAS = {
    "administracion": "admin", "comercializacion": "comercial",
    "atencionalcliente": "atencion", "recursoshumanos": "rrhh",
    "postventa": "postventa", "importaciones": "importa", "exportaciones": "exporta",
    "mantenimiento": "manten", "contabilidad": "contable",
}


def clave_de(nombre):
    """«Atención al cliente» -> «atencion». La clave es lo que viaja por la URL: sólo
    letras, 3 a 12 — es lo que `areas.valida()` acepta y nada más entra de afuera.

    Cuando no entra en 12 se busca una abreviatura conocida, si no la primera palabra
    que entre, y recién al final se corta. Un corte a lo bruto se ve fiero para siempre:
    la clave termina en la URL y en el nombre de la carpeta de la memoria."""
    limpio = re.sub(r"[^a-z]", "", _sin_tildes(nombre))
    if len(limpio) <= 12:
        return limpio if len(limpio) >= 3 else (limpio + "area")[:12]
    if limpio in ABREVIATURAS:
        return ABREVIATURAS[limpio]
    for palabra in _sin_tildes(nombre).split():
        p = re.sub(r"[^a-z]", "", palabra)
        if 3 <= len(p) <= 12:
            return p
    return limpio[:12]


# El catálogo se indexa por la MISMA clave que se le calcula a lo que escribe el usuario:
# «Administración», «administracion» y «ADMINISTRACION» tienen que caer todos en el mismo
# lugar, y esa clave va recortada a 12 letras porque es lo que `areas.valida()` acepta.
# (Sin esto, «Administración» daba «administraci» y no encontraba su propio vocabulario.)
POR_OFICIO = {clave_de(k): v for k, v in OFICIOS.items()}


def preguntar(texto, defecto=""):
    su = " [%s]" % defecto if defecto else ""
    try:
        r = input("%s%s: " % (texto, su)).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n(cancelado)")
        sys.exit(1)
    return r or defecto


def si_o_no(texto, defecto=True):
    d = "S/n" if defecto else "s/N"
    r = preguntar("%s (%s)" % (texto, d)).lower()
    if not r:
        return defecto
    return r.startswith("s")


def elegir_color(actual=""):
    print("\n  Color de la tira:")
    for i, (nombre, hexa) in enumerate(PALETA, 1):
        marca = "  ← el que tiene" if hexa == actual else ""
        print("    %d) %-9s %s%s" % (i, nombre, hexa, marca))
    while True:
        r = preguntar("  Número (o un color #RRGGBB)", "1" if not actual else "")
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", r or ""):
            return "#" + r.lstrip("#").upper()
        if r.isdigit() and 1 <= int(r) <= len(PALETA):
            return PALETA[int(r) - 1][1]
        if not r and actual:
            return actual
        print("  No entendí. Poné un número de la lista o un color tipo #E6A800.")


# ── La configuración ───────────────────────────────────────────────────────────
def config_vacia():
    """El primer agente es el de la casa, y su clave es `cacho` a propósito: la app se
    llama así, su cara es la que se ve en la pantalla de entrada y el programa la busca
    por nombre. El NOMBRE que se muestra sí se puede cambiar."""
    return {
        "empresa": "", "rubro": "",
        "agentes": [{
            "clave": "cacho", "nombre": "Cacho", "rol": "La casa",
            "gente": "vos y la máquina", "color": "#C96442", "cara": "cacho.png",
            "propias": ["cacho", "computadora", "sistema", "respaldo", "backup"],
            "palabras": ["servidor", "archivo", "carpeta", "clave", "correo",
                         "impresora", "sincronizar", "error"],
        }],
    }


def cargar():
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                c = json.load(fh)
            if c.get("agentes"):
                return c
        except (ValueError, OSError) as e:
            # Un JSON roto NO puede hacer que se pierda todo en silencio: se avisa y se
            # deja el archivo viejo a un costado antes de empezar de nuevo.
            print("⚠️  %s no se pudo leer (%s). Lo dejo como empresa.json.roto." %
                  (os.path.basename(CONFIG), e))
            try:
                os.replace(CONFIG, CONFIG + ".roto")
            except OSError:
                pass
    return config_vacia()


def guardar(cfg):
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


# ── Alta y edición de un agente ────────────────────────────────────────────────
def nuevo_agente(cfg, sugerido=""):
    usadas = {a["clave"] for a in cfg["agentes"]}
    print("\n─── Un agente nuevo ───")
    print("  Pensalo como una persona del equipo: ¿de qué se ocupa?")
    sector = preguntar("  Sector (Administración, Logística, Ventas…)", sugerido)
    if not sector:
        return None
    oficio = POR_OFICIO.get(clave_de(sector), {})

    clave = clave_de(sector)
    if clave in usadas:
        print("  Ya tenés un agente con ese nombre.")
        return None

    nombre = preguntar("  ¿Le ponés un nombre propio? (Enter = %s)" % sector, sector)
    rol = oficio.get("rol") or sector
    gente = preguntar("  ¿Con quién habla esta área?", oficio.get("gente", ""))
    color = elegir_color(oficio.get("color", ""))

    propias = list(oficio.get("propias", []))
    palabras = list(oficio.get("palabras", []))
    if not propias:
        # Un sector que no está en el catálogo: sin vocabulario, Cacho nunca le manda
        # una charla sola y la cara queda de adorno. Se piden las palabras AHORA.
        print("\n  No conozco ese sector, así que necesito unas palabras para reconocerlo.")
        print("  ¿Qué palabras aparecen en una charla de esta área y no en las otras?")
        print("  (separadas por coma — p. ej.: flete, remito, transportista)")
        crudo = preguntar("  Palabras")
        propias = [p.strip().lower() for p in crudo.split(",") if p.strip()]
    # El nombre propio y el del sector siempre reconocen a su área: «decile a Logística…»
    for p in (nombre, sector):
        if _sin_tildes(p) not in [_sin_tildes(x) for x in propias]:
            propias.append(p.lower())

    cara = clave + ".png"
    caras.png(caras.iniciales_de(nombre), color, os.path.join(STATIC, cara))
    return {"clave": clave, "nombre": nombre, "rol": rol, "gente": gente,
            "color": color, "cara": cara, "propias": propias, "palabras": palabras}


def editar_agente(a):
    print("\n─── %s (%s) ───" % (a["nombre"], a["rol"]))
    a["nombre"] = preguntar("  Nombre", a["nombre"])
    a["rol"] = preguntar("  Sector", a["rol"])
    a["gente"] = preguntar("  Con quién habla", a["gente"])
    a["color"] = elegir_color(a["color"])
    print("  Palabras que lo identifican: %s" % ", ".join(a["propias"][:8]))
    extra = preguntar("  ¿Agregás alguna? (coma, Enter para dejarlo así)")
    for p in extra.split(","):
        if p.strip() and p.strip().lower() not in a["propias"]:
            a["propias"].append(p.strip().lower())
    # La cara se regenera SALVO que sea una foto puesta a mano: si el archivo no es el
    # círculo que generamos (lo sabemos por el tamaño), no se pisa el trabajo de nadie.
    ruta = os.path.join(STATIC, a["cara"])
    propia = os.path.isfile(ruta) and os.path.getsize(ruta) > 20000
    if a["clave"] == "cacho":
        print("  (la cara de %s es la de la app, no se toca)" % a["nombre"])
    elif propia and not si_o_no("  Esa cara es una imagen tuya. ¿La reemplazo por el círculo?", False):
        pass
    else:
        caras.png(caras.iniciales_de(a["nombre"]), a["color"], ruta)
    return a


# ── Lo que se escribe ──────────────────────────────────────────────────────────
def escribir_areas(cfg):
    """Genera `areas.py`. El contrato lo fija `serve_sesiones.py`, que importa este
    módulo arriba de todo: si sale roto, Cacho no abre. Por eso al final se compila."""
    L = []
    w = L.append
    w("# -*- coding: utf-8 -*-")
    w('"""areas.py — las áreas de %s, con su cara y su color.' % (cfg["empresa"] or "tu empresa"))
    w("")
    w("LO ESCRIBIÓ `configurar.py`. Se puede editar a mano, pero si volvés a correr el")
    w("asistente se reescribe: lo que quieras que quede, contestáselo a él.")
    w("")
    w("EL ÁREA NO ES EL TEMA: ES CON QUIÉN HABLÁS. Definidas así no se pisan.")
    w("")
    w("LA CARA IDENTIFICA, EL COLOR REFUERZA. Nunca al revés y nunca el color solo: es lo")
    w("que hace que la tira siga sirviendo con sol de frente, en el celular, o para alguien")
    w("que no distingue rojo de verde.")
    w("")
    w("CLASIFICA SOLA. La máquina propone leyendo la charla —contando palabras, sin modelo:")
    w("es instantáneo y no puede inventar— y vos corregís de un toque. Lo que no reconoce")
    w("cae en la primera área, que es el cajón de lo transversal y no un tacho de descarte.")
    w('"""')
    w("from __future__ import annotations")
    w("")
    w("import re")
    w("import unicodedata")
    w("")
    w("# El orden es el de la tira, de arriba abajo. También desempata en `de_texto()`:")
    w("# es arbitrario, pero tiene que ser ESTABLE.")
    w("AREAS = [")
    for a in cfg["agentes"]:
        w("    {")
        w('        "clave": %r, "nombre": %r, "rol": %r,' % (a["clave"], a["nombre"], a["rol"]))
        w('        "gente": %r, "color": %r, "cara": %r,' % (a["gente"], a["color"], a["cara"]))
        w('        "propias": %r,' % (sorted(set(a["propias"])),))
        w('        "palabras": %r,' % (sorted(set(a["palabras"])),))
        w("    },")
    w("]")
    w("POR_CLAVE = {a[\"clave\"]: a for a in AREAS}")
    w("DEFECTO = %r" % cfg["agentes"][0]["clave"])
    w("# Cuánto hay que sumar para ganarle al default. Las palabras PROPIAS de un área")
    w("# valen 2 y deciden solas; las genéricas valen 1 y necesitan compañía. Sin esto,")
    w("# «arreglando lo de Ventas» sumaba 1 y caía en el default.")
    w("MINIMO = 2")
    w("PESO_PROPIA = 2")
    w('_RE_CLAVE = re.compile(r"[a-z]{3,12}")')
    w("")
    w("")
    w("def _norm(t: str) -> str:")
    w('    t = unicodedata.normalize("NFD", (t or "").lower())')
    w('    return "".join(c for c in t if unicodedata.category(c) != "Mn")')
    w("")
    w("")
    w('_INDICE = [(a["clave"],')
    w('            [(_norm(p), PESO_PROPIA) for p in a.get("propias", [])] +')
    w('            [(_norm(p), 1) for p in a.get("palabras", [])])')
    w("           for a in AREAS]")
    w("")
    w("")
    w("def de_texto(texto: str) -> str:")
    w('    """A qué área pertenece lo que se está haciendo. Siempre una clave válida.')
    w("")
    w("    Gana la que más veces aparece, y sólo si llega al mínimo; si no, es del")
    w("    default. Ante un empate gana el orden de `AREAS`: lo peor que puede hacer un")
    w('    clasificador es cambiar de opinión solo entre una lectura y la siguiente."""')
    w("    t = _norm(texto)")
    w("    if not t:")
    w("        return DEFECTO")
    w("    mejor, cuantas = DEFECTO, 0")
    w("    for clave, palabras in _INDICE:")
    w("        n = sum(t.count(p) * peso for p, peso in palabras)")
    w("        if n > cuantas:")
    w("            mejor, cuantas = clave, n")
    w("    return mejor if cuantas >= MINIMO else DEFECTO")
    w("")
    w("")
    w("def valida(clave: str) -> str | None:")
    w('    """La clave si existe; None si no. Es lo único que se acepta de afuera."""')
    w('    c = (clave or "").strip().lower()')
    w("    return c if _RE_CLAVE.fullmatch(c) and c in POR_CLAVE else None")
    w("")
    w("")
    w("def color(clave: str) -> str:")
    w('    return POR_CLAVE.get(clave or DEFECTO, POR_CLAVE[DEFECTO])["color"]')
    w("")
    w("")
    w("def para_el_front() -> list:")
    w('    """Lo que necesita el navegador para dibujar la tira: sin las palabras, que no')
    w('    le sirven y son cien líneas de vocabulario viajando en cada carga."""')
    w('    return [{k: a[k] for k in ("clave", "nombre", "rol", "gente", "color", "cara")}')
    w("            for a in AREAS]")
    w("")
    w("")
    w('if __name__ == "__main__":')
    w("    for p in [%s]:" % ", ".join(
        repr("una charla de " + a["rol"].lower()) for a in cfg["agentes"]))
    w('        print("  %-10s ← %s" % (de_texto(p), p))')
    ruta = os.path.join(AQUI, "areas.py")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return ruta


PLANTILLA_CLAUDE = """# CLAUDE.md — {empresa}

> Esto lo lee TODO agente de {empresa} al empezar cualquier charla. Es el mapa: qué es la
> empresa y quién se ocupa de qué. Cada byte de acá se relee en cada vuelta, así que
> escribí corto: el detalle vive en la memoria de cada área.

## Qué es esta empresa

{rubro}

## Quién se ocupa de qué

Cada área es **con quién hablás**, no un tema. Lo de un área se decide en su área:

<!-- agentes:inicio — esta lista la mantiene `configurar.py`; el resto del archivo es tuyo -->
{tabla}
<!-- agentes:fin -->

## Cómo se trabaja acá

- **Verificá antes de afirmar.** Nada de «debería andar». Si tocaste algo, corrélo y
  mostrá la salida real. Si falló, decilo con el error a la vista.
- **Los datos se leen de la fuente, nunca de memoria.** Si no tenés el dato, buscalo; no
  lo estimes ni lo inventes.
- **Un cambio de cara al público se confirma antes** (mandar un mensaje, publicar, borrar,
  mandar un mail). Que te lo hayan aprobado una vez no vale para la próxima.
- **Se dice lo que NO se hizo.** Un trabajo entregado a medias sin avisar es peor que uno
  no empezado.
- Si un pedido se apoya en algo falso, decilo en vez de complacer.

## Dónde vive lo que cada uno aprende

`.claude/memory/<área>/MEMORY.md` — el índice de cada agente. Una regla del área va ahí;
acá arriba sólo lo que vale para TODOS.
"""

PLANTILLA_MEMORIA = """> Índice de **{nombre}** — {rol}. Habla con: {gente}.

Acá va, en una línea cada una, las cosas que {nombre} aprende y no se pueden olvidar:
cómo se hace algo, con quién se chequea, qué NO hay que hacer.

Formato: una línea por regla, la importante primero.

- (todavía no hay nada anotado)
"""


def escribir_proyecto(cfg):
    """La carpeta donde nacen las charlas. Cacho lista `~/Claude/Projects/*`: si esto no
    existe, la ventana abre pero el ＋ no tiene dónde crear la pestaña."""
    nombre = cfg["empresa"] or "Mi empresa"
    carpeta = os.path.join(CARPETA_PROYECTOS, nombre)
    os.makedirs(carpeta, exist_ok=True)

    tabla = "\n".join(
        "- **%s** (%s) — %s" % (a["nombre"], a["rol"], a["gente"] or "lo suyo")
        for a in cfg["agentes"])
    md = os.path.join(carpeta, "CLAUDE.md")
    if not os.path.isfile(md):
        with open(md, "w", encoding="utf-8") as fh:
            fh.write(PLANTILLA_CLAUDE.format(
                empresa=nombre,
                rubro=cfg["rubro"] or "(contá en dos líneas a qué se dedica)",
                tabla=tabla))
    else:
        # El CLAUDE.md es de ellos desde el día 2: lo que escribieron NO se pisa. Pero la
        # lista de agentes SÍ se mantiene al día, o el día que agregan «Taller» el archivo
        # que leen todos los agentes sigue diciendo que son cuatro. Por eso la lista vive
        # entre marcadores y es lo único que se reemplaza.
        try:
            with open(md, encoding="utf-8") as fh:
                texto = fh.read()
        except OSError as e:
            print("   ⚠️  No pude leer %s (%s): la lista de agentes quedó vieja." % (md, e))
            return carpeta
        bloque = re.search(r"(<!-- agentes:inicio.*?-->\n)(.*?)(\n<!-- agentes:fin -->)",
                           texto, re.S)
        if not bloque:
            # Le sacaron los marcadores: es SU archivo, no se adivina dónde iba la lista.
            print("   · CLAUDE.md: no encontré la marca de la lista de agentes, no lo toco.")
            print("     Agregá a mano los que falten: %s" % md)
        elif bloque.group(2) == tabla:
            pass
        else:
            with open(md, "w", encoding="utf-8") as fh:
                fh.write(texto[:bloque.start(2)] + tabla + texto[bloque.end(2):])
            print("   · CLAUDE.md: actualicé la lista de agentes (lo demás quedó igual)")

    for a in cfg["agentes"]:
        d = os.path.join(carpeta, ".claude", "memory", a["clave"])
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "MEMORY.md")
        if not os.path.isfile(f):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(PLANTILLA_MEMORIA.format(
                    nombre=a["nombre"], rol=a["rol"], gente=a["gente"] or "lo suyo"))
    return carpeta


# ── El menú ────────────────────────────────────────────────────────────────────
def mostrar(cfg):
    print("\n  Los agentes de %s:" % (cfg["empresa"] or "tu empresa"))
    for i, a in enumerate(cfg["agentes"], 1):
        print("   %d) %-14s %-18s habla con %s" %
              (i, a["nombre"], "(" + a["rol"] + ")", a["gente"] or "—"))


def elegir_agente(cfg, verbo):
    mostrar(cfg)
    r = preguntar("  ¿Cuál querés %s? (número, Enter para volver)" % verbo)
    if r.isdigit() and 1 <= int(r) <= len(cfg["agentes"]):
        return int(r) - 1
    return None


def main():
    print("\n" + "=" * 68)
    print("  Cacho — configurar los agentes de tu empresa")
    print("=" * 68)

    cfg = cargar()
    primera = not cfg["empresa"]

    cfg["empresa"] = preguntar("\n¿Cómo se llama la empresa?", cfg["empresa"])
    print("\n¿A qué se dedica? Dos líneas alcanzan — es lo que van a saber todos los")
    print("agentes antes de contestar nada.")
    cfg["rubro"] = preguntar("  En qué anda", cfg["rubro"])

    if primera:
        print("\nTe propongo arrancar con tres sectores: Administración, Logística y Ventas.")
        print("Después agregás o sacás los que quieras.")
        if si_o_no("¿Los creo?"):
            for s in ("Administración", "Logística", "Ventas"):
                o = POR_OFICIO[clave_de(s)]
                clave = clave_de(s)
                cara = clave + ".png"
                caras.png(caras.iniciales_de(s), o["color"], os.path.join(STATIC, cara))
                cfg["agentes"].append({
                    "clave": clave, "nombre": s, "rol": o["rol"], "gente": o["gente"],
                    "color": o["color"], "cara": cara,
                    "propias": o["propias"], "palabras": o["palabras"]})

    while True:
        mostrar(cfg)
        print("\n  1) Agregar un agente     2) Cambiarle algo a uno")
        print("  3) Borrar uno            4) Guardar y salir")
        op = preguntar("\n  Qué hacés", "4")
        if op == "1":
            if len(cfg["agentes"]) >= 7:
                print("\n  ⚠️  Ya tenés siete. Más de siete caras de color dejan de servir")
                print("      para razonar: conviene sacar una antes de sumar otra.")
                if not si_o_no("  ¿Sumás una igual?", False):
                    continue
            a = nuevo_agente(cfg)
            if a:
                cfg["agentes"].append(a)
                guardar(cfg)
        elif op == "2":
            i = elegir_agente(cfg, "cambiar")
            if i is not None:
                cfg["agentes"][i] = editar_agente(cfg["agentes"][i])
                guardar(cfg)
        elif op == "3":
            i = elegir_agente(cfg, "borrar")
            if i == 0:
                print("\n  Ese es el de la casa: es donde cae lo que no es de nadie. No se borra.")
            elif i is not None:
                a = cfg["agentes"].pop(i)
                print("  Borrado: %s. (Las charlas que tenía quedan en el agente de la casa.)"
                      % a["nombre"])
                guardar(cfg)
        elif op == "4":
            break
        else:
            print("  No entendí.")

    guardar(cfg)
    ruta = escribir_areas(cfg)

    # El server importa `areas.py` arriba de todo: un archivo roto acá es un Cacho que no
    # abre. Se compila Y se corre antes de decir que está listo.
    import py_compile
    import subprocess
    try:
        py_compile.compile(ruta, doraise=True)
        subprocess.run([sys.executable, ruta], check=True,
                       stdout=subprocess.DEVNULL, cwd=AQUI)
    except (py_compile.PyCompileError, subprocess.CalledProcessError) as e:
        print("\n❌ El areas.py generado no corre: %s" % e)
        print("   No se cambió nada más. Avisá con este mensaje.")
        return 1

    carpeta = escribir_proyecto(cfg)

    print("\n" + "=" * 68)
    print("  Listo.")
    print("=" * 68)
    print("   · areas.py            %d agentes" % len(cfg["agentes"]))
    print("   · static/             la cara de cada uno")
    print("   · %s" % carpeta)
    print("        CLAUDE.md        lo que todos saben de la empresa")
    print("        .claude/memory/  lo que cada uno va aprendiendo")
    print("\n  Abrí Cacho y vas a ver la tira de caras a la izquierda.")
    print("  Para cambiar algo, volvé a correr:  python3 configurar.py")
    if os.path.exists(os.path.expanduser("~/Applications/Cacho.app")):
        print("\n  (si Cacho ya estaba abierto, cerralo y abrilo de nuevo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

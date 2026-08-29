# -*- coding: utf-8 -*-
"""areas.py — las áreas de tu operación, con su cara y su color.

(Esta es la versión PÚBLICA del módulo: `publicar_cacho.py` la copia al repo público
como `areas.py`. La privada —`tools/areas.py`— es la de la casa; las dos comparten el
contrato completo: mismas claves, mismas funciones, mismos pesos.)

LA IDEA. Cuando trabajás cosas muy distintas sobre la misma terminal, cuesta enfocar.
Cacho agrupa las sesiones por ÁREA, como si cada una fuera una persona del equipo:
tocás una cara en la tira y ves solo lo de esa área.

EL ÁREA NO ES EL TEMA: ES **CON QUIÉN HABLÁS**. Definidas así son mutuamente
excluyentes y no hay que discutir dos veces dónde va cada cosa:

    Cacho    → vos y la máquina (el repo, la infra, lo que no le sirve a nadie de afuera)
    Carla    → los clientes            Jaime  → el mercado
    Eterna   → los locales             Xara   → proveedores y bancos
    Waldemar → la mercadería (qué comprás, a quién, a cuánto, qué rota)

Es un EJEMPLO armado para un comercio con locales: editá nombres, colores, caras y
vocabulario a gusto (las caras viven en `static/`). Regla que conviene respetar:
**seis es el techo** — razonar con categorías de color se cae después de 8 y la guía
de interfaces dice 3 a 5. Si te hace falta una séptima, sacá otra.

LA CARA IDENTIFICA, EL COLOR REFUERZA. Nunca al revés y nunca el color solo. Es lo que
hace que esto siga funcionando con sol de frente, en el celular, o para alguien que no
distingue rojo de verde.

CLASIFICA SOLA, Y ESO NO ES UN LUJO. Los sistemas de etiquetar a mano se mueren solos:
las etiquetas solo funcionan si alguien las pone completas y para siempre, nadie lo
hace, y el sistema se degrada en silencio. Por eso acá la máquina propone y vos
corregís de un toque — nunca al revés. Lo que no reconoce va a Cacho, que es el cajón
de lo transversal y no una categoría de descarte.

El clasificador es **deliberadamente tonto**: contar palabras, sin modelo. Corre en
cada lectura de transcript, tiene que ser instantáneo y sobre todo NO puede inventar.
"""
from __future__ import annotations

import re
import unicodedata

# El orden es el de la tira, de arriba abajo. Cacho primero: es la casa y el «ver
# todo». OJO: este orden también desempata en `de_texto()`; moverlo cambia a quién le
# toca un texto que da igual en dos áreas. Es arbitrario pero tiene que ser ESTABLE.
AREAS = [
    {
        "clave": "cacho", "nombre": "Cacho", "rol": "Dirección",
        "gente": "vos y la máquina", "color": "#C96442", "cara": "cacho.png",
        # Cacho es el DEFAULT: lo que no reconoce nadie cae acá. Estas palabras están
        # para que lo suyo gane cuando además aparece vocabulario de otra área de pasada.
        "propias": ["launchd", "plist", "cacho", "tailscale", "policía", "policia",
                    "dashboard", "terminal"],
        "palabras": ["cron", "servidor", "puerto", "repo", "commit", "git ", "refactor",
                     "auditor", "memoria ram", "disco", "backup", "respaldo", "sync"],
    },
    {
        "clave": "carla", "nombre": "Carla", "rol": "Atención al cliente",
        "gente": "los clientes", "color": "#C2418C", "cara": "carla.png",
        # «whatsapp» sola no alcanza: por WhatsApp también salen informes internos, que
        # son de Eterna. Lo que distingue a Carla es el CLIENTE.
        "propias": ["carla", "reseña", "resena", "call center",
                    "atención al cliente", "atencion al cliente"],
        "palabras": ["cliente escribió", "consulta de un cliente", "responder al cliente",
                     "los clientes"],
    },
    {
        "clave": "jaime", "nombre": "Jaime", "rol": "Marketing",
        "gente": "el mercado", "color": "#0072CE", "cara": "jaime.png",
        "propias": ["jaime", "pauta", "creativ", "publicid", "tiktok", " ads", "seo",
                    "campaña", "campana de"],
        "palabras": ["anuncio", "posteo", "reel", "audiencia", "instagram", "blog",
                     "landing", "carrusel"],
    },
    {
        "clave": "eterna", "nombre": "Eterna", "rol": "Operaciones",
        "gente": "los locales", "color": "#2E8B57", "cara": "eterna.png",
        # OJO: «local» a secas NO va. Es palabra comodín (aparece en «servidor local»,
        # «archivo local») y ensucia la clasificación: las palabras muy comunes hacen
        # que el área aparezca donde no va.
        "propias": ["eterna", "encargado", "vendedor", "mostrador", "capacitación",
                    "capacitacion", "comisión", "comision"],
        "palabras": ["locales", "sucursal", "stock", "visita al local",
                     "reunión de encargados"],
    },
    {
        "clave": "waldemar", "nombre": "Waldemar", "rol": "Producto",
        "gente": "la mercadería", "color": "#7A5AF8", "cara": "waldemar.png",
        # No se pisa con Eterna ni con Xara aunque compartan vocabulario: «stock» a
        # secas sigue siendo del mostrador (Eterna) y «proveedor» del que hay que
        # pagarle (Xara). Lo de Waldemar es el VERBO DE COMPRA.
        "propias": ["waldemar", "reposicion", "reposición", "surtido", "sobrestock",
                    "rotacion", "rotación", "stock muerto", "lista de precios",
                    "mayorista", "coleccion", "colección", "que comprar", "qué comprar"],
        "palabras": ["comprar", "compra de", "catalogo", "catálogo", "importacion",
                     "importación", "temporada", "liquidar", "outlet",
                     "cobertura de stock", "producto nuevo"],
    },
    {
        "clave": "xara", "nombre": "Xara", "rol": "Administración",
        "gente": "proveedores y bancos", "color": "#B8860B", "cara": "xara.png",
        "propias": ["xara", "bancari", "contab", "cobranza", "flujo de caja",
                    "estado de cuenta", "proveedor"],
        "palabras": ["banco", "factura", "iva", "saldo", "balance", "boleta", "cheque",
                     "gasto"],
    },
]
POR_CLAVE = {a["clave"]: a for a in AREAS}
DEFECTO = "cacho"
# Cuánto hay que sumar para ganarle al default. Las palabras PROPIAS de un área —su
# nombre, «pauta», «bancari»— valen 2 y por lo tanto deciden solas; las genéricas valen
# 1 y necesitan compañía. Sin esto, «arreglando lo de Carla» sumaba 1 y caía en Cacho.
# Un nombre propio no puede pesar lo mismo que «banco».
MINIMO = 2
PESO_PROPIA = 2
_RE_CLAVE = re.compile(r"[a-z]{3,12}")


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


_INDICE = [(a["clave"],
            [(_norm(p), PESO_PROPIA) for p in a.get("propias", [])] +
            [(_norm(p), 1) for p in a.get("palabras", [])])
           for a in AREAS]


def de_texto(texto: str) -> str:
    """A qué área pertenece lo que se está haciendo. Siempre devuelve una clave válida.

    Gana la que más veces aparece, y sólo si llega al mínimo; si no, es de Cacho. Ante
    un empate gana el orden de `AREAS` — es arbitrario, pero es ESTABLE: lo peor que
    puede hacer un clasificador es cambiar de opinión solo entre una lectura y la
    siguiente."""
    t = _norm(texto)
    if not t:
        return DEFECTO
    mejor, cuantas = DEFECTO, 0
    for clave, palabras in _INDICE:
        n = sum(t.count(p) * peso for p, peso in palabras)
        if n > cuantas:
            mejor, cuantas = clave, n
    return mejor if cuantas >= MINIMO else DEFECTO


def valida(clave: str) -> str | None:
    """La clave si existe; None si no. Es lo único que se acepta de afuera."""
    c = (clave or "").strip().lower()
    return c if _RE_CLAVE.fullmatch(c) and c in POR_CLAVE else None


def color(clave: str) -> str:
    return POR_CLAVE.get(clave or DEFECTO, POR_CLAVE[DEFECTO])["color"]


def para_el_front() -> list:
    """Lo que necesita el navegador para dibujar la tira: sin las palabras, que no le
    sirven y son 90 líneas de vocabulario viajando en cada carga."""
    return [{k: a[k] for k in ("clave", "nombre", "rol", "gente", "color", "cara")}
            for a in AREAS]


if __name__ == "__main__":
    pruebas = [
        "subiendo los creativos de la campaña a Meta Ads",
        "arreglando el sync de launchd que quedó con error en el servidor",
        "capacitación de los vendedores del mostrador",
        "los costos bancarios y la factura del proveedor",
        "Carla, hay que responder al cliente de la reseña",
        "me pasaron la lista de precios del mayorista, qué comprar",
        "cuánto stock muerto tenemos y qué liquidar",
        "hola",
    ]
    for p in pruebas:
        print("  %-8s ← %s" % (de_texto(p), p))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""caras.py — dibuja la cara de un agente: un círculo de color con sus iniciales.

Por qué existe: en la tira lateral, LA CARA IDENTIFICA Y EL COLOR REFUERZA (nunca el
color solo). Un agente sin imagen deja la tira con un cuadrito roto, así que cada agente
que se crea desde `configurar.py` nace con una cara aunque nadie tenga un dibujo a mano.
Después se reemplaza por una foto o un dibujo de verdad: es un PNG común en `static/`.

Sin dependencias, como todo Cacho: escribe el PNG a mano (zlib + struct) y trae su propia
tipografía de 5x7 puntos. Nada de Pillow, que en una Mac recién instalada no está.

Uso:
    python3 caras.py AD "#E6A800" static/administracion.png
"""
import os
import struct
import sys
import zlib

LADO = 256          # el front la muestra chica; 256 aguanta pantalla Retina

# Tipografía de 5x7 puntos. Sólo mayúsculas y dígitos: una cara lleva 1 o 2 iniciales.
FUENTE = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "Ñ": ["01010", "00000", "10001", "11001", "10101", "10011", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def _rgb(hexa):
    """'#E6A800' -> (230, 168, 0). Un color raro no puede voltear el asistente: gris."""
    h = (hexa or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (120, 120, 120)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (120, 120, 120)


def _claro(rgb):
    """Luminancia percibida (ITU-R BT.601). Sobre un amarillo, tinta NEGRA; sobre un
    azul, blanca. Sin esto una cara amarilla con letras blancas no se lee."""
    r, g, b = rgb
    return (r * 299 + g * 587 + b * 114) / 1000 > 150


def png(iniciales, color, destino):
    """Escribe el PNG y devuelve la ruta. `iniciales`: 1 o 2 caracteres."""
    fondo = _rgb(color)
    tinta = (0, 0, 0) if _claro(fondo) else (255, 255, 255)
    letras = [c for c in (iniciales or "?").upper()[:2] if c in FUENTE] or ["?"]
    if letras == ["?"]:
        letras = ["A"]

    # El lienzo arranca transparente y se le pinta el círculo: así la cara queda
    # redonda de verdad y no un cuadrado, se la ponga sobre el fondo que se la ponga.
    filas = [[(0, 0, 0, 0)] * LADO for _ in range(LADO)]
    c = (LADO - 1) / 2.0
    radio = LADO / 2.0 - 1
    for y in range(LADO):
        for x in range(LADO):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= radio - 1:
                filas[y][x] = fondo + (255,)
            elif d <= radio:                       # borde suavizado: 1 px de alfa
                filas[y][x] = fondo + (int(255 * (radio - d)),)

    # Las iniciales, centradas. El punto de la tipografía se escala para que el texto
    # ocupe ~la mitad del círculo (queda legible incluso en el renglón chico del celular).
    ancho_txt = len(letras) * 5 + (len(letras) - 1) * 2  # 2 puntos de separación
    punto = int(LADO * 0.46 / ancho_txt)
    x0 = int((LADO - ancho_txt * punto) / 2)
    y0 = int((LADO - 7 * punto) / 2)
    for i, letra in enumerate(letras):
        mapa = FUENTE[letra]
        ox = x0 + i * 7 * punto
        for fy, linea in enumerate(mapa):
            for fx, bit in enumerate(linea):
                if bit != "1":
                    continue
                for py in range(punto):
                    for px in range(punto):
                        x, y = ox + fx * punto + px, y0 + fy * punto + py
                        if 0 <= x < LADO and 0 <= y < LADO:
                            filas[y][x] = tinta + (255,)

    crudo = b"".join(b"\x00" + bytes(v for p in fila for v in p) for fila in filas)

    def trozo(tipo, datos):
        return (struct.pack(">I", len(datos)) + tipo + datos +
                struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF))

    cuerpo = (b"\x89PNG\r\n\x1a\n" +
              trozo(b"IHDR", struct.pack(">IIBBBBB", LADO, LADO, 8, 6, 0, 0, 0)) +
              trozo(b"IDAT", zlib.compress(crudo, 9)) +
              trozo(b"IEND", b""))
    carpeta = os.path.dirname(os.path.abspath(destino))
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(destino, "wb") as fh:
        fh.write(cuerpo)
    return destino


def iniciales_de(nombre):
    """«Administración» -> «AD» · «Casa Central» -> «CC». Dos palabras, dos letras."""
    partes = [p for p in (nombre or "").split() if p]
    if len(partes) >= 2:
        return (partes[0][0] + partes[1][0]).upper()
    if partes:
        return partes[0][:2].upper()
    return "??"


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(2)
    print("OK:", png(sys.argv[1], sys.argv[2], sys.argv[3]))

# Cacho — guía para usarlo

Cacho es **una sola ventana con todas tus charlas de Claude**. En vez de tener seis
Terminales abiertas sin saber cuál está trabajando y cuál te está esperando desde hace
dos horas, tenés una lista a la izquierda que te lo dice.

Y arriba de eso: **cada charla pertenece a un sector de tu empresa**, con su cara y su
color. Tocás una cara y ves sólo lo de ese sector.

---

## Instalarlo

Doble clic en **`INSTALAR.command`**. Te va a preguntar el nombre de la empresa, a qué se
dedica, y te propone arrancar con tres agentes: Administración, Logística y Ventas.

Si preferís que no pregunte nada (o si se lo pedís a un asistente que no tiene a quién
preguntarle), se le dice todo de una:

```bash
bash instalar.sh --sin-preguntar \
  --empresa "Mi empresa SA" \
  --sectores "Administración,Logística,Ventas" \
  --perfil ~/Downloads/mi-empresa.md
```

`--perfil` es un archivo de texto que contás **a qué se dedica la empresa y qué es lo que
se puede arruinar**. Entra entero al `CLAUDE.md` que leen todos los agentes, y es la
diferencia entre un asistente que contesta genérico y uno que sabe de qué vivís. Si no
tenés uno a mano, `--rubro "una línea"` alcanza para arrancar.

Cuando termina queda **Cacho.app** en `~/Applications`. Arrastrala al Dock.

---

## El día a día

### Empezar a trabajar con un sector

1. Abrí Cacho.
2. En la tira de la izquierda, tocá la cara del sector (por ejemplo **Administración**).
3. Apretá **＋**. Se abre una charla nueva, ya marcada como de ese sector.
4. Escribile como le escribirías a una persona: *«mirá estas facturas y decime cuáles
   vencen esta semana»*.

### Volver a una charla de ayer

Las charlas terminadas quedan plegadas abajo. Tocás una y se reabre **donde quedó**: se
acuerda de todo lo que hablaron.

### Saber qué está pasando sin abrir nada

Cada renglón de la lista te dice tres cosas:

- **de qué va** la charla,
- **qué está haciendo ahora** (por ejemplo: *leyendo planilla-setiembre.xlsx*),
- **lo último que vos pediste** — no el último mensaje, que casi siempre es «dale».

Un **🔔** significa que esa charla te está esperando a vos. Además suena y te avisa el
sistema.

### El semáforo de cada charla

Al lado de cada una hay un indicador de cuánto **pesa**. En cada vuelta la charla se
relee entera, así que una charla muy larga se vuelve lenta y cara. Cuando el semáforo se
pone feo, conviene cerrar esa y abrir una nueva.

### Lo demás que está ahí

| Para | Cómo |
|---|---|
| Buscar una charla | El buscador arriba de la lista, o **⌘K** |
| Que una charla no se te pierda | **Fijarla**: sube al tope y se queda |
| Ponerle nombre propio a una charla | Tocás el título y lo escribís |
| Dictar en vez de escribir | El botón **🎙** |
| Pegar una captura de pantalla | **⌘V** adentro de la charla |
| Mandarle un archivo | El **＋** de la charla |

### Desde el teléfono

Si tenés Tailscale instalado en la Mac y en el celular, la misma ventana se abre en el
teléfono. Sirve para mirar desde afuera si aquello que dejaste corriendo ya terminó.

---

## Los agentes

### Cambiar, agregar o sacar un sector

Abrí la Terminal y pegá esto:

```bash
cd ~/Claude/cacho && python3 configurar.py
```

Te deja agregar un agente, cambiarle el nombre o el color, o borrarlo. Después **cerrá
Cacho y abrilo de nuevo** para ver el cambio.

> **Siete es el techo.** Más de siete caras de colores dejan de servir para orientarse de
> un vistazo. Si te hace falta una octava, conviene sacar otra antes.

### El área no es el tema: es con quién hablás

Es la regla que hace que nunca haya que discutir dos veces dónde va una charla:

- **Administración** → con quien lleva la plata y los papeles.
- **Logística** → con quien mueve la mercadería.
- **Ventas** → con quien atiende al cliente.

Definidos así no se pisan. Si los definís por tema (*«lo de los precios»*) vas a tener la
misma charla peleada entre dos sectores para siempre.

### Cómo decide Cacho a qué sector va cada charla

Contando palabras: si aparece *flete*, *remito* o *transportista*, es de Logística. **No
usa inteligencia artificial para esto a propósito** — así es instantáneo y, sobre todo,
no puede inventar. Lo que no reconoce cae en el primer agente, que es el cajón de lo
transversal, no un tacho de descarte.

Si se equivoca, **lo corregís de un toque** en la charla y listo.

---

## Enseñarle cosas a un agente

Esto es lo que hace que sirva de verdad en vez de ser un chat más.

**Lo que sabe TODA la empresa** (quién es quién, cómo se trabaja) vive en:

```
~/Claude/Projects/<tu empresa>/CLAUDE.md
```

**Lo que sabe cada sector** —cómo se hace algo, con quién se chequea, qué NO hay que
hacer— vive en:

```
~/Claude/Projects/<tu empresa>/.claude/memory/<sector>/MEMORY.md
```

Son archivos de texto común: los abrís y escribís. Una línea por cosa, la importante
primero. Ejemplo de una línea buena en la memoria de Logística:

> - Los envíos al interior se despachan **antes de las 14:00** o quedan para el otro día.

Lo más cómodo es no escribirlos a mano: cuando un agente aprenda algo que no se puede
olvidar, decile **«anotá esto en tu memoria»**.

---

## El policía

Antes de que algo que se cambió se empiece a usar, conviene que lo lea alguien que no lo
escribió. Eso hace `revisar.py`:

```bash
cd ~/Claude/cacho && python3 revisar.py            # lo que cambió en esta carpeta
python3 revisar.py ~/donde/sea/archivo.py          # un archivo cualquiera
```

Te marca en 🔴 lo que está mal y se va a notar —cuentas equivocadas, algo que rompe, un
error que se traga en silencio, una contraseña adentro del código— y en 🟡 lo que va a
confundir dentro de tres meses.

**El que revisa no escribe.** El policía no toca ni un archivo: te muestra lo que
encontró y arreglarlo lo decidís vos. Si no encuentra nada, lo dice y se calla; no
inventa para justificar la pasada.

---

## Si algo no anda

| Pasa esto | Hacé esto |
|---|---|
| Apretás el ícono y no abre nada | Esperá 5 segundos: la primera vez levanta el motor |
| Te pide un **PIN** | Está en la Mac, en el archivo `~/.cacho_pin` |
| Cambiaste un agente y no lo ves | Cerrá Cacho y abrilo de nuevo |
| Una cara aparece rota | Corré de nuevo `python3 configurar.py` |
| No abre de ninguna manera | Miralo con `cat ~/Library/Logs/cacho-server.log` |

**Cerrar la ventana no mata nada.** Las charlas viven en la Mac, no en la ventana: una
tarea que dejaste corriendo te sigue esperando cuando volvés a abrir.

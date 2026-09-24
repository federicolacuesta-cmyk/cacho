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

## Cómo conectarle información

Enseñarle cómo se trabaja (lo de arriba) es la mitad. La otra mitad es **de dónde saca los
datos**: un agente sin los números de tu empresa es un chat más.

Hay cuatro puertas. Conviene recorrerlas en este orden, que es el de menos a más trabajo:

| Puerta | Qué es | Cuánto cuesta |
|---|---|---|
| **1. Carpetas** | Los archivos de tu Mac | Nada, ya funciona |
| **2. MCP** | El conector de una app conocida | Un rato, y sin Terminal |
| **3. API** | La puerta de tus sistemas propios | Días, y alguien que lo arme |
| **4. Base en espejo** | Una copia de la base del sistema | Lo mismo, una sola vez |

### 1. Las carpetas de tu Mac — ya la tenés

Todo lo que esté en el disco, el agente lo abre solo: Excel, PDF, fotos, mails guardados.
No hay nada que configurar. Y **Dropbox, Drive o OneDrive sincronizados son una carpeta
más**: lo que se baja a la máquina se lee igual que el resto.

Esta puerta es el 80% del valor del primer mes. Lo único que conviene hacer es ordenar:
una carpeta por tema y decirle en cuál mirar.

> Si un archivo te llega todos los meses, guardalo siempre en la misma carpeta y con el
> mismo criterio de nombre (`2026-09 factura proveedor.pdf`). Un agente no adivina dónde
> está algo, pero un orden lo entiende enseguida.

### 2. MCP: el enchufe de una app

Un **MCP** es un conector ya hecho para una aplicación conocida — Gmail, Google Drive,
Calendar, Notion, Slack, tu tienda online. Se conecta una vez con el usuario de la empresa
y desde ahí el agente entra solo a buscar: vos no le copiás ni le pegás nada.

La diferencia con la puerta 1: en vez de mostrarle un papel, le das la llave del archivo.

**La forma fácil, sin Terminal:** los conectores de tu cuenta de Claude. En
[claude.ai](https://claude.ai) → **Configuración → Conectores**, los prendés con un clic y
entrás con la cuenta de la empresa. Quedan disponibles en las charlas de Cacho.

**Para lo que no esté en esa lista**, desde la Terminal:

```bash
claude mcp list                      # los que ya tenés, y si están andando
claude mcp add --help                # cómo agregar uno nuevo
claude mcp add-from-claude-desktop   # traer los que ya usabas en la app de escritorio
```

> **Poné sólo los que el sector usa.** Un agente con quince conectores tarda más y se
> dispersa; uno con los tres que necesita va al grano.

### 3. API: la puerta de tus sistemas

El sistema con el que facturás, el ERP, el que maneja la operación, el banco. Eso no tiene
conector hecho — y es justamente donde están los datos que no tiene nadie más. Por eso es
la puerta que cambia decisiones.

El camino es más corto de lo que parece:

1. Preguntale al proveedor del sistema dos cosas: **«¿tiene API?»** y **«¿me pueden dar
   acceso de lectura a la base?»**.
2. Te va a dar una **clave** (la va a llamar *API key* o *token*) y un manual.
3. Alguien escribe el pedacito de código que trae los datos y los deja en un archivo. Ese
   alguien puede ser el propio agente: pegale el manual que te pasaron y pedíselo.

**Que sea de sólo lectura.** Para mirar y sacar conclusiones alcanza con leer; permiso de
escritura es riesgo sin beneficio hasta que sepas exactamente qué querés que escriba.

### 4. La base de datos, en espejo

Si el sistema tiene base propia, lo mejor no es leerla en vivo: es **copiarla a la Mac una
vez por noche y leer la copia**. Dos razones, las dos importantes: ninguna equivocación del
agente puede tocar lo que está funcionando, y una consulta pesada no le frena el sistema a
la gente que está trabajando.

Lo que sí hay que tener claro es hasta cuándo llega la copia. Si se hace a las 3 AM, el
agente sabe hasta ayer — y tiene que decirlo cuando conteste, no dar el número de hoy como
si lo tuviera.

### Las claves no van en el chat

Una clave de API pegada en una conversación queda escrita ahí para siempre. Van en un
archivo de la máquina, y el código la busca ahí. Lo mismo para contraseñas y accesos a
bancos. Si una se te escapó en un chat, pedile al proveedor que la dé de baja y sacá otra:
lleva cinco minutos.

### La regla que ahorra tres meses

**No conectes todo de una.** Elegí UN proceso que tenga dos cosas juntas: plata visible y
datos a mano. Conectale lo que ese proceso necesita y usalo 30 días. Recién ahí vas a saber
qué pedirle de verdad, y el segundo proceso sale en una semana.

Conectar diez fuentes el primer día termina siempre igual: un asistente impresionante que
no cambió ninguna decisión.

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

# DXSpot Agregator

Agregador Telnet de información DX. Mantiene conexiones compartidas con tres
fuentes RBN y una conexión DXCluster independiente por cada cliente, combina
sus líneas en tiempo real y las ofrece mediante un servidor Telnet propio:

- un nodo DXCluster;
- un RBN de CW;
- un RBN digital;
- un RBN local.

Las líneas se entregan sin prefijos ni modificaciones de protocolo para que el
flujo resultante siga siendo utilizable por clientes DXCluster. La procedencia
no se incrusta en el flujo agregado.

## Características

- Un indicativo común para las conexiones RBN compartidas.
- Una conexión DXSPOT por cliente, identificada con su indicativo y el
  SSID reservado `-77`.
- Keepalive configurable contra todas las fuentes.
- Reconexión permanente con espera exponencial configurable.
- Negociación Telnet básica en ambos sentidos.
- Keepalive de clientes iniciado por el propio cliente, sin bytes Telnet
  inyectados por el servidor.
- Difusión en tiempo real a todos los clientes conectados.
- Cola independiente por cliente; un consumidor bloqueado no frena al resto.
- Dashboard con estado, dirección, puerto, tasa, última recepción,
  reconexiones y clientes.
- Dashboard web de solo lectura con tablas, gráficas y actividad del sistema
  actualizadas en tiempo real.
- Colores de estado y una vista de actividad común para los eventos relevantes.
- Descarga automática de `CTY.DAT`, resolución interna del país del spotter y
  del DX, y caché local para arranques sin conexión.
- Agrupamiento Dinámico de Spots (ADS) configurable por cliente para reducir
  duplicados de RBN después de aplicar sus filtros.
- Sin dependencias externas; requiere Python 3.10 o posterior.

## Arquitectura

El lanzador `dxspot_agregator.py` conserva el comando de ejecución original,
pero la implementación está dividida en el paquete `dxspot`. También puede
arrancarse con `python3 -m dxspot`:

- `application.py`: ciclo de vida y coordinación de componentes.
- `network.py`: conexiones upstream y sesiones de clientes Telnet.
- `telnet.py`: negociación, escritura y cierre del protocolo Telnet.
- `config.py`: modelos y validación de `config.json`.
- `models.py`: estado de sockets, clientes, spots y filtros.
- `commands.py`: interpretación y respuestas de comandos `dxa`.
- `profiles.py`: carga y guardado atómico de perfiles por indicativo.
- `countries.py`: descarga, caché y resolución de `CTY.DAT`.
- `dashboard.py`: composición, color y adaptación del dashboard.
- `constants.py`: fuentes, etiquetas, bandas y expresiones compartidas.
- `cli.py`: argumentos, señales y arranque de la aplicación.
- `__main__.py`: ejecución directa del paquete.

Los módulos de protocolo y dominio no dependen del dashboard ni del CLI. Esto
permite ampliar filtros, formatos de visualización o almacenamiento sin volver
a concentrar esas responsabilidades en el servidor.

## Configuración

Crea la configuración real:

```bash
cp config.example.json config.json
```

Edita `login`, direcciones, puertos y comandos iniciales. `login` se utiliza
para las conexiones RBN compartidas. Las cuatro claves de fuente son fijas:
`dxcluster`, `rbn_cw`, `rbn_digital` y `rbn_local`.

### Países de spotter y DX

Al arrancar se comprueba la versión vigente de `CTY.DAT` publicada por Amateur
Radio Country Files:

```json
"country_file": {
  "enabled": true,
  "url": "https://www.country-files.com/cty/cty.dat",
  "cache_path": "data/cty.dat",
  "download_timeout_seconds": 15,
  "update_interval_seconds": 86400
}
```

El fichero se comprueba al arrancar y después con la periodicidad indicada por
`update_interval_seconds` (un día por defecto). La descarga y el análisis se
realizan sin detener el tráfico; la base activa se sustituye de forma atómica
solo después de validar el nuevo contenido. Si una actualización falla, se
mantiene la base ya cargada. Durante el arranque, si la descarga falla, se usa
la última copia válida; si tampoco existe una copia, el servicio arranca sin
información de países. El encabezado del dashboard muestra la versión CTY
cargada y el panel de eventos registra cada intento y resultado.

Los validadores HTTP `ETag` y `Last-Modified` se guardan junto a la caché en
`cty.dat.http.json`. Las siguientes comprobaciones son condicionales: una
respuesta `304 Not Modified` confirma la versión sin descargar nuevamente
CTY.DAT. Si el servidor remoto no publica validadores, se descarga y valida el
fichero completo.

Para cada línea con formato `DX de SPOTTER: frecuencia DX ...` se conserva
internamente el indicativo y la entidad CTY tanto del spotter como del DX,
incluidas excepciones de indicativo completo y el prefijo coincidente más
largo. También quedan disponibles continente, zonas CQ/ITU y coordenadas para
filtros posteriores. Estos metadatos no se añaden ni modifican la línea
entregada a los clientes.

Las frecuencias recibidas desde `RBNCW` y `RBNMGM` se normalizan a un decimal
antes de aplicar filtros, ADS y entrega. Por ejemplo, `14025.00` se publica como
`14025.0`. Antes de entregar cualquier spot, el relleno posterior a `:` se
recalcula para situar el decimal de la frecuencia en la columna 23. Los campos
posteriores conservan el espaciado que sigue a la frecuencia. También se
reconocen líneas sin separación entre el `:` del spotter y la frecuencia.

Los indicativos de spotter RBN con un SSID numérico inmediatamente anterior al
marcador `-#` también se normalizan. Por ejemplo, `CA0LL-3-#` se entrega como
`CA0LL-#`. La forma normalizada se utiliza para metadatos, filtros, ADS y el
recuento de spotters únicos, evitando tratar cada SSID como una estación
distinta. El carácter `:` permanece pegado al spotter y el espacio liberado se
añade después del separador hasta alcanzar la columna común de frecuencia. De
este modo, las frecuencias quedan alineadas por el decimal. Los indicativos que
no siguen esa estructura permanecen intactos.

### Conexiones salientes

El `login` de configuración admite un único indicativo, opcionalmente seguido
de un SSID AX.25 entre 0 y 15. No admite espacios, sufijos operativos ni más de
un indicativo. Se envía como primera línea a RBNCW y RBNMGM. En RBNLCL se
elimina el SSID: por ejemplo, `EA1HFJ-7` se envía como `EA1HFJ`.

DXSPOT no utiliza ese valor común. Cuando un cliente entra al agregador se abre
una conexión DXSPOT exclusiva para su sesión y se reemplaza cualquier SSID de
su login por `-77`: `EA1ABC` se conecta como `EA1ABC-77` y `EA1ABC-3` también
se conecta como `EA1ABC-77`. Los comandos configurados para DXSPOT se envían
después de ese login en cada conexión individual.

```json
"upstream": {
  "keepalive_seconds": 180,
  "keepalive_command": "",
  "reconnect_initial_seconds": 1,
  "reconnect_max_seconds": 30
}
```

El keepalive predeterminado es una línea Telnet vacía. Si una fuente exige un
comando concreto, se puede cambiar con `keepalive_command`. Una desconexión,
un error de red o el cierre remoto siempre inicia otro intento; la espera crece
desde 1 hasta 30 segundos y vuelve a 1 después de conectar.

### Dashboard web

```json
"web": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 8080
}
```

La interfaz web es de solo lectura y actualiza el resumen y las tasas una vez
por segundo. La columna de métricas distribuye tres tarjetas por fila: primero
las tres fuentes comunes y después los clientes conectados con el mismo
formato. Los datos de cada conexión se presentan en vertical sobre una gráfica
alta con referencias en el eje Y y 600 segundos de histórico. En clientes se
muestra únicamente la entrega general final. Cada punto es el total móvil de
líneas por minuto calculado sobre los últimos 10 segundos y suavizado mediante
una media móvil exponencial (`EMA`, α 0,025). La tasa se recalcula y registra
una vez por segundo. No se muestran métricas de bytes.
La cabecera unificada concentra identidad, conexión en directo, versión, CTY,
actividad, uptime y clientes sin reservar una segunda franja para el título.
En pantallas de escritorio, las tarjetas y gráficas ocupan la columna
izquierda. La columna derecha permite elegir mediante un selector compacto
entre la actividad, cada stream compartido de entrada, la entrada SPOTS HUMANOS
de cada cliente y su stream final de salida. Sólo se consulta el contenido de
la vista seleccionada. En pantallas estrechas ambas columnas se apilan. Eventos
y líneas de stream usan timestamps `[HH:MM:SS.mmm]`.
El servidor conserva las 600 muestras de cada socket: una nueva conexión web
recibe inmediatamente los diez minutos disponibles y después solo las
actualizaciones corrientes. Las actualizaciones llegan mediante una conexión
SSE permanente en `/api/events`; `/api/state` se conserva para la carga inicial
y como sondeo de respaldo automático si el stream se interrumpe. Fuera de
Docker escucha únicamente en la máquina local de forma predeterminada. Si se publica
`0.0.0.0`, conviene restringir el puerto con firewall o un proxy inverso.

### Servidor para clientes

```json
"server": {
  "host": "127.0.0.1",
  "port": 7300,
  "client_timeout_seconds": 300,
  "client_queue_lines": 1000,
  "default_sources": [
    "dxcluster"
  ],
  "client_config_path": "data/clients.json",
  "welcome": "DXSpot Agregator"
}
```

La dirección predeterminada solo escucha conexiones locales. Para ofrecer el
servicio en todas las interfaces se puede usar `0.0.0.0`; antes conviene
restringir el puerto mediante firewall.

Al conectar, el servidor envía una línea en blanco, un bloque de 80 caracteres
con el título centrado, otra línea en blanco y después muestra `DXA > Login:`:

```text

--------------------------------------------------------------------------------
                                DXSpot Agregator
--------------------------------------------------------------------------------
         Software EXPERIMENTAL que combina varias fuentes de spots DX.
       Cada cliente puede seleccionar fuentes y aplicar filtros propios.
             Un algoritmo agrupa spots repetidos y reduce el ruido.

      EXPERIMENTAL software combining multiple real-time DX spot sources.
          Each client can select sources and apply individual filters.
        A reduction algorithm groups duplicate spots and reduces noise.
--------------------------------------------------------------------------------

DXA > Login:
```

Espera un único indicativo, con un SSID opcional entre 0 y 15. El indicativo se
normaliza a mayúsculas y se confirma con `INDICATIVO de DXA > Login OK`. No se
solicita contraseña: el login identifica el perfil, pero no autentica la
identidad del operador.

La primera vez que se conecta un indicativo, o si conserva un perfil antiguo sin
esta preferencia, el servidor solicita:

```text
EA1ABC de DXA > Idioma / Language [ES/EN]:
```

Solo admite `ES` o `EN`, sin distinguir mayúsculas. La selección se guarda en el
perfil y no vuelve a solicitarse en conexiones posteriores. Desde ese momento,
los prompts y el bloque de estado se muestran en el idioma elegido. Después de
aceptar el login se recuerdan las fuentes activas y los filtros restaurados antes
de comenzar a entregar spots. Por ejemplo, en inglés:

```text
EA1ABC de DXA > Connection established

DXA READY - EA1ABC
--------------------------------------------------------------------------------
SOURCES : HUMAN SPOTS: ON | SKIMMER CW/RTTY: OFF
        : SKIMMER FTx: OFF | SKIMMER LOCAL: OFF
ADS     : OFF | WINDOW 5s | QUALITY Q1
OPTIONS : BEACON ON | SEEME OFF
FILTERS : RBN ONLY | 0 RULES
--------------------------------------------------------------------------------

```

Siempre aparecen las cuatro fuentes. `ON` indica que el cliente recibirá sus
spots y `OFF` que esa fuente no se entregará a esa sesión.

`client_timeout_seconds` limita el tiempo disponible para completar el login y,
cuando corresponde, la selección de idioma.
Una vez autenticado, el servidor no inyecta comandos Telnet de keepalive en el
flujo. La conexión permanece abierta mientras el socket siga disponible; el
cliente puede mantenerla activa enviando líneas vacías, que se aceptan
silenciosamente. Para cerrarla debe enviar `dxa bye`.

La antigua clave `server.telnet_keepalive_seconds` se ignora si permanece en un
fichero de configuración existente, por lo que puede eliminarse sin afectar al
resto de sus valores.

Todo comando que no sea propio del agregador se reenvía a `DXSPOT` sin
modificar sus mayúsculas, espacios ni terminación de línea, utilizando la
conexión DXSPOT exclusiva del cliente que lo envió. Si esa conexión no está
disponible en ese momento, el comando se descarta y se registra el evento.
Se exceptúan `set/skimmer`, en cualquiera de sus variantes, y `set/seeme`:
estos comandos no se reenvían, reciben `Command rejected` o
`Comando no aceptado`, según el idioma del perfil, y el bloqueo queda registrado
en los eventos.

Cada cliente dispone de su propia cola. Si acumula
`client_queue_lines` líneas sin poder consumirlas, solo ese cliente se
desconecta.

### Filtros por cliente

Un indicativo que se conecta por primera vez comienza únicamente con `DXSPOT`;
`RBNCW`, `RBNMGM` y `RBNLCL` están desactivados, no hay reglas de filtrado, el
modo de filtros está limitado a fuentes RBN (`FILTER OFF`), ADS está
desactivado, SEEME está desactivado y BEACON está activado. Las fuentes
iniciales se pueden redefinir mediante `server.default_sources`. La selección y
los filtros se guardan en `server.client_config_path` cada vez que un comando
los modifica y se restauran en conexiones posteriores. El SSID forma parte de
la identidad del perfil, por lo que `EA1ABC` y `EA1ABC-7` tienen configuraciones
diferentes.

La selección se controla mediante comandos con el prefijo `dxa`. Los nombres
válidos de fuente son `dxcluster` (`DXSPOT`), `rbn_cw` (`RBNCW`),
`rbn_digital` (`RBNMGM`) y `rbn_local` (`RBNLCL`).

```text
dxa set/skimmer       activa RBNCW, RBNMGM y RBNLCL
dxa set/skimmer cw    activa RBNCW y desactiva RBNMGM
dxa set/skimmer ftx   activa RBNMGM y desactiva RBNCW
dxa unset/skimmer     desactiva RBNCW, RBNMGM y RBNLCL
dxa unset/skimmer cw  desactiva RBNCW
dxa unset/skimmer ftx desactiva RBNMGM
dxa set/skimmer lcl   activa RBNLCL
dxa unset/skimmer lcl desactiva RBNLCL
dxa set/ads           activa ADS con la ventana guardada
dxa set/ads 8         activa ADS con una ventana móvil de 8 segundos
dxa set/ads 8 3       activa ADS a 8 segundos con calidad mínima 3
dxa unset/ads         desactiva ADS sin borrar la ventana guardada
dxa set/beacon        deja pasar anuncios BEACON y NCDXF
dxa unset/beacon      filtra anuncios BEACON y NCDXF
dxa set/seeme         el indicativo conectado omite filtros y ADS
dxa unset/seeme       el indicativo conectado pasa por filtros y ADS
dxa status            muestra fuentes, opciones y filtros activos
dxa status/default    restaura y muestra el perfil predeterminado
dxa bye               desconecta al cliente que envía el comando
```

`dxa status` presenta las fuentes, opciones y filtros en un único bloque
compacto. Las reglas se muestran mediante su número y expresión:

```text
EA1ABC de DXA > Command accepted

DXA STATUS - EA1ABC
--------------------------------------------------------------------------------
SOURCES : HUMAN SPOTS: ON | SKIMMER CW/RTTY: ON
        : SKIMMER FTx: OFF | SKIMMER LOCAL: ON
ADS     : ON | WINDOW 30s | QUALITY Q2
OPTIONS : BEACON OFF | SEEME ON
FILTERS : ALL SOURCES | 2 RULES

  1: not on hf
  2: not by cq 14,15
--------------------------------------------------------------------------------

```

`dxa status/default` restablece el perfil canónico del indicativo conectado:
solo `DXSPOT`, ninguna regla, filtros limitados a RBN, ADS desactivado con
ventana 5s y calidad Q1, SEEME desactivado y BEACON activado. Este comando no
depende de `server.default_sources`. La nueva configuración se guarda
inmediatamente y devuelve el bloque `DXA STATUS` resultante.

Todos los comandos aceptados que cambian fuentes, ADS, BEACON, SEEME o filtros
responden con `Command accepted` o `Comando aceptado`, según el idioma, y el
bloque completo de estado. Después de devolver información sobre una consulta o
un cambio de configuración, la entrega de datos en tiempo real a ese cliente se
pausa durante cinco segundos. Los spots recibidos durante la pausa permanecen en
su cola y los demás clientes no se ven afectados.

Si un comando comienza por `dxa` pero no se reconoce o contiene parámetros no
válidos, el servidor responde:

```text
EA1ABC de DXA > Command rejected
```

### Agrupamiento Dinámico de Spots (ADS)

ADS pertenece al perfil de cada cliente, se conserva entre conexiones y está
desactivado de forma predeterminada:

```text
dxa set/ads
dxa set/ads 8
dxa set/ads 8 3
dxa unset/ads
```

`dxa set/ads` activa ADS conservando la ventana guardada. La variante con un
número cambia la ventana móvil y activa ADS. Un segundo número configura la
calidad mínima, expresada como el número de spotters RBN distintos que debe
alcanzar el grupo para poder entregarse. Se admiten ventanas enteras entre 1 y
60 segundos y calidades entre 1 y 100. Los valores predeterminados para perfiles
nuevos o antiguos sin estos ajustes son 5 segundos y calidad 1. `dxa unset/ads`
desactiva el agrupamiento sin borrar su ventana ni su calidad.

Cuando está activo, ADS se aplica exclusivamente a los spots de `RBNCW`,
`RBNMGM` y `RBNLCL`. Dos spots de la misma estación DX se consideran
equivalentes si sus frecuencias difieren como máximo `±0,2 kHz`. Se toma como
base la primera línea del grupo, por lo que el indicativo de spotter visible es
el primero que escuchó la estación. Cuando interviene más de un spotter RBN
distinto, se añade el número de estaciones adicionales: si ocho estaciones la
han escuchado, se muestra `+7`. En los anuncios RBN agrupados, el contador
sustituye al marcador `CQ`: `CW 18 dB 28 WPM +7  1522Z`. El contador y su
relleno ocupan exactamente el ancho anterior de `CQ` para no desplazar la hora
ni alterar los campos delimitados después de `:`. En los anuncios de balizas,
el campo comprendido desde `NCDXF` o `BEACON` hasta la hora se convierte en
`BCN+7` seguido de relleno; si el número no cabe, se abrevia como `B+7`. También
conserva exactamente el ancho original, por lo que el texto adicional de la
baliza no permanece ni invade el campo de hora. Las recepciones repetidas del
mismo spotter no aumentan el contador.

La entrega se produce cuando transcurre la ventana configurada sin recibir otro
spot equivalente. Cada recepción equivalente reinicia la ventana completa; por
tanto, un grupo puede permanecer abierto durante más tiempo mientras siga
recibiendo actividad. Al cerrarse, el grupo se descarta silenciosamente si no
alcanza la calidad mínima. Las recepciones repetidas del mismo spotter reinician
la ventana, pero no aumentan la calidad.

Los filtros se aplican antes de ADS. Todos los spots que los superan entran en
el mismo espacio de agrupación del cliente. Los valores admitidos por una misma
selección geográfica no crean grupos distintos: por ejemplo, si el filtro deja
pasar las zonas CQ 14 y 15, los spots procedentes de ambas zonas pueden
agruparse entre sí. Lo mismo sucede con una selección de varias entidades
DXCC. Sin filtros geográficos, todos los orígenes se agrupan globalmente.

Los spots de `DXSPOT`, las líneas RBN que no tengan formato de spot y cualquier
fuente con ADS desactivado continúan entregándose inmediatamente. Al cambiar la
configuración de una sesión, los grupos pendientes se vacían para no perder
spots ni mezclar criterios anteriores y nuevos.

### Filtro de balizas

La entrega de anuncios de balizas también pertenece al perfil del usuario:

```text
dxa set/beacon
dxa unset/beacon
```

`dxa set/beacon` deja pasar los spots cuyo comentario contiene las palabras
`BEACON` o `NCDXF`. `dxa unset/beacon` los descarta antes de aplicar los demás
filtros y antes de ADS. Las coincidencias se realizan sobre el comentario del
spot, no sobre los indicativos. Las balizas están permitidas de forma
predeterminada para conservar el comportamiento de los perfiles existentes.

### Visibilidad del indicativo conectado

Esta excepción pertenece al perfil del usuario:

```text
dxa set/seeme
dxa unset/seeme
```

Con `dxa set/seeme`, los spots dirigidos al indicativo conectado se entregan
directamente, omitiendo filtros y ADS. Con `dxa unset/seeme`, esos spots pasan
por el mismo flujo de filtrado y agrupamiento que los demás. Para la comparación
se ignora el SSID del login y se comprueba si el indicativo normalizado aparece
en cualquier parte del campo DX. Por ejemplo, `EA1ABC-7` coincide con `EA1ABC`,
`EA1ABC/P` y `F/EA1ABC`. La opción está desactivada de forma predeterminada.

### Filtros de spots

Los filtros son numerados y pertenecen únicamente al cliente que los define.
Volver a usar un número sustituye su regla anterior. De forma predeterminada
solo se aplican a los spots procedentes de `RBNCW`, `RBNMGM` y `RBNLCL`;
`DXSPOT` queda sin filtrar hasta que el cliente activa `dxa set/filter`.

El alcance de los filtros también pertenece al perfil del cliente:

```text
dxa set/filter    aplica los filtros a todas las fuentes, incluido DXSPOT
dxa unset/filter  aplica los filtros solamente a RBNCW, RBNMGM y RBNLCL
```

El modo predeterminado es `unset/filter`. El alcance activo aparece como
`FILTERS : ALL SOURCES` o `FILTERS : RBN ONLY` durante la conexión y al
ejecutar `dxa sh/filter`.

```text
dxa rej/spot 1 not by cq 14,15
```

Los filtros de zona CQ admiten estas cuatro formas:

```text
dxa rej/spot 1 not by cq 14,15  rechaza spotters que no sean CQ 14 o 15
dxa rej/spot 1 by cq 14,15      rechaza spotters que sean CQ 14 o 15
dxa rej/spot 1 not cq 17        rechaza estaciones DX que no sean CQ 17
dxa rej/spot 1 cq 19            rechaza estaciones DX que sean CQ 19
```

`by` selecciona el indicativo del spotter; sin `by` se comprueba la estación DX
anunciada. `not` invierte la pertenencia a las zonas indicadas. Si no se puede
resolver el país o la zona, las formas con `not` rechazan el spot y las formas
sin `not` lo dejan pasar. Los banners, respuestas y demás mensajes que no
tengan formato de spot no se ven afectados.

La misma sintaxis permite filtrar por entidad DXCC mediante su prefijo principal
en `CTY.DAT`. Los valores no distinguen mayúsculas y minúsculas; se usan
prefijos, no números de entidad:

```text
dxa rej/spot 1 not by dxcc EA,F  rechaza spotters que no sean EA o F
dxa rej/spot 1 by dxcc EA,F      rechaza spotters que sean EA o F
dxa rej/spot 1 not dxcc K        rechaza estaciones DX que no sean K
dxa rej/spot 1 dxcc 3D2/C        rechaza estaciones DX que sean 3D2/C
```

También se pueden indicar varias entidades separadas por comas. Si una entidad
no puede resolverse, se aplica el mismo criterio que con CQ: las formas con
`not` rechazan el spot y las formas sin `not` lo dejan pasar.

También se puede filtrar por banda:

```text
dxa rej/spot 1 on 160m
dxa rej/spot 2 not on 160m,80m,60m,40m,30m,20m,17m,15m,12m,10m
```

La forma `on` rechaza los spots que pertenezcan a alguna de las bandas
indicadas. La forma `not on` rechaza los que no pertenezcan a ninguna de ellas.
La banda se determina internamente a partir de la frecuencia en kHz; la línea
original no se modifica. Con `not on`, una frecuencia desconocida o fuera de
los intervalos reconocidos también se rechaza.

La lista completa de HF dispone del atajo:

```text
hf = 160m,80m,60m,40m,30m,20m,17m,15m,12m,10m
```

El atajo se puede utilizar con ambas variantes:

```text
dxa rej/spot 2 not on hf
dxa rej/spot 3 on hf
```

El atajo se expande internamente, pero se vuelve a mostrar como `hf` en el
feedback y en `dxa sh/filter`.

También se puede filtrar directamente por uno o varios intervalos de
frecuencia, expresados en kHz y con ambos límites incluidos:

```text
dxa rej/spot 4 on freq 7200/7400
dxa rej/spot 5 not on freq 7000/7200,14000/14350
```

La forma `on freq` rechaza los spots situados dentro de alguno de los
intervalos. La forma `not on freq` rechaza los situados fuera de todos ellos.

Las condiciones CQ, DXCC, banda y frecuencia pueden combinarse dentro de una
misma regla mediante `and`, `or`, `not` y paréntesis:

```text
dxa rej/spot 6 on 20m and not by cq 14,15
dxa rej/spot 7 by dxcc EA or by dxcc F
dxa rej/spot 8 (on 40m or on 80m) and not by dxcc EA
dxa rej/spot 9 not (on freq 7000/7200 or on freq 14000/14350)
```

Una expresión verdadera rechaza el spot. `not` tiene mayor prioridad que
`and`, y `and` tiene mayor prioridad que `or`; los paréntesis permiten cambiar
ese orden. Las reglas numeradas diferentes mantienen la lógica existente:
basta que una de ellas sea verdadera para rechazar el spot. Los filtros simples
anteriores siguen siendo válidos y los perfiles guardados con el formato
anterior se migran automáticamente al volver a guardarse.

Cuando se acepta o sustituye un filtro, el cliente recibe el bloque completo
`DXA STATUS` con las reglas que quedan aplicadas.

Los filtros se eliminan por número o en conjunto:

```text
dxa clear/spot 1    elimina el filtro 1
dxa clear/spot all  elimina todos los filtros
```

Después de borrar se devuelve el bloque completo `DXA STATUS`. Intentar borrar
un número que no existe es válido y no produce ningún otro cambio.

Los filtros activos del cliente se pueden consultar con:

```text
dxa sh/filter
```

La respuesta es el mismo bloque completo `DXA STATUS`, incluida la lista de
reglas, y mantiene la pausa general de cinco segundos.

## Ejecución

Valida primero el fichero:

```bash
python3 dxspot_agregator.py --check-config
```

Inicia el servicio:

```bash
python3 dxspot_agregator.py
```

También se puede indicar otra configuración:

```bash
python3 dxspot_agregator.py --config /ruta/config.json
```

## Contenedor

La imagen utiliza Python 3.12, no instala dependencias adicionales y ejecuta el
servicio con un usuario no privilegiado. `config.example.json` se incluye como
configuración de respaldo, pero Compose monta el `config.json` local sin
incorporarlo a la imagen.

Construye e inicia el servicio:

```bash
docker compose up -d --build
```

Consulta su salida:

```bash
docker compose logs -f
```

Detén el servicio:

```bash
docker compose down
```

El servidor queda disponible en el puerto 7300 del host. Puede cambiarse solo
el puerto exterior sin modificar `config.json`:

```bash
DXA_PORT=7400 docker compose up -d
```

Compose establece `DXA_SERVER_HOST=0.0.0.0` y
`DXA_SERVER_PORT=7300` dentro del contenedor. Estas variables tienen prioridad
sobre `server.host` y `server.port`; fuera de Docker se siguen usando los
valores del JSON. También establece `DXA_WEB_HOST=0.0.0.0` y
`DXA_WEB_PORT=8080` para el dashboard web. La caché `CTY.DAT` y los perfiles de
clientes se conservan en el volumen `dxspot-data`, mientras que `config.json`
se monta como solo lectura.

El dashboard queda disponible en:

```text
http://127.0.0.1:8080
```

Puede cambiarse únicamente el puerto web exterior:

```bash
DXA_WEB_PORT=8181 docker compose up -d
```

Para probar el servidor desde otra terminal:

```bash
telnet 127.0.0.1 7300
```

El comando `dxa bye` cierra la sesión después de enviar:

```text
EA1ABC de DXA > Thank you for using DXSpot-Agregator.
```

El dashboard de terminal se activa únicamente al ejecutar en una terminal
interactiva. El dashboard web funciona también dentro de Docker y puede
desactivarse con `"web": {"enabled": false}` en `config.json`. Con salida
redirigida se imprimen eventos de conexión y desconexión.

El dashboard CLI utiliza el mismo concepto de tarjetas que la web, sin
gráficas. Distribuye hasta tres tarjetas por fila: primero las fuentes comunes
y después los clientes conectados, con sus datos dispuestos en vertical. En
las tarjetas de cliente, `ENTREGA/MIN` es la única tasa mostrada y representa
los spots DX finales entregados después de combinar las cuatro fuentes y
aplicar su perfil; no contabiliza banners, respuestas, prompts ni avisos.
`COLA` muestra la ocupación de su cola de salida. El CLI no muestra contadores
de bytes. Los estados conectados,
transitorios, desconectados y desactivados se distinguen mediante colores
ANSI. En terminales anchos, la columna derecha permite alternar entre
`ACTIVIDAD`, los streams compartidos de entrada, la entrada SPOTS HUMANOS de
cada cliente y su salida final. Las flechas izquierda y derecha recorren las
vistas; `A` vuelve directamente a Actividad. Las conexiones y entidades
cargadas aparecen en verde y los errores o desconexiones en rojo. En terminales
estrechos se conserva automáticamente el diseño apilado. Si `stdin` no es una
terminal, la vista permanece en Actividad.

## Qué contabiliza el dashboard

- `RATE`: exclusivamente líneas reconocidas con formato `DX de ...` durante
  los últimos 10 segundos, suavizadas mediante una EMA con α 0,025 y
  recalculadas cada segundo.
- `ÚLTIMO`: edad de la última recepción.
- `REC.`: número de ciclos de reconexión de la fuente.

Banners, prompts, login, keepalive, comandos, respuestas y negociación Telnet
no incrementan ningún contador de líneas, aunque sí actualizan la actividad
técnica y los bytes internos. Los caracteres de control procedentes de las
fuentes —incluido `BEL`— se eliminan para impedir sonidos y desalineaciones del
dashboard.

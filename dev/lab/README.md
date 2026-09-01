# DomoAI Virtual Domotics Lab

Este laboratorio simula las fronteras de protocolo que consume DomoAI. No
simula radios Zigbee ni sustituye el commissioning Matter, ETS/KNX o la
validación con hardware.

## Runner local reproducible

El runner `domoai-lab` centraliza las operaciones seguras del laboratorio y no
requiere una instalación domótica real:

```bash
uv run domoai-lab up
uv run domoai-lab status
uv run domoai-lab smoke
uv run domoai-lab down
```

El gateway compartido puede arrancar con el bootstrap operativo del perfil
`lab`. `deploy/wsl/run-gateway.sh` y `deploy/windows/run-gateway.ps1` cargan el
`.env` ignorado de `dev/lab` solo con líneas `KEY=VALUE`; no imprimen ni
persisten sus credenciales. El perfil comprueba únicamente endpoints
allowlisted, conserva cualquier valor explícito y genera
`data/runtime-bootstrap.json` sin secretos.

```text
DOMOAI_BOOTSTRAP_PROFILE=lab
DOMOAI_BOOTSTRAP_MANIFEST_PATH=data/runtime-bootstrap.json
```

El bootstrap configura endpoints, mappings de telemetría y, cuando detecta el
Home Assistant local con credenciales, selecciona los assets canónicos
`dispatchable-battery-lab.json` y `ev-charging-lab.json` si no se han indicado
otros. No infiere rutas, no fabrica evidencia HIL y no activa autoridad de
producción: cargar el `DispatchableBatteryBinding` deja el estado en
`software-qualified` y `/readyz` sigue bloqueando la autoridad física hasta
qualification real. Si HA no tiene credenciales o no está disponible, no se
seleccionan esos assets.

El arranque por defecto cubre MQTT/Zigbee2MQTT y Modbus. Con el perfil `lab`,
la batería virtual y el cargador se activan desde los assets canónicos si HA
está autenticado y reachable; las variables explícitas siguen teniendo
prioridad:

```bash
uv run domoai-lab up --services mqtt zigbee2mqtt modbus homeassistant
uv run domoai-lab up --services matter-server
```

Mosquitto conserva los mensajes retained de discovery y estado en el volumen
Docker nombrado `domoai-lab-mqtt-data`. Home Assistant usa, por separado,
`domoai-lab-homeassistant-data`; crea ambos una vez antes del primer arranque:

```bash
docker volume create domoai-lab-mqtt-data
docker volume create domoai-lab-homeassistant-data
```

No uses `docker compose down -v`: puede borrar la identidad de Home Assistant
y el discovery MQTT retenido.

## Batería virtual compartida

La batería de laboratorio es un único modelo dinámico proyectado por HTTP,
MQTT/Home Assistant y Modbus TCP. No es hardware ni habilita autoridad de
producción.

```bash
docker compose -f dev/lab/compose.yaml --profile battery up -d --build battery
curl http://127.0.0.1:8090/health
```

Endpoints: `GET /state`, `POST /command`, `POST /tick` con
`{"seconds":1800}` y `POST /fault`. Modbus TCP escucha en `127.0.0.1:1503`
con `configs/modbus-battery.json`. La batería publica descubrimiento MQTT
retenido bajo `homeassistant/` y estado en `domoai/battery/state`.

```bash
docker compose -f dev/lab/compose.yaml --profile homeassistant --profile battery \
  up -d --build mqtt battery homeassistant
DOMOAI_MODBUS_HOST=127.0.0.1 \
DOMOAI_MODBUS_PORT=1503 \
DOMOAI_MODBUS_CONFIG_PATH=dev/lab/configs/modbus-battery.json \
uv run pytest -q tests/contract/test_modbus_adapter.py -k battery
```

Para enlazar la batería con KNX Virtual, que corre fuera de Docker en
Windows, ejecuta `knxd` en WSL. KNX Virtual conserva su única interfaz en
`172.26.80.1:3671`; el gateway WSL usa la IP real de WSL y publica `3672/UDP`
para ETS y DomoAI. Esto evita el NAT de Docker, que cambia el origen de los
telegramas KNXnet/IP y rompe los ACK de tunneling de KNX Virtual:

```bash
docker compose -f dev/lab/compose.yaml --profile battery --profile homeassistant \
  up -d --build mqtt battery homeassistant

dev/lab/knx-gateway/run-wsl.sh
```

Deja ese proceso ejecutándose. El script detecta la IP de WSL que alcanza a
KV, descarga una copia de usuario de `knxd` si aún no existe y genera la
configuración temporal con `src-port=3673`. No requiere instalar paquetes con
`sudo`. En otra terminal, declara el endpoint del gateway y deja que
`domoai-lab` supervise el bridge:

```bash
DOMOAI_KNX_GATEWAY_HOST="$(ip route get 172.26.80.1 | awk '{for (i=1; i<=NF; i++) if ($i==\"src\") {print $(i+1); exit}}')" \
DOMOAI_KNX_GATEWAY_PORT=3672 \
DOMOAI_KNX_ROUTE_BACK=0 \
DOMOAI_KNX_BRIDGE_MAPPING_PATH=dev/lab/configs/knx-battery-virtual.json \
uv run domoai-lab up --services mqtt battery knx-bridge
```

El arranque inicia primero MQTT y la batería y después el proceso WSL. Espera
un estado `ready`; `degraded` o `failed` tienen salida no cero y no son una
prueba válida de readback. `ready` exige también el readback independiente de
los tres grupos de batería. El estado y el log quedan en `.lab-state/`.

```bash
uv run domoai-lab status --services mqtt battery knx-bridge
```

En ETS modifica la conexión existente para apuntar a la IP actual de WSL en
`3672/UDP`; conserva la dirección individual `1.0.255`. No conectes ETS ni
DomoAI directamente a `172.26.80.1:3671`: KV queda reservado para el único
upstream `knxd`.

El puente publica el estado MQTT en `4/0/1`, `4/0/2` y `4/0/3`, responde a las
lecturas KNX de esas tres direcciones con el último estado retenido y convierte
`4/0/0` en una consigna MQTT firmada (`+` carga, `-` descarga, `0` parada).
Esto es necesario porque el proyecto ETS actual no tiene un dispositivo KNX
asociado a esas direcciones; la respuesta la da explícitamente la fachada de
laboratorio, no KNX Virtual por sí solo.
El puerto `1503` es solo el Modbus TCP de la batería; no es el puerto KNX.

Para observar la orden que llega desde ETS:

```bash
docker compose -f dev/lab/compose.yaml exec mqtt \
  mosquitto_sub -h mqtt -t domoai/battery/power/set -v -C 1
```

Esta pasarela es únicamente una multiplexación de laboratorio; no concede
autoridad física de producción ni sustituye la cualificación KNX/IP. El
servicio Compose `knx-gateway` queda bajo el perfil experimental
`knxdocker`; no lo arranques para esta topología Windows/WSL.

El proceso del bridge supervisa la sesión de MQTT y KNX por separado dentro de
una sesión conjunta: si cualquiera de los transportes pierde la conexión,
cancela el otro pump, libera ambos clientes y vuelve a conectarlos con backoff
exponencial (1–30 s por defecto). Una caída transitoria queda en `degraded` y
no termina el proceso; `failed` se reserva para errores de configuración o de
payload que no son recuperables automáticamente. El estado retenido se vuelve
a exigir después de cada reconexión antes de proyectarlo otra vez al bus.

Para detener el bridge supervisado y después los servicios Docker:

```bash
uv run domoai-lab down
```

El mapping de Home Assistant enlaza la batería mediante el claim estable
`mqtt:lab-battery-1` del registro de dispositivos, no mediante el
`device_id` UUID generado localmente por HA. El provider consulta el registro
vivo y resuelve el UUID actual en cada discovery; si el claim desaparece,
aparece duplicado o cambia, el binding falla cerrado y debe revisarse el
registro MQTT, no copiar un UUID a mano.

`dev/lab/.env` es opcional, está ignorado por Git y solo se carga en el
subproceso de Docker para `up`, `status` y `down`. `domoai-lab smoke` no carga
ese archivo, elimina las variables `DOMOAI_*` del proceso hijo y ejecuta solo
fixtures locales; por tanto no activa OMIE, Open-Meteo, Home Assistant live,
Matter commissioning ni KNX/IP.

Para detener y borrar volúmenes de forma explícita:

```bash
uv run domoai-lab down --volumes
```

Si `docker` no está en `PATH`, usa `--docker-bin` o `DOMOAI_DOCKER_BIN`. El
runner valida los servicios antes de lanzar Compose y nunca imprime valores
del archivo de entorno.

## Cargador EV virtual

El cargador EV de laboratorio es, igual que la batería, un único modelo
dinámico proyectado por HTTP, MQTT/Home Assistant y Modbus TCP.

```bash
docker compose -f dev/lab/compose.yaml --profile ev-charger up -d --build ev-charger
curl http://127.0.0.1:8091/health
```

Endpoints: `GET /state`, `POST /command`, `POST /tick` con
`{"seconds":1800}`, `POST /fault`, `POST /connect` y `POST /disconnect`.
Modbus TCP escucha en `127.0.0.1:1504` con `configs/modbus-ev-charger.json`
(distinto del `1503` de la batería para poder correr ambos labs a la vez). El
cargador publica descubrimiento MQTT retenido bajo `homeassistant/` y estado
en `domoai/ev-charger/state`.

```bash
docker compose -f dev/lab/compose.yaml --profile ev-charger \
  up -d --build mqtt ev-charger
DOMOAI_MODBUS_HOST=127.0.0.1 \
DOMOAI_MODBUS_PORT=1504 \
DOMOAI_MODBUS_CONFIG_PATH=dev/lab/configs/modbus-ev-charger.json \
uv run pytest -q tests/contract/test_modbus_adapter.py -k ev
```

Solo admite carga (`charge_ev`/`stop_ev`), sin descarga/V2G. `/connect` y
`/disconnect` simulan enchufar/desenchufar el vehículo; desconectar durante
una carga activa la detiene automáticamente, igual que exige el gate de
seguridad de producción (`DynamicSafetyGuard`, sin cambios).

Para que el runtime pueda usar el cargador, configura además el binding
canónico y el mapping de rutas exactas de Home Assistant:

```bash
DOMOAI_EV_CHARGING_BINDING_PATHS=dev/lab/configs/ev-charging-lab.json
DOMOAI_HOME_ASSISTANT_MAPPING_PATH=dev/lab/configs/home-assistant-ev-charger.json
```

El binding limita la carga a 7,4 kW, enlaza el ID canónico `lab.ev_charger` y
no declara una hora de salida hasta que exista una entidad de departure real.
El mapping por sí solo no habilita el comando.

El mapping público reutiliza la dirección `0` en bloques Modbus distintos
(entrada discreta y registros de entrada). El simulador mantiene esos bloques
separados y traduce internamente sus offsets; así el bit `ev.connected` no
corrompe el `ev.soc` y el registro de mando `10` sigue siendo un holding
register. Batería, EV y los demás servicios pueden ejecutarse a la vez porque
sus puertos publicados son distintos.

## Medidor de agua virtual

El medidor de agua de laboratorio es de solo lectura (caudal instantáneo +
volumen acumulado) — a diferencia de batería/EV, no tiene ningún comando
canónico, así que no hay endpoint `/command` ni registro Modbus escribible.

```bash
docker compose -f dev/lab/compose.yaml --profile water-meter up -d --build water-meter
curl http://127.0.0.1:8092/health
```

Endpoints: `GET /state`, `POST /flow` (control de laboratorio para fijar el
caudal simulado — `{"flow_rate_lpm": 6.0}` — no es un comando canónico,
ningún medidor real lo aceptaría) y `POST /fault`. Modbus TCP escucha en
`127.0.0.1:1505` con `configs/modbus-water-meter.json` (registros de solo
lectura). El medidor publica descubrimiento MQTT retenido bajo
`homeassistant/` y estado en `domoai/water-meter/state`.

Dejar el caudal fijado en un valor distinto de cero sin motivo simula una
fuga — el volumen acumulado sigue subiendo de forma observable por el mismo
camino de lectura normal, sin mecanismo aparte.

## Termostato virtual

El termostato de laboratorio simula temperatura interior real (recurrencia
RC: masa térmica, pérdida por envolvente, calefacción/refrigeración) — a
diferencia del agua, sí tiene comando canónico (`heat_thermostat`/
`cool_thermostat`/`stop_thermostat`), igual que batería/EV.

```bash
docker compose -f dev/lab/compose.yaml --profile thermal up -d --build thermal
curl http://127.0.0.1:8093/health
```

Endpoints: `GET /state`, `POST /hvac_mode` (control de laboratorio —
`{"mode": "heat"}`/`"cool"`/`"off"`), `POST /exterior_temperature`
(`{"exterior_temperature_c": 5.0}`) y `POST /fault`. Modbus TCP escucha en
`127.0.0.1:1506` con `configs/modbus-thermal.json` — `thermal.hvac_power`
es de lectura+escritura (como `battery.power`/`ev_charging`: un registro de
comando de punto flotante firmado, positivo=calentar, negativo=enfriar,
cero=apagar); `thermal.indoor_temperature` es de solo lectura. El
termostato publica descubrimiento MQTT retenido bajo `homeassistant/` y
estado en `domoai/thermal/state`.

El mismo modelo físico (masa térmica × pérdida UA × potencia HVAC) lo
razona también el optimizador CP-SAT — el laboratorio y el optimizador
están de acuerdo en la física, no son dos implementaciones independientes.

## MVP local: MQTT/Zigbee2MQTT + Modbus TCP

Requisitos: Docker Compose y `uv`.

Si esta distro WSL no expone `docker` en el `PATH` aunque Docker Desktop esté
integrado, el daemon puede usarse mediante el ejecutable Windows:

```bash
export DOMOAI_DOCKER_BIN="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
"$DOMOAI_DOCKER_BIN" compose -f dev/lab/compose.yaml up -d --build mqtt zigbee2mqtt modbus
```

En una instalación con integración WSL normal, sustituye
`"$DOMOAI_DOCKER_BIN" compose` por `docker compose`.

```bash
docker compose -f dev/lab/compose.yaml up -d --build mqtt zigbee2mqtt modbus
docker compose -f dev/lab/compose.yaml ps
```

Endpoints desde el host:

- Mosquitto/fake Zigbee2MQTT: `mqtt://127.0.0.1:1883`.
- PyModbus simulator: `127.0.0.1:1502`.
- PyModbus web console: `http://127.0.0.1:8081`.

Configura DomoAI en la shell, sin guardar secretos:

```bash
export DOMOAI_ZIGBEE2MQTT_URL="mqtt://127.0.0.1:1883"
export DOMOAI_ZIGBEE2MQTT_BASE_TOPIC="zigbee2mqtt"
export DOMOAI_MODBUS_HOST="127.0.0.1"
export DOMOAI_MODBUS_PORT="1502"
export DOMOAI_MODBUS_CONFIG_PATH="dev/lab/configs/modbus.json"

uv run pytest -q tests/integration/test_zigbee2mqtt_smoke.py \
  tests/integration/test_modbus_smoke.py
```

El fake Zigbee2MQTT publica `bridge/state`, `bridge/devices`, estados
retenidos, disponibilidad y acepta únicamente payloads `/set` de los campos
que cada dispositivo virtual ya expone. El broker de desarrollo no tiene
autenticación; para probar autenticación, configura el broker fuera del
default y usa `DOMOAI_MQTT_USERNAME`/`DOMOAI_MQTT_PASSWORD` solo en el entorno.

## Reproducir estados

Publicar un estado de luz desde el host:

```bash
docker compose -f dev/lab/compose.yaml exec mqtt \
  mosquitto_pub -h mqtt -t zigbee2mqtt/living_room/main_light/set \
  -m '{"state":"OFF","brightness":80}'
```

Consultar los topics retenidos:

```bash
docker compose -f dev/lab/compose.yaml exec mqtt \
  mosquitto_sub -h mqtt -t 'zigbee2mqtt/#' -v -C 8
```

La consola HTTP de PyModbus permite observar y cambiar puntos para escenarios
de polling, disponibilidad y codecs. El mapping DomoAI está en
`configs/modbus.json`; sus offsets son PDU y no etiquetas `40001`.

## Home Assistant en el mismo stack

Para arrancar Home Assistant junto con MQTT/Zigbee2MQTT y Modbus, usa el
mismo proyecto Compose y su perfil explícito:

```bash
docker compose -f dev/lab/compose.yaml --profile homeassistant up -d --build \
  mqtt zigbee2mqtt modbus homeassistant
docker compose -f dev/lab/compose.yaml ps
```

Todos los servicios comparten la red del proyecto `domoai-lab`. Desde el host,
Home Assistant queda en `http://127.0.0.1:8123`; desde otro contenedor del
laboratorio, la URL es `http://homeassistant:8123`.

La configuración persistente de Home Assistant se guarda en el volumen Docker
nombrado `domoai-lab-homeassistant-data`, independiente del worktree desde el
que se ejecute Compose. No uses `docker compose down -v` mientras este volumen
sea el almacenamiento operativo, salvo que exista un respaldo verificado y la
eliminación sea intencionada.

En el primer arranque abre `http://127.0.0.1:8123`, completa el onboarding y
genera un token long-lived local. El token se configura solo en la shell y no
se guarda en el repositorio:

```bash
export DOMOAI_HOME_ASSISTANT_URL="http://127.0.0.1:8123"
export DOMOAI_HOME_ASSISTANT_TOKEN="<set-locally>"
uv run pytest -q tests/integration/test_home_assistant_smoke.py
```

Para validar además la ruta Provider SDK → runtime → MCP, activa el provider
explícitamente y ejecuta el smoke read-only:

```bash
set -a
. dev/lab/.env
set +a
export DOMOAI_HOME_ASSISTANT_PROVIDER=1
uv run pytest -q tests/integration/test_home_assistant_provider_smoke.py
```

Este test no ejecuta comandos ni escribe estados; comprueba health, discovery,
StateStore y `discover_devices`/`get_state` semánticos.

El directorio montado conserva el onboarding y los estados del laboratorio,
pero sus artefactos generados (`.storage`, base de datos, logs y metadatos) se
ignoran explícitamente en Git. Puedes cargar el archivo local ignorado
`dev/lab/.env` antes de ejecutar DomoAI:

```bash
set -a
. dev/lab/.env
set +a
uv run pytest -q tests/integration/test_home_assistant_smoke.py
```

No sincronices ni compartas ese archivo: contiene credenciales. Si el resto del
laboratorio ya está arrancado, puedes iniciar solo este perfil con
`--profile homeassistant up -d homeassistant`.

## Otros perfiles manuales

Matter Server + Matter.js virtual light:

```bash
docker compose -f dev/lab/compose.yaml --profile matter up -d --build matter-server
export DOMOAI_MATTER_SERVER_URL="ws://127.0.0.1:5580/ws"
```

El perfil arranca Matter Server y un nodo On/Off Matter.js sintético en la
misma red Docker. `--enable-test-net-dcl` es obligatorio para este nodo de
desarrollo y queda limitado al perfil virtual; no debe copiarse a producción.

La primera vez, abre `http://127.0.0.1:5580/` y commissiona el dispositivo con
el pairing code que muestran los logs de `matter-device`:

```bash
docker compose -f dev/lab/compose.yaml --profile matter logs matter-device \
  | grep "manual pairing code"
```

Después valida la ruta real DomoAI → Matter Server → nodo comisionado:

```bash
DOMOAI_MATTER_SERVER_URL="ws://127.0.0.1:5580/ws" \
  uv run pytest -q tests/integration/test_matter_server_smoke.py
```

El smoke es opt-in y requiere que el nodo esté comisionado. El compose crea
el nodo virtual, pero no lo commissiona automáticamente para evitar cambios
de estado ocultos.

KNX Virtual/ETS está documentado en [`knx-virtual.md`](knx-virtual.md). Requiere
Windows o una VM, una interfaz KNXnet/IP accesible y confirmación de las
direcciones del proyecto en `configs/knx-virtual.json` o
`configs/knx-battery-virtual.json`.

## Reset y diagnóstico

```bash
docker compose -f dev/lab/compose.yaml logs -f zigbee2mqtt modbus
docker compose -f dev/lab/compose.yaml down
```

Este reset detiene los contenedores y conserva los volúmenes nombrados. Para
eliminar datos persistentes usa `uv run domoai-lab down --volumes` solo después
de verificar un respaldo y aceptar explícitamente la pérdida de datos.

Si Docker no está disponible, la suite determinista de DomoAI y
`tests/integration/test_virtual_lab_assets.py` siguen siendo ejecutables. Los
smoke tests live deben quedar omitidos hasta que el servicio correspondiente
esté realmente accesible.

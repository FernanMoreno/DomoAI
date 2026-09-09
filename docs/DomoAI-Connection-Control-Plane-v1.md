# DomoAI — Connection Control Plane v1
## Diagnóstico, arquitectura y plan detallado de implementación

**Estado:** propuesta de arquitectura y plan de ejecución  
**Prioridad:** P0  
**Objetivo principal:** reducir al mínimo la fricción de conexión y compatibilidad para el usuario final sin degradar las garantías actuales de seguridad, aislamiento semántico, readback, policy, approval y fail-closed behavior.

---

# 1. Resumen ejecutivo

DomoAI ya dispone de una base sólida para operar una casa una vez que los sistemas físicos están correctamente configurados:

- modelo semántico canónico;
- `AdapterPort` como frontera universal;
- composición multi-adapter;
- `DeviceRegistry`;
- `StateStore`;
- planificación y ejecución con policy;
- readback;
- approval;
- safety kernel;
- MCP único;
- reconexión y degradación por fuente;
- soporte actual para Home Assistant, Matter, Zigbee2MQTT, MQTT genérico, KNX/IP y Modbus TCP.

El problema crítico está en una capa anterior:

> DomoAI sabe operar una conexión configurada, pero todavía no sabe ayudar al usuario a crear, validar, reparar y mantener esa conexión.

Actualmente, para muchos conectores, el usuario u operador debe conocer detalles que no deberían pertenecer a la experiencia normal de producto:

- IP o hostname;
- puertos;
- variables `DOMOAI_*`;
- tokens;
- credenciales MQTT;
- rutas WebSocket;
- mappings;
- configuración KNX;
- group addresses;
- DPTs;
- registros Modbus;
- TLS;
- certificados;
- interfaces;
- bridges externos;
- detalles de red.

Esto genera una diferencia importante:

```text
MCP universal ≠ conexión universal
```

La solución propuesta es introducir un plano de control de conexiones por delante de los adapters actuales:

```text
Connection Control Plane
        ↓
ManagedAdapterSet
        ↓
AdapterPort
        ↓
DiscoveryService
        ↓
DeviceRegistry / StateStore
        ↓
MCP único
```

El objetivo no debe ser `zero configuration`, porque en protocolos como KNX, Modbus o MQTT genérico sería inseguro o técnicamente imposible.

El objetivo correcto es:

> **zero unnecessary configuration**

El usuario solo debería introducir aquello que realmente no puede descubrirse, inferirse de forma segura o importarse automáticamente.

---

# 2. Diagnóstico del código actual

## 2.1. El runtime empieza demasiado tarde

La interfaz actual:

```python
class AdapterPort(Protocol):
    adapter_id: str

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def discover(self) -> AdapterSnapshot: ...
    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]: ...
    async def execute(...) -> AdapterExecutionAck: ...
    def subscribe_events(self) -> AsyncIterator[SourceEvent]: ...
    async def health(self) -> AdapterHealth: ...
```

está bien diseñada para operar una fuente ya configurada.

Pero no cubre:

- encontrar gateways;
- descubrir brokers;
- detectar Home Assistant;
- seleccionar interfaces;
- pedir credenciales;
- probar autenticación;
- pairing;
- commissioning;
- reauth;
- reconfigure;
- reparar conexiones;
- persistir conexiones;
- mostrar diagnósticos de producto.

Por tanto, ampliar `AdapterPort` con estas responsabilidades sería un error de diseño.

Debe mantenerse como interfaz operativa.

---

# 3. Problemas estructurales concretos

## 3.1. El gateway espera que exista una fuente real

Actualmente el gateway está orientado a arrancar con al menos un adapter real configurado.

Esto produce un problema circular para producto:

```text
usuario instala DomoAI
        ↓
todavía no ha configurado la casa
        ↓
DomoAI necesita una fuente configurada
        ↓
no puede ofrecer correctamente el onboarding
```

La solución es permitir:

```text
zero-provider boot
```

El runtime debe poder arrancar sin dispositivos físicos y permanecer fail-closed.

---

## 3.2. La configuración física vive principalmente en Settings

Actualmente existen campos como:

```text
home_assistant_url
home_assistant_token
zigbee2mqtt_url
generic_mqtt_url
generic_mqtt_mapping_path
matter_server_url
knx_gateway_host
knx_gateway_port
knx_config_path
modbus_host
modbus_port
modbus_config_path
```

Esto es adecuado para despliegues técnicos y legacy, pero no como modelo primario de configuración de usuario.

La configuración de infraestructura de DomoAI debe seguir en `Settings`.

La configuración de sistemas domóticos debe migrar progresivamente a una entidad persistente de conexión.

---

## 3.3. `_create_configured_adapters()` crea una composición estática

El composition root crea adapters en startup.

Consecuencias:

- añadir una conexión requiere reconstruir runtime;
- cambiar IP requiere restart;
- cambiar credenciales requiere restart;
- eliminar una conexión requiere restart;
- sustituir mapping requiere restart;
- no existe lifecycle dinámico por conexión.

Esto es incompatible con un onboarding moderno.

---

## 3.4. `CompositeAdapter` no representa correctamente múltiples instancias del mismo protocolo

Hoy cada adapter tiene un `adapter_id` estable como:

```text
knx
matter
home_assistant
zigbee2mqtt
mqtt
modbus
```

Pero una instalación real puede contener:

```text
2 gateways KNX
2 brokers MQTT
2 controladores Modbus
2 instancias Home Assistant
```

Por tanto hay que separar:

```text
connector_id
```

de:

```text
connection instance id
```

Ejemplo:

```text
connector_id = knx
connection_id = 019f...
runtime adapter_id = knx:019f...
```

---

# 4. Arquitectura propuesta

## 4.1. Vista general

```text
┌───────────────────────────────────────────────┐
│                 USER / ADMIN UI               │
└────────────────────────┬──────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────┐
│            Connection Control Plane           │
│                                               │
│  discovery                                    │
│  onboarding                                   │
│  authentication                               │
│  pairing                                      │
│  validation                                   │
│  persistence                                  │
│  diagnostics                                  │
│  reconfigure / reauth                         │
│  repair                                       │
└────────────────────────┬──────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────┐
│               ManagedAdapterSet               │
│                                               │
│  attach                                       │
│  replace                                      │
│  detach                                       │
│  health projection                            │
│  existing reconnect supervision               │
└────────────────────────┬──────────────────────┘
                         │
             ┌───────────┼───────────┐
             ▼           ▼           ▼
            KNX          HA        Matter ...
             │           │           │
             └───────────┴───────────┘
                         │
                         ▼
                    AdapterPort
                         │
                         ▼
                 DiscoveryService
                         │
                         ▼
             DeviceRegistry / StateStore
                         │
                         ▼
                    MCP único
```

---

# 5. Nuevo modelo de dominio

## 5.1. ConnectionProfile

Crear:

```text
src/domoai/domain/connections.py
```

Modelo propuesto:

```python
class ConnectionProfile(StrictModel):
    id: UUID
    connector_id: str
    unique_id: str

    endpoint: dict[str, str | int | bool]
    options: dict[str, str | int | float | bool]

    credential_ref: str | None = None
    mapping_ref: str | None = None

    enabled: bool = True
    schema_version: Literal["v1"] = "v1"

    created_at: datetime
    updated_at: datetime
```

Reglas:

- `connector_id` identifica el tipo de tecnología;
- `id` identifica esta conexión concreta;
- `unique_id` evita duplicados;
- `credential_ref` referencia secretos, nunca los contiene;
- `mapping_ref` apunta a mappings persistidos/importados;
- `endpoint` contiene configuración no sensible;
- `options` contiene parámetros técnicos no sensibles.

---

# 6. Máquina de estado de conexión

Separar dos dimensiones.

## 6.1. TransportStatus

```text
UNKNOWN
REACHABLE
CONNECTED
AUTH_REQUIRED
AUTH_FAILED
DEGRADED
UNREACHABLE
INCOMPATIBLE
DISABLED
```

## 6.2. InventoryStatus

```text
UNKNOWN
READY
MAPPING_REQUIRED
PAIRING_REQUIRED
EMPTY
FAILED
```

## 6.3. Estado de producto

La UI puede proyectar:

```text
CONNECTED
NEEDS_SETUP
REAUTH_REQUIRED
DEGRADED
UNAVAILABLE
DISABLED
```

Ejemplo KNX:

```text
transport_status = CONNECTED
inventory_status = MAPPING_REQUIRED
product_status = NEEDS_SETUP
```

Esto es mucho más útil que representar todo como `connected=False`.

---

# 7. Nuevo contrato ConnectionProviderPort

Crear:

```text
src/domoai/runtime/connection_ports.py
```

Propuesta:

```python
class ConnectionProviderPort(Protocol):
    connector_id: str

    async def discover(
        self,
        context: DiscoveryContext,
    ) -> list[ConnectionCandidate]:
        ...

    async def probe(
        self,
        candidate: ConnectionCandidate,
    ) -> ProbeResult:
        ...

    async def begin_setup(
        self,
        candidate: ConnectionCandidate | None,
    ) -> SetupStep:
        ...

    async def submit_setup(
        self,
        session: OnboardingSession,
        values: Mapping[str, str],
    ) -> SetupTransition:
        ...

    async def validate(
        self,
        draft: ConnectionDraft,
        secrets: SecretResolverPort,
    ) -> ValidationResult:
        ...

    def build_adapter(
        self,
        profile: ConnectionProfile,
        secrets: SecretResolverPort,
    ) -> AdapterPort:
        ...

    async def diagnose(
        self,
        profile: ConnectionProfile,
        secrets: SecretResolverPort,
    ) -> DiagnosticsReport:
        ...
```

Separación:

```text
ConnectionProviderPort
        │
        │ configura
        ▼
    AdapterPort
        │
        │ opera
        ▼
runtime semántico
```

---

# 8. ManagedAdapterSet

Crear:

```text
src/domoai/runtime/managed_adapter_set.py
```

Debe implementar `AdapterPort`.

Operaciones nuevas:

```python
async def attach(
    connection_id: UUID,
    adapter: AdapterPort,
) -> None

async def replace(
    connection_id: UUID,
    adapter: AdapterPort,
) -> None

async def detach(
    connection_id: UUID,
) -> None
```

Debe soportar:

```text
0..N adapters
```

No solo 1..N.

---

# 9. Zero-provider boot

Con cero adapters:

```text
discover()
→ AdapterSnapshot vacío

read_state()
→ vacío / unavailable

execute()
→ rejected

health()
→ runtime healthy, physical_sources=0

subscribe_events()
→ stream operativo sin fuentes físicas
```

Nunca se debe volver al simulador de manera implícita.

El simulador debe seguir siendo exclusivamente fixture/dev.

---

# 10. Preservar la reconexión actual

La lógica actual de `CompositeAdapter` para:

- stream failure;
- source unavailable;
- exponential backoff;
- reconnect;
- source reconnected;

es correcta.

No se debe introducir un segundo reconnect loop dentro de ConnectionManager.

Debe existir una sola autoridad operativa:

```text
ManagedAdapterSet
       ↓
reconnect supervisor
       ↓
observed connection status
       ↓
Connection Manager
       ↓
UI
```

---

# 11. Persistencia

Crear migración SQLite:

```text
src/domoai/persistence/migrations/0xx_connection_manager.sql
```

Tabla:

```sql
CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    unique_id TEXT NOT NULL,
    endpoint_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    credential_ref TEXT,
    mapping_ref TEXT,
    enabled INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connector_id, unique_id)
);
```

También:

```text
src/domoai/persistence/connection_repository.py
```

Operaciones:

```text
save
get
get_by_unique_id
list_all
delete
set_enabled
```

---

# 12. SecretStore

Crear:

```text
src/domoai/runtime/secret_ports.py
src/domoai/persistence/connection_secrets.py
```

Interfaz:

```python
class SecretStorePort(Protocol):
    async def put(...) -> str: ...
    async def get(...) -> SecretStr: ...
    async def delete(...) -> None: ...
```

Implementación local:

```text
EncryptedSQLiteSecretStore
```

Requisitos:

- AES-GCM;
- master key fuera de la base de datos;
- nunca devolver secrets en modelos de API;
- nunca registrar secrets;
- nunca incluir secrets en MCP;
- nunca incluir secrets en audit;
- nunca incluir secrets en diagnostics.

---

# 13. Diagnósticos tipados

Crear:

```python
class ConnectionDiagnostic(StrictModel):
    code: str
    severity: DiagnosticSeverity
    retryable: bool
    user_action: str | None
    message: str
    technical_details: dict[str, str | int | bool]
```

Códigos iniciales:

```text
dns_resolution_failed
tcp_connect_timeout
connection_refused
tls_certificate_invalid

authentication_required
authentication_failed
credential_expired

protocol_handshake_failed
schema_version_incompatible

knx_no_tunnel_slot
knx_secure_credentials_required

mqtt_auth_rejected
mqtt_tls_failed

modbus_no_response
modbus_profile_required

mapping_required
mapping_invalid

matter_bluetooth_unavailable
matter_network_credentials_required

no_devices_found
readback_failed

duplicate_connection
```

---

# 14. Home Assistant

## Experiencia objetivo

```text
Home Assistant encontrado
192.168.1.40

[Conectar]
```

Después:

```text
Introducir credencial
        ↓
probar REST
        ↓
probar WebSocket
        ↓
leer entity registry
        ↓
leer device registry
        ↓
guardar
        ↓
attach adapter
        ↓
DiscoveryService.refresh()
```

## Autodiscovery

P1 técnico mínimo:

- mDNS;
- hostname común;
- manual fallback.

## Reauth

Un 401/403 debe producir:

```text
REAUTH_REQUIRED
```

y no:

```text
UNAVAILABLE
```

---

# 15. KNX/IP

## Experiencia objetivo

```text
Interfaces KNX encontradas

✓ Weinzierl IP Interface
✓ MDT IP Interface

[Conectar]
```

El usuario no debe introducir normalmente:

```text
3671
route_back
multicast
interface binding
xknx flags
```

Estos valores pueden existir en:

```text
Opciones avanzadas
```

## Mapping

La conexión KNX y el mapping KNX son problemas distintos.

Flujo:

```text
gateway reachable
        ↓
tunnel connected
        ↓
mapping available?
        │
        ├─ yes → READY
        │
        └─ no → MAPPING_REQUIRED
```

No se deben inferir group addresses de tráfico arbitrario.

## P1

- importar mapping DomoAI;
- importador ETS;
- KNX Secure;
- keyring.

---

# 16. Matter

Separar:

```text
Controller Connection
```

de:

```text
Device Commissioning
```

## Caso A

```text
DomoAI → Matter Server existente
```

## Caso B

```text
DomoAI controla Matter fabric
        ↓
pairing QR/manual code
        ↓
commission
```

No asumir que Home Assistant expone siempre un WebSocket Matter externo.

Cuando Matter está gestionado por HA:

```text
DomoAI → Home Assistant
```

puede ser la ruta recomendada.

---

# 17. Zigbee2MQTT

El adapter existente ya puede aprovechar:

- definitions;
- bridge state;
- availability;
- events;
- readback.

El onboarding debe centrarse en:

```text
broker discovery
credentials
TLS
base topic
validation
```

---

# 18. MQTT genérico

Mantener la regla:

> No inferir nunca un write topic de tráfico arbitrario.

Soporte opcional:

```text
MQTT Discovery
       ↓
validator
       ↓
MqttAdapterMapping v1
       ↓
GenericMqttAdapter
```

El mapping generado debe pasar los mismos validadores actuales.

---

# 19. Modbus

Modbus no es semánticamente autodescriptivo.

No hacer:

```text
scan masivo de registros
        ↓
heurística IA
        ↓
"esto parece temperatura"
```

Sí hacer:

```text
probe endpoint
        ↓
Device Identification si existe
        ↓
vendor + model + revision
        ↓
buscar perfil compatible
        ↓
mapping v1
```

Fallback:

```text
PROFILE_REQUIRED
```

---

# 20. SDK

No romper el SDK actual.

Añadir contrato v2 o sibling contract:

```python
class ConnectionProviderRegistration:
    manifest: ConnectionProviderManifest
    provider_factory: ...
    adapter_factory: ...
```

Factory:

```python
adapter_factory(
    profile: ConnectionProfile,
    secret_resolver: SecretResolverPort,
) -> AdapterPort
```

Los plugins futuros podrán aportar:

```text
discovery
onboarding
validation
diagnostics
adapter
```

sin crear un MCP paralelo.

---

# 21. Migración legacy

Mantener:

```text
DOMOAI_HOME_ASSISTANT_*
DOMOAI_KNX_*
DOMOAI_MODBUS_*
DOMOAI_MQTT_*
DOMOAI_MATTER_*
```

durante la transición.

Crear:

```text
src/domoai/application/legacy_connection_import.py
```

Comportamiento:

```text
si existen ConnectionProfiles:
    usar persistidos
else:
    leer Settings legacy
    crear perfiles runtime
```

Los secretos no deben copiarse automáticamente.

Ejemplo:

```text
credential_ref =
env:DOMOAI_HOME_ASSISTANT_TOKEN
```

---

# 22. API de gestión

No exponer configuración de credenciales mediante tools MCP semánticas.

Crear un plano administrativo separado.

Posible API local:

```text
GET    /api/connections
POST   /api/connections/discover
POST   /api/connections
POST   /api/connections/{id}/validate
POST   /api/connections/{id}/reauth
POST   /api/connections/{id}/repair
PATCH  /api/connections/{id}
DELETE /api/connections/{id}
```

Restricciones:

- local-only por defecto;
- autenticación si sale de loopback;
- CSRF/origin protections si existe UI web;
- no incluir secrets en responses.

---

# 23. Definition of Done global

La iniciativa no se considera completa hasta cumplir:

| Caso | Resultado |
|---|---|
| DomoAI recién instalado sin `DOMOAI_*` | Gateway arranca |
| Sin fuentes físicas | runtime fail-closed |
| HA visible en LAN | aparece en discovery |
| Token HA incorrecto | diagnóstico de autenticación |
| IP HA cambia | reconfigure sin borrar casa |
| KNX/IP visible | usuario no introduce 3671 |
| KNX sin mapping | `MAPPING_REQUIRED` |
| KNX cae | `DEGRADED/UNAVAILABLE` |
| KNX vuelve | auto-reconnect |
| Añadir conexión | no restart |
| Eliminar conexión | no restart |
| Cambiar mapping | reemplazo solo de esa conexión |
| Dos KNX | rutas independientes |
| Dos MQTT | rutas independientes |
| MQTT desconocido | cero writes inferidos |
| Modbus desconocido | perfil requerido |
| Matter HA-managed | no requiere publicar Matter server |
| secrets | nunca aparecen en audit/MCP/API |
| configuración legacy | sigue funcionando |

---

# 24. Plan detallado de implementación

# Fase P0 — Foundation

## P0.1 — Crear dominio de conexiones

### Objetivo

Definir el contrato común para cualquier conexión física.

### Tareas

- [x] Crear `src/domoai/domain/connections.py`.
- [x] Crear `ConnectionProfile`.
- [x] Crear `ConnectionCandidate`.
- [x] Crear `ConnectionDraft`.
- [x] Crear `ConnectionDiagnostic`.
- [x] Crear enums de estado.
- [x] Crear `ValidationResult`.
- [x] Crear `ProbeResult`.
- [x] Crear `OnboardingSession`.
- [x] Crear `SetupStep`.
- [x] Crear `SetupTransition`.
- [x] Validar `connector_id`.
- [x] Validar `unique_id`.
- [x] Prohibir campos sensibles en `endpoint`.
- [x] Prohibir campos sensibles en `options`.

### Dependencias

Ninguna.

### Criterios de aceptación

- modelos strict;
- `extra="forbid"`;
- no aceptan secretos en metadata;
- serialización estable;
- tests unitarios.

### Tests

```text
tests/unit/domain/test_connections.py
```

Casos:

- modelo válido;
- connector inválido;
- unique_id vacío;
- duplicate diagnostic code;
- intento de `password` en endpoint;
- intento de `token` en options.

### Estado de implementación

P0.1 completada con modelos strict, validación de metadata sensible y tests
unitarios en `tests/unit/domain/test_connections.py`.

---

## P0.2 — Persistencia

### Tareas

- [x] Crear migración SQL.
- [x] Crear `ConnectionRepository`.
- [x] Implementar `save`.
- [x] Implementar `get`.
- [x] Implementar `get_by_unique_id`.
- [x] Implementar `list_all`.
- [x] Implementar `delete`.
- [x] Implementar `set_enabled`.
- [x] Garantizar atomicidad.
- [x] Añadir índices.
- [x] Añadir UNIQUE connector + unique_id.

### Dependencias

P0.1.

### Criterios de aceptación

- restart conserva conexiones;
- duplicados rechazados;
- migración es aditiva;
- backup actual incluye tabla automáticamente.

### Tests

```text
tests/unit/persistence/test_connection_repository.py
tests/integration/test_connection_persistence.py
```

### Estado de implementación

P0.2 completada con migración SQLite aditiva `018_connection_manager.sql`,
`ConnectionRepository` transaccional y cobertura de reinicio, duplicados,
rollback y backup.

---

## P0.3 — SecretStore

### Tareas

- [x] Crear `SecretStorePort`.
- [x] Crear `SecretResolverPort`.
- [x] Crear almacenamiento cifrado.
- [x] Definir formato `secret:<uuid>`.
- [x] Definir formato `env:<VARIABLE>`.
- [x] Añadir AES-GCM.
- [x] Definir master key file.
- [x] Permisos de fichero estrictos.
- [x] No retornar secrets en repr.
- [x] Redactar excepciones.
- [x] Añadir rotación futura como extensión.

### Dependencias

P0.1.

### Criterios de aceptación

- plaintext no aparece en DB;
- plaintext no aparece en logs;
- plaintext no aparece en diagnostics;
- `env:` funciona.

### Tests

```text
tests/security/test_connection_secret_store.py
```

### Estado de implementación

P0.3 completada con `FileSecretStore`, AES-GCM, referencias `secret:`/`env:`,
permisos fail-closed, escritura atómica y errores tipados sin plaintext.
Verificada con `tests/security/test_connection_secret_store.py`.

---

# Fase P0 — Runtime dinámico

## P0.4 — ManagedAdapterSet

### Objetivo

Permitir 0..N conexiones dinámicas.

### Tareas

- [x] Crear `ManagedAdapterSet`.
- [x] Implementar `AdapterPort`.
- [x] Portar lógica segura de `CompositeAdapter`.
- [x] Soportar cero hijos.
- [x] Implementar `attach`.
- [x] Implementar `replace`.
- [x] Implementar `detach`.
- [x] Mantener colas de eventos.
- [x] Mantener backpressure.
- [x] Mantener coalescing.
- [x] Mantener diagnostics.
- [x] Mantener reconnection backoff.
- [x] Cancelar tasks de child al detach.
- [x] Desconectar child al replace.
- [x] Proteger mutaciones con lock.
- [x] Mantener registry unavailable semantics.

### Dependencias

P0.1.

### Criterios de aceptación

- runtime vacío operativo;
- attach sin restart;
- detach sin restart;
- replace sin restart;
- reconnect igual que antes;
- ningún event pump huérfano.

### Tests

```text
tests/unit/runtime/test_managed_adapter_set.py
tests/integration/test_dynamic_adapter_lifecycle.py
tests/performance/test_dynamic_adapter_events.py
```

Casos:

- 0 adapters;
- 1 adapter;
- 5 adapters;
- attach;
- detach;
- replace;
- adapter que falla;
- adapter que vuelve;
- event stream muerto;
- repeated replace;
- cancellation.

### Estado de implementación

P0.4 completada con lifecycle mutable serializado, event pumps con ownership
explícito, colas acotadas, coalescing, backoff y semántica unavailable.
Verificada con las pruebas unitarias, de integración y de rendimiento indicadas.

---

## P0.5 — Identidad de instancia

### Tareas

- [x] Definir `connector_id`.
- [x] Definir `connection_id`.
- [x] Definir runtime `adapter_id`.
- [x] Permitir adapter_id dinámico en built-ins.
- [x] Mantener IDs legacy.
- [x] Garantizar SourceRef estable.
- [x] Actualizar diagnostics.
- [x] Actualizar coverage.
- [x] Actualizar registry expectations.
- [x] Añadir pruebas para dos adapters del mismo tipo.

### Dependencias

P0.4.

### Criterios de aceptación

```text
knx:<id-a>
knx:<id-b>
```

pueden coexistir.

### Estado de implementación

P0.5 completada con `connection:<uuid>` como identidad runtime y remapeo de
`SourceRef` por instancia en HA/KNX. Verificada con
`tests/contract/test_multi_instance_same_connector.py`.

### Tests

```text
tests/contract/test_multi_instance_same_connector.py
```

---

## P0.6 — Zero-provider boot

### Tareas

- [x] Eliminar requisito físico de startup.
- [x] Mantener simulator únicamente fixture.
- [x] Crear ManagedAdapterSet vacío.
- [x] Permitir iniciar gateway.
- [x] Exponer `physical_sources=0`.
- [x] Rechazar ejecución física.
- [x] Permitir administración.

### Dependencias

P0.4.

### Criterios de aceptación

```bash
unset DOMOAI_HOME_ASSISTANT_URL
unset DOMOAI_KNX_GATEWAY_HOST
unset DOMOAI_MODBUS_HOST
...
uv run domoai-mcp-gateway
```

debe iniciar correctamente.

### Estado de implementación

P0.6 completada: el entrypoint del gateway usa un `ManagedAdapterSet` vacío,
expone `physical_sources=0`, mantiene `/healthz`/`/readyz` sanitizados y
rechaza comandos físicos hasta activar una conexión. Verificada en
`tests/integration/test_zero_provider_boot.py`.

---

# Fase P0 — Connection Control Plane

## P0.7 — ConnectionProviderPort

### Tareas

- [x] Crear interfaz.
- [x] Crear registry de connection providers.
- [x] Asociar connector_id → provider.
- [x] Crear manifest v1.
- [x] Implementar `discover`.
- [x] Implementar `probe`.
- [x] Implementar `validate`.
- [x] Implementar `build_adapter`.
- [x] Implementar `diagnose`.
- [x] Soportar onboarding steps.

### Dependencias

P0.1, P0.3.

### Criterios de aceptación

Un provider fixture puede recorrer:

```text
discover
→ select
→ setup
→ validate
→ build
```

### Estado de implementación

P0.7 completada con manifest/registry v1, sesiones acotadas
`discover → select → setup → validate → build`, provider fixture y contratos
secret-safe.

---

## P0.8 — ConnectionManager

Crear:

```text
src/domoai/application/connection_manager.py
```

### Tareas

- [x] `discover_connections()`.
- [x] `create_connection()`.
- [x] `validate_connection()`.
- [x] `activate_connection()`.
- [x] `replace_connection()`.
- [x] `disable_connection()`.
- [x] `delete_connection()`.
- [x] `reauth_connection()`.
- [x] `diagnose_connection()`.
- [x] `repair_connection()`.
- [x] persist before activation;
- [x] rollback si attach falla;
- [x] audit sanitized;
- [x] refresh inventory tras attach;
- [x] mark unavailable tras detach.

### Dependencias

P0.2, P0.3, P0.4, P0.7.

### Criterios de aceptación

Crear una conexión fixture no requiere restart.

### Estado de implementación

P0.8 completada con `ConnectionManager`, SQLite serializado, persistencia antes
de attach, compensación de errores, CRUD/control, reauth, reconfiguración,
diagnostics y auditoría sanitizada.

---

# Fase P0 — Home Assistant

## P0.9 — Home Assistant Connection Provider

### Tareas

- [x] Crear `adapters/home_assistant/connection.py`.
- [x] Implementar manual candidate.
- [x] Implementar mDNS discovery.
- [x] Detectar base URL.
- [x] Pedir token.
- [x] Probar REST.
- [x] Probar WebSocket.
- [x] Leer registry.
- [x] Obtener unique_id estable.
- [x] Emitir auth errors.
- [x] Emitir TLS errors.
- [x] Emitir timeout errors.
- [x] Crear ConnectionProfile.
- [x] Build existing `HomeAssistantProviderAdapter`.
- [x] Soportar reauth.
- [x] Soportar reconfigure.

### Dependencias

P0.8.

### Criterios de aceptación

El usuario solo necesita:

```text
seleccionar HA
+
autorizar/introducir credencial
```

si discovery funciona.

### Tests

```text
tests/unit/adapters/home_assistant/test_connection_provider.py
tests/integration/test_home_assistant_onboarding.py
```

### Estado de implementación

P0.9 completada con candidates manuales/mDNS, validación REST + WebSocket,
registry, identidad por conexión, diagnósticos auth/TLS/timeout y construcción
del `HomeAssistantProviderAdapter` existente. Reauth y reconfiguración quedan
coordinadas por `ConnectionManager`.

---

# Fase P0 — KNX

## P0.10 — KNX Connection Provider

### Tareas

- [x] Crear `adapters/knx/connection.py`.
- [x] Añadir gateway discovery.
- [x] Enumerar interfaces.
- [x] Identificar tunnelling capability.
- [x] Seleccionar gateway.
- [x] Autoseleccionar puerto.
- [x] Ocultar route_back por defecto.
- [x] Detectar no tunnel slot.
- [x] Detectar secure-required.
- [x] Separar transport setup y mapping.
- [x] Crear estado `MAPPING_REQUIRED`.
- [x] Importar mapping DomoAI existente.
- [x] Validar mapping.
- [x] Build existing `KnxAdapter`.

### Dependencias

P0.8, P0.5.

### Criterios de aceptación

Caso normal:

```text
usuario selecciona KNX gateway
usuario importa proyecto/mapping
fin
```

Sin pedir manualmente:

```text
3671
xknx
flags internos
```

### Estado de implementación

P0.10 completada con discovery inyectable de gateway/interfaces, defaults
seguros de puerto y route-back, diagnósticos de tunnelling/secure/tunnel-slot,
estado `MAPPING_REQUIRED`, validación de mapping y construcción del adapter KNX
existente.

---

# Fase P0 — Administración local

## P0.11 — Management API

### Tareas

- [x] endpoints de connections;
- [x] endpoint discovery;
- [x] setup session;
- [x] validation;
- [x] diagnostics;
- [x] reauth;
- [x] repair;
- [x] delete;
- [x] disable;
- [x] local-only default;
- [x] response redaction;
- [x] auth si non-loopback;
- [x] rate limiting básico;
- [x] audit.

### Dependencias

P0.8.

### Estado de implementación

P0.11 completada con API Starlette versionada, CRUD/control, discovery,
onboarding por sesión, reauth, repair, diagnostics, reconfiguración, delete,
CSRF/origin local, autenticación HTTPS fuera de loopback, rate limiting acotado
y auditoría desde el manager.

---

## P0.12 — UI mínima

### Pantallas

- [x] primera instalación;
- [x] sistemas encontrados;
- [x] añadir manualmente;
- [x] introducir credenciales;
- [x] seleccionar mapping;
- [x] conexión correcta;
- [x] conexiones existentes;
- [x] degraded;
- [x] reauth;
- [x] diagnostics;
- [x] remove;
- [x] advanced settings.

### Dependencias

P0.11.

### UX target

```text
Sistemas encontrados

Home Assistant
192.168.1.40
[Conectar]

KNX/IP — Weinzierl
192.168.1.50
[Conectar]

[+ Añadir manualmente]
```

### Estado de implementación

P0.12 completada como superficie HTML local sin build frontend, con estados de
primera instalación, discovery, alta manual, credenciales, mapping, éxito,
conexiones existentes, degraded, reauth, diagnostics, remove y advanced settings.

---

# Fase P0 — Compatibilidad legacy

## P0.13 — Legacy Settings Importer

### Tareas

- [x] detectar HA legacy;
- [x] detectar KNX legacy;
- [x] detectar Matter legacy;
- [x] detectar MQTT legacy;
- [x] detectar Zigbee2MQTT legacy;
- [x] detectar Modbus legacy;
- [x] convertir a runtime profiles;
- [x] usar `env:` para secrets;
- [x] mantener comportamiento actual;
- [x] warnings deprecados solo cuando proceda.

### Dependencias

P0.8.

### Criterios de aceptación

Los tests actuales de configuración deben seguir pasando.

### Estado de implementación

P0.13 completada con importación explícita y no destructiva de las seis familias
legacy, perfiles deshabilitados, referencias `env:`/`secret:`, warnings de
configuración incompleta/deprecada y persistencia administrativa sin modificar
la ruta legacy existente.

---

# Fase P1 — Zigbee2MQTT

## P1.1 — Broker onboarding

### Tareas

- [x] broker discovery si disponible;
- [x] manual fallback;
- [x] username/password;
- [x] TLS;
- [x] cert validation;
- [x] base topic discovery;
- [x] detectar bridge online;
- [x] build Zigbee2MqttAdapter.

### Estado de implementación

P1.1 completada con proveedor gestionado `zigbee2mqtt`, fallback manual,
discovery opcional inyectable, validación MQTT/TLS/auth, secretos protegidos por
SecretStore, detección de retained bridge/base topic, diagnósticos sanitizados,
activación en `ManagedAdapterSet` e identidad runtime por instancia. El camino
legacy `DOMOAI_ZIGBEE2MQTT_*` se mantiene sin cambios.

P1.2–P1.8 están implementadas y verificadas mediante contratos, tests de
seguridad, integración y composición. Las superficies de operador se mantienen
local-first: MQTT Discovery expone preview y persistencia con confirmación
explícita; Matter commissioning expone inicio/confirmación/cleanup sin devolver
el código; y los providers externos se cargan de forma aislada por entry point.

---

# Fase P1 — MQTT genérico

## P1.2 — MQTT Discovery importer

### Tareas

- [x] leer discovery prefix;
- [x] parsear payloads soportados;
- [x] generar mapping DomoAI;
- [x] validar mapping;
- [x] prohibir plantillas ejecutables;
- [x] prohibir wildcard writes;
- [x] presentar preview al usuario;
- [x] guardar mapping_ref.

### Criterio crítico

Ninguna publicación física hasta que el mapping sea explícito y validado.

---

# Fase P1 — Matter

## P1.3 — Matter controller onboarding

### Tareas

- [x] descubrir Matter Server local;
- [x] validar schema;
- [x] distinguir Python/matter.js server;
- [x] conectar controller;
- [x] listar nodos;
- [x] build MatterServerAdapter.

---

## P1.4 — Matter commissioning

### Tareas

- [x] definir commissioning flow;
- [x] QR code;
- [x] manual code;
- [x] Bluetooth availability;
- [x] Wi-Fi credentials;
- [x] Thread prerequisites;
- [x] commissioned node confirmation;
- [x] timeout;
- [x] cleanup de session fallida.

### Restricción

Solo si DomoAI controla la fabric/controller correspondiente.

---

# Fase P1 — Modbus

## P1.5 — Device identification

### Tareas

- [x] probe TCP;
- [x] Read Device Identification;
- [x] vendor;
- [x] product code;
- [x] revision;
- [x] unit-id selection segura;
- [x] no scan agresivo por defecto.

---

## P1.6 — Modbus Profile Catalog

### Tareas

- [x] definir profile schema;
- [x] vendor/model matching;
- [x] firmware constraints;
- [x] generar ModbusMappingDocument;
- [x] signature/hash de profile;
- [x] unknown device → PROFILE_REQUIRED.

---

# Fase P1 — SDK

## P1.7 — Connection Provider SDK

### Tareas

- [x] manifest;
- [x] registry;
- [x] entry-point group;
- [x] provider factory;
- [x] adapter factory con profile;
- [x] conformance tests;
- [x] diagnostics validation;
- [x] secret-safe metadata;
- [x] documentation.

---

# Fase P1 — Observabilidad

## P1.8 — Connection metrics

Métricas:

```text
connection_total
connection_connected
connection_degraded
connection_unavailable

connection_reconnect_attempt_total
connection_reconnect_success_total

connection_auth_failure_total
connection_discovery_duration_seconds
connection_validation_duration_seconds
```

Implementadas en `RuntimeOperationalMetrics`, el `ConnectionManager`, el
coordinador de onboarding y el collector Prometheus. Las labels se validan con
allowlist y límites de cardinalidad; el snapshot legacy sólo añade la sección
`connections` cuando existe una muestra de lifecycle.

No labels con:

```text
IP completa
token
password
serial sensible
```

---

# Fase P2 — KNX avanzado

## P2.1 — ETS importer

- [x] importar project metadata;
- [x] group addresses;
- [x] DPT;
- [x] nombres;
- [x] áreas;
- [x] generar mapping DomoAI;
- [x] review manual antes de writes.

---

## P2.2 — KNX Secure

- [x] keyring;
- [x] IP Secure;
- [x] Data Secure;
- [x] credential store;
- [x] rotation;
- [x] diagnostics.

---

# Fase P2 — Home Assistant OAuth

## P2.3

- [x] OAuth flow;
- [x] token refresh;
- [x] revocation;
- [x] reauth;
- [x] migration desde long-lived token.

---

# Fase P2 — Repair Engine

## P2.4

Crear acciones reparables tipadas:

```text
[x] reauthenticate
[x] retry
[x] select_interface
[x] replace_endpoint
[x] import_mapping
[x] select_profile
[x] trust_certificate
[x] update_credentials
```

Nunca ejecutar cambios de riesgo automáticamente sin confirmación.

### Estado de cierre P2 (2026-09-08)

Las cuatro superficies P2 están implementadas en el runtime, API local,
secret store, schemas v1 y documentación operativa. La suite focalizada del
control plane pasa con `55 passed`; la composición del proyecto pasa con
`577 passed, 18 skipped` usando un `DOCKER_CONFIG` efímero.

La qualification externa permanece separada de la validación software: el
handshake KNX Secure necesita un gateway comisionado, OAuth necesita un
proveedor Home Assistant real y ETS necesita proyectos de fabricantes. Sin
esas dependencias, la evidencia se conserva como bloqueada y fail-closed.

---

# 25. Orden recomendado de PRs

Para reducir blast radius:

## PR 1

```text
domain connections
+
repository
+
tests
```

## PR 2

```text
SecretStore
+
security tests
```

## PR 3

```text
ManagedAdapterSet
+
zero-provider behavior
```

## PR 4

```text
runtime_factory integration
+
legacy compatibility
```

## PR 5

```text
ConnectionProviderPort
+
fixture provider
```

## PR 6

```text
ConnectionManager
```

## PR 7

```text
Home Assistant provider
+
onboarding integration tests
```

## PR 8

```text
KNX provider
+
gateway discovery
+
mapping-required state
```

## PR 9

```text
management API
```

## PR 10

```text
minimal onboarding UI
```

Después comenzar P1.

---

# 26. Gates de CI

Cada PR debe mantener:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
uv lock --check
```

Añadir además:

```bash
uv run pytest -q tests/connection
uv run pytest -q tests/security/test_connection_secret_store.py
uv run pytest -q tests/integration/test_dynamic_adapter_lifecycle.py
```

---

# 27. Criterios arquitectónicos no negociables

## 27.1. Un solo MCP

No crear:

```text
knx-mcp
mqtt-mcp
matter-mcp
```

Todos siguen debajo del MCP universal.

## 27.2. No crear una segunda ruta de ejecución

Connection Manager configura conexiones.

No ejecuta comandos físicos directamente.

Toda escritura continúa:

```text
PlanService
→ policy
→ approval
→ admission
→ executor
→ adapter
→ readback
```

## 27.3. No inferir writes

Nunca inferir:

- MQTT command topics;
- KNX group writes;
- Modbus writable registers;
- Matter vendor commands.

## 27.4. Secrets nunca atraviesan el dominio semántico

No deben aparecer en:

- Device;
- SourceRef;
- StateSnapshot;
- Command;
- ExecutionOutcome;
- Audit;
- MCP;
- diagnostics públicos.

## 27.5. Discovery no equivale a autorización

Encontrar un gateway no concede:

- escritura;
- consentimiento;
- qualification;
- approval;
- authority.

## 27.6. Mapping no equivale a qualification física

Un mapping válido no certifica:

- cableado;
- firmware;
- fabricante;
- tolerancias;
- funcionamiento real.

---

# 28. Resultado final esperado

La experiencia de instalación debe evolucionar desde:

```text
export DOMOAI_KNX_GATEWAY_HOST=...
export DOMOAI_KNX_GATEWAY_PORT=3671
export DOMOAI_KNX_CONFIG_PATH=...
...
```

a:

```text
DomoAI
  ↓
Configura tu casa
  ↓
Sistemas encontrados
  ↓
KNX/IP — Weinzierl
  ↓
Conectar
  ↓
Importar proyecto
  ↓
37 dispositivos compatibles
  ↓
Finalizar
```

Y para Home Assistant:

```text
DomoAI
  ↓
Home Assistant encontrado
  ↓
Conectar
  ↓
Autorizar
  ↓
143 dispositivos detectados
  ↓
Finalizar
```

La complejidad física continúa existiendo.

Lo que cambia es quién debe gestionarla:

```text
ANTES
usuario → protocolo

DESPUÉS
usuario → DomoAI → Connection Provider → protocolo
```

Ese cambio es el que convierte la arquitectura universal ya existente en una experiencia de producto verdaderamente universal.

---

# 29. Crear conectores sin repetir infraestructura

El contrato de proveedor sigue teniendo cinco puntos de extensión:

```text
discover → probe → validate → build_adapter → diagnose
```

Eso no significa que cada cliente tenga que escribir cinco sistemas desde
cero. El SDK incluye una base común y un generador:

```bash
uv run domoai-admin connector create \
  --connector-id mi_conector \
  --display-name "Mi conector" \
  --protocol mi-protocolo \
  --output ./mi-conector
uv run domoai-admin connector check ./mi-conector
```

El proyecto generado trae el `ConnectionProviderManifest`, los cinco métodos
asíncronos, el punto donde se implementa el adaptador, el entry point de
Python, una prueba de contrato y la documentación. La base compartida ofrece
`manual_candidate`, diagnósticos seguros y un estado explícito
`provider_adapter_not_implemented`; por tanto, un scaffold no puede
presentarse como conectado hasta que el autor implemente el protocolo real.

## 29.1 Catálogo y onboarding genérico

Cada proveedor declara sus campos de configuración una sola vez en el
manifest mediante `SetupFieldDescriptor`. El endpoint
`GET /api/v1/connection-providers` devuelve únicamente esa descripción sin
valores. La interfaz de gestión genera el formulario desde el catálogo, de
modo que un conector nuevo no necesita una pantalla nueva: el operador puede
descubrirlo o introducir endpoint y credencial, y el flujo continúa por
`ConnectionManager` y `FileSecretStore`.

## 29.2 Home Assistant OAuth

El endpoint `POST /api/v1/home-assistant/oauth/discover` recibe una URL base y
prueba únicamente los documentos estándar OAuth Authorization Server y OpenID
Connect. La respuesta está limitada, valida URLs sin credenciales y, si no hay
metadatos utilizables, devuelve `oauth_metadata_unavailable` para mostrar el
formulario manual. El `client_id` y el `redirect_uri` siguen perteneciendo al
registro de la aplicación externa.

Al iniciar OAuth, la configuración validada queda ligada al `state` efímero.
El callback sólo necesita `state` y `code`; los tokens siguen viajando
exclusivamente por el límite del secreto y la respuesta pública contiene sólo
la referencia opaca y sus metadatos.

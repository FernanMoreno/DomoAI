# Recetas declarativas

Una receta es un contrato público que describe cómo mapear un producto a los
conceptos de DomoAI. No es un script y no tiene permisos para abrir sockets,
intercambiar tokens o ejecutar comandos arbitrarios.

## Campos comunes

| Campo | Función |
| --- | --- |
| `schema_version` | Versión del formato de receta. |
| `connector_id` | Identificador estable del proveedor. |
| `display_name` | Nombre visible, sin secretos. |
| `device_type` | Tipo normalizado principal. |
| `transport_family` | Engine que procesará la receta. |
| `discovery_strategies` | Estrategias permitidas, incluida `manual`. |
| `auth_schemes` | Forma de autenticación, sin incluir el secreto. |
| `capabilities` | Capacidades públicas que recibirá el agente. |
| `protocol_profile` | Mapping tipado específico de Modbus, KNX o Matter. |

## Capacidades

Cada capacidad debe tener un ID estable y una clase compatible con su valor:

```yaml
capabilities:
  - capability_id: brightness
    kind: integer
    unit: '%'
    minimum: 0
    maximum: 100
    read_path: device://brightness
```

Las capacidades declaradas en `protocol_profile` deben coincidir exactamente
con los IDs de `capabilities`. Así se evita que un mapping físico cree una
capacidad que no está autorizada en el contrato público.

## Lectura, escritura y readback

Una lectura describe de dónde sale el estado. Una escritura describe qué
operación está permitida. Toda escritura que cambia estado debe indicar cómo
se confirma el resultado mediante readback.

```text
read state -> command -> readback -> normalized StateSnapshot
```

Si no se puede describir un readback fiable, la operación debe permanecer sólo
de lectura o ser rechazada durante la validación.

## Validación antes del transporte

Pydantic valida la receta antes de construir un transport. Entre otras reglas,
se comprueban:

- IDs, tipos y límites;
- unicidad de entidades, capacidades y comandos;
- compatibilidad entre el protocolo y el profile;
- rangos de registros y group addresses;
- DPTs y clusters conocidos;
- ausencia de credenciales;
- allowlists de operaciones;
- readback de las escrituras.

El registry de engines también comprueba que exista un discovery o transport
compatible, sin hacer red durante esa validación.

## Qué no admite una receta

No admite código ejecutable embebido, expresiones con acceso a red, secretos,
credenciales dentro de URLs, comodines de discovery sin límites ni comandos
fuera de las allowlists del protocolo.

## Ejemplos oficiales

- [`modbus-temperature.yaml`](../../../examples/connectors/modbus-temperature.yaml)
- [`knx-light.yaml`](../../../examples/connectors/knx-light.yaml)
- [`matter-light.yaml`](../../../examples/connectors/matter-light.yaml)

Los ejemplos son intencionadamente secret-free y sirven como punto de partida,
no como garantía de que un modelo físico concreto use exactamente ese mapa.

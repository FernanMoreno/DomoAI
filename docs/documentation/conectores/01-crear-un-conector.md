# Crear un conector paso a paso

Esta es la ruta recomendada para añadir un producto que utiliza un protocolo
ya soportado.

## 1. Elegir el engine

Selecciona uno de estos valores en `transport_family`:

```text
mqtt | rest | modbus | knx | matter
```

Si el producto necesita HTTP, la receta usa `rest`; el endpoint operativo debe
ser HTTPS salvo que el entorno y la política permitan explícitamente una
excepción local.

## 2. Escribir la receta pública

La receta contiene sólo mapping y metadatos no sensibles. Como mínimo,
declara:

```yaml
schema_version: v1
connector_id: acme_light
display_name: Acme Light
device_type: light
transport_family: mqtt
discovery_strategies:
  - manual
auth_schemes:
  - username_password
capabilities:
  - capability_id: power
    kind: boolean
    state_topic: acme/{device_id}/state
    command:
      name: power
      command_topic: acme/{device_id}/set
      payload_template: '{value}'
      readback_topic: acme/{device_id}/state
```

Para Modbus, KNX y Matter hay además un `protocol_profile` tipado. Consulta
los [ejemplos del repositorio](../../../examples/connectors/) y las guías de
[protocolos](../protocolos/01-mqtt-rest.md).

## 3. Generar el paquete

```bash
uv run domoai-admin connector create \
  --recipe ./recipe.yaml \
  --output ./acme-light-connector
```

El comando genera:

- `pyproject.toml` con el entry point de DomoAI;
- el provider y su manifest;
- el adapter boundary;
- una prueba inicial;
- una copia de `recipe.yaml`;
- la carga tipada de la receta;
- discovery y transport engine para Modbus, KNX y Matter cuando corresponda.

## 4. Comprobar el scaffolding

```bash
uv run domoai-admin connector check ./acme-light-connector
```

El estado `incomplete` es correcto si todavía no se ha suministrado el
transport real. Significa que el paquete y su contrato fueron generados, no
que el dispositivo esté listo para producción.

## 5. Configurar el despliegue

El despliegue aporta el endpoint, el modo de seguridad y las referencias a
secretos mediante el flujo normal de `ConnectionSetup`. El recipe no debe
contener contraseñas, tokens, claves, URLs con credenciales ni códigos de
commissioning.

Para Modbus, KNX y Matter, la fábrica de transporte se inyecta en el borde de
despliegue. Esto permite que el mismo mapping sea público y portable sin
convertir la receta en código con acceso a red.

## 6. Probar y cualificar

Prueba primero con los transports in-memory y los tests de contrato. Después
ejecuta una prueba atendida con el endpoint y el dispositivo real. El resultado
externo debe registrarse como una gate separada del `software_status`.

## Errores frecuentes

- Usar `+` o `#` para descubrir topics MQTT sin límite.
- Declarar una escritura sin readback.
- Copiar un token en `recipe.yaml`.
- Usar una dirección KNX sin DPT.
- Exponer un cluster Matter o un comando vendor sin allowlist.
- Confundir `connector_id` del YAML con el ID del scaffold.

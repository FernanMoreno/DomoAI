# MQTT y REST/OpenAPI

MQTT y REST son los engines más sencillos para empezar cuando el producto
expone una API estable o topics conocidos.

## MQTT

Una receta MQTT declara topics de estado y, si procede, un topic de comando:

```yaml
transport_family: mqtt
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

Reglas importantes:

- sólo se permiten las variables declaradas por el contrato, como
  `{device_id}` y `{value}` donde corresponda;
- discovery debe estar limitado a topics explícitos;
- los comodines `+` y `#` no se usan para ingerir un broker completo;
- los mensajes se convierten a capacidades normalizadas;
- una escritura debe poder confirmarse mediante estado o readback.

El broker, puerto, TLS, usuario y referencia al secreto llegan desde
`ConnectionSetup` y el secret store.

## REST/OpenAPI

REST se usa para APIs HTTP/HTTPS. El mapping necesita una operación allowlisted
y una ruta JSON conocida:

```yaml
transport_family: rest
auth_schemes:
  - token
capabilities:
  - capability_id: temperature
    kind: number
    unit: °C
    read_path: /devices/{device_id}
    read_json_path: $.temperature
```

El perfil OpenAPI limita los métodos, paths, parámetros, payloads y schemas de
respuesta. Las escrituras requieren método, payload y readback explícitos.

Reglas operativas:

- se selecciona el endpoint permitido durante onboarding;
- no se siguen redirects arbitrarios;
- se acotan tamaño de respuesta y paths;
- no se permite una URL con credenciales;
- el token permanece en el secret store.

## Cuándo subir a custom provider

Si MQTT necesita pairing propietario o REST requiere una secuencia dinámica no
representable por una allowlist, añade un profile específico o usa el provider
personalizado. No conviertas una API genérica en un conjunto de llamadas
arbitrarias sólo para evitar ese límite.

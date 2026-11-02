# Modbus declarativo

La receta Modbus describe el mapa físico que DomoAI puede leer o escribir. El
engine compila el profile tipado a la mapping document del adapter Modbus
nativo.

## Ejemplo mínimo

```yaml
transport_family: modbus
capabilities:
  - capability_id: temperature
    kind: number
    unit: °C
    read_path: modbus://temperature
protocol_profile:
  transport: modbus
  unit_ids: [1]
  entities:
    - entity_id: temperature
      name: Temperature
      semantic_type: sensor
      unit_id: 1
      capabilities:
        - name: temperature
          state:
            area: input_register
            address: 20
            data_type: int16
            scale: 0.1
```

## Qué se declara

- `unit_id` del dispositivo Modbus;
- área y dirección del registro;
- tipo de dato y codec;
- escala y offset finitos;
- unidad y semántica compatibles;
- operación de comando, si existe;
- readback de toda escritura.

## Validaciones de seguridad

El profile rechaza IDs y direcciones fuera de rango, escalas no finitas o
nulas, combinaciones de tipo incompatibles, capacidades duplicadas y comandos
sin readback. La validación ocurre antes de construir el transport.

La implementación actual conserva la limitación del adapter nativo: el
readback de una receta Modbus debe corresponder al punto de estado que el
adapter puede leer. Un readback en un registro distinto requiere ampliar el
adapter nativo antes de declararlo soportado.

## Conexión operativa

La discovery declarativa genera un candidato manual con `host` y `port` (por
defecto, `502`). La fábrica de `ModbusRecipeTransport` recibe ese endpoint y
debe ser suministrada por el despliegue. La receta nunca contiene credenciales
ni abre la conexión por sí misma.

## Qualification

El software puede probar codec, mapping, rangos, comandos y readback con un
transport in-memory. La qualification externa debe verificar el registro real,
la unidad, el valor escalado, los límites y el readback contra el dispositivo
concreto.

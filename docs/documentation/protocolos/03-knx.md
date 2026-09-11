# KNX declarativo

La receta KNX declara group addresses, DPTs y la relación entre estado,
comando y readback. El engine compila el profile a la mapping document del
adapter KNX nativo.

## Ejemplo mínimo

```yaml
transport_family: knx
protocol_profile:
  transport: knx
  secure_mode: none
  group_dpts:
    1/1/1: '1.001'
    1/1/2: '1.001'
  entities:
    - entity_id: living-room-light
      name: Living room light
      semantic_type: light
      capabilities:
        - name: power
          dpt: '1.001'
          state_group_address: 1/1/1
          command_group_address: 1/1/2
          readback_group_address: 1/1/1
```

## Qué se declara

- dirección de grupo con formato válido;
- DPT soportado para cada capacidad;
- `state_group_address` para lectura;
- `command_group_address` para escritura;
- `readback_group_address` para confirmar la escritura;
- `secure_mode` y mapping consistente en `group_dpts`.

## Validaciones de seguridad

Se rechazan group addresses inválidas, el broadcast `0/0/0` (o `0/0`), DPTs
desconocidos, capacidades duplicadas, incompatibilidades semánticas y
escrituras sin readback. Las claves KNX Secure no se incluyen en la receta.

## Conexión operativa

La discovery bounded genera un candidato KNX/IP con `host` y `port` (por
defecto, `3671`). El despliegue inyecta el transport y resuelve cualquier
referencia segura en el borde de conexión. La recipe sólo describe el mapping
público.

## Qualification

Los tests software verifican DPT, conversiones, allowlists y readback con un
transport in-memory. La prueba externa debe verificar que las direcciones
están cableadas en el proyecto ETS correcto, que el bus responde y que los
valores observados representan realmente la capacidad declarada.

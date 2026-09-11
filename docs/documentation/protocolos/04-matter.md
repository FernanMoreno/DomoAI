# Matter declarativo

La receta Matter restringe un conjunto de nodos ya comisionados a endpoints,
clusters, atributos y comandos estándar concretos. No es un mecanismo de
commissioning ni una forma de descubrir cualquier cluster sin límites.

## Ejemplo mínimo

```yaml
transport_family: matter
auth_schemes:
  - commissioning_code
protocol_profile:
  transport: matter
  node_ids: [42]
  commissioning_required: true
  endpoints:
    - endpoint_id: 1
      device_type_ids: [256, 257]
      capabilities:
        - name: power
          cluster_id: 6
          attribute_id: 0
          command_names: ['On', 'Off', 'Toggle']
          readback: true
```

## Qué se declara

- allowlist opcional de `node_ids`;
- endpoint y device type;
- cluster y attribute estándar;
- comandos estándar permitidos;
- readback para cambios de estado;
- si el commissioning es un requisito externo.

## Límites

El profile rechaza clusters o atributos que no forman parte del catálogo
soportado, comandos duplicados o vendor commands que no estén allowlisted,
endpoints repetidos y escrituras sin readback. Las credenciales de fabric y
commissioning no se escriben en el YAML.

La receta usa el Matter Server configurado por el despliegue. El adapter
filtra los nodos y las interacciones antes de publicarlas como capacidades; no
expone al agente todo el fabric.

## Discovery y commissioning

La discovery declarativa acepta una URL `ws://` o `wss://` del Matter Server y
devuelve un candidato bounded. La URL no contiene secretos. El commissioning
del nodo y la compatibilidad del fabric siguen siendo una actividad atendida y
una gate externa.

## Qualification

Los tests software verifican allowlists, lectura de atributos y comandos
idempotentes con un transport in-memory. La qualification externa debe
comisionar el nodo real, verificar endpoint y device type, y probar atributos,
comandos y readback con la versión concreta del fabric y Matter Server.

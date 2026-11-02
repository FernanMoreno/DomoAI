# Cómo llega el conector al agente

El agente no consume directamente la receta ni el transport. Consume la
superficie normalizada que genera el adapter.

## Flujo de datos

```text
recipe.yaml
   ▼
ConnectorRecipe
   ▼
RecipeConnectionProvider
   ▼
protocol recipe adapter
   ▼
Device / Capability / StateSnapshot / Command
   ▼
DeviceRegistry + StateStore
   ▼
MCP
   ▼
agent
```

## Ejemplo conceptual

Una receta puede describir un registro Modbus, un group address KNX o un
attribute Matter. El agente recibe algo equivalente a:

```text
device: living-room-light
capability: power
state: on
commands: turn_on, turn_off
```

No recibe un comando genérico para escribir cualquier registro, publicar en
cualquier topic o invocar cualquier cluster.

## Por qué importa

- La política puede razonar sobre capacidades estables.
- La UI y MCP no necesitan una página por fabricante.
- Los comandos quedan limitados por el mapping validado.
- Un producto nuevo que usa un protocolo existente no necesita enseñar al
  agente el protocolo desde cero.
- Un mapping ambiguo no se convierte en una acción ejecutable.

## Lo que el agente no puede hacer

El agente no puede ampliar la allowlist de una receta desde una conversación,
leer secretos del secret store, saltarse readback ni convertir una operación
desconocida en un comando. Si una nueva capacidad es necesaria, se cambia la
receta o el provider, se valida y se vuelve a cualificar.

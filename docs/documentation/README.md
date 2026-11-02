# Documentación de conectores DomoAI

Esta carpeta explica cómo se conectan productos y protocolos externos con
DomoAI, desde una receta YAML hasta las capacidades que recibe el agente.

La regla principal es sencilla:

> Si el producto usa un protocolo que DomoAI ya entiende, se describe con una
> receta. Si usa un protocolo o una semántica que DomoAI no puede representar
> de forma segura, se implementa un proveedor personalizado.

## Inicio rápido

1. Decide si el producto encaja en MQTT, REST/OpenAPI, Modbus, KNX o Matter.
2. Copia el ejemplo correspondiente de `examples/connectors/`.
3. Declara las capacidades y los datos físicos del producto.
4. Valida la receta y genera el scaffolding:

   ```bash
   uv run domoai-admin connector create \
     --recipe ./recipe.yaml \
     --output ./my-connector
   uv run domoai-admin connector check ./my-connector
   ```

5. Configura el endpoint y las referencias a secretos en el despliegue.
6. Inyecta el transporte real, prueba el dispositivo y completa la
   qualification externa.

El scaffolding puede generarse sin escribir cinco métodos a mano, pero no
inventa un transporte ni declara listo un proveedor que todavía no ha sido
conectado a un dispositivo real.

## Índice

### Fundamentos

- [Modelo mental](fundamentos/01-modelo-mental.md)
- [Universalidad y límites](fundamentos/02-universalidad-y-limites.md)

### Crear conectores

- [Crear un conector paso a paso](conectores/01-crear-un-conector.md)
- [Recetas declarativas](conectores/02-recetas-declarativas.md)
- [Proveedor personalizado y sus cinco métodos](conectores/03-proveedor-personalizado.md)

### Protocolos

- [MQTT y REST/OpenAPI](protocolos/01-mqtt-rest.md)
- [Modbus](protocolos/02-modbus.md)
- [KNX](protocolos/03-knx.md)
- [Matter](protocolos/04-matter.md)

### Runtime y agente

- [Ciclo de vida de una conexión](runtime/01-ciclo-de-vida.md)
- [Cómo llega el conector al agente](runtime/02-exposicion-al-agente.md)

### Seguridad

- [Secretos y OAuth](seguridad/01-secretos-y-oauth.md)

### Operación

- [Discovery, probe, validate y diagnose](operacion/01-discovery-probe-validation-diagnose.md)
- [Qualification y troubleshooting](operacion/02-qualification-y-troubleshooting.md)

## Documentos de referencia existentes

Esta sección es el índice orientado a uso diario. Los documentos técnicos
existentes siguen siendo referencias válidas:

- [Connector recipes](../connector-recipes.md): contrato y ejemplos compactos.
- [Connection Provider SDK](../connection-provider-sdk.md): SDK y entry points.
- [Connection Control Plane v1](../DomoAI-Connection-Control-Plane-v1.md):
  diseño completo del plano de control.
- [Adapter coverage](../adapter-coverage.md): cobertura y qualification de
  adapters.

## Qué significa “conectado”

En esta documentación distinguimos tres estados:

| Estado | Significado |
| --- | --- |
| Declarado | La receta es válida y describe las capacidades públicas. |
| Software-qualified | El engine, el adapter, los contratos y los tests funcionan. |
| Externamente cualificado | El endpoint, el dispositivo, el commissioning o el proveedor externo han sido probados y evidenciados. |

Pasar el primer o segundo estado no permite afirmar automáticamente el tercero.

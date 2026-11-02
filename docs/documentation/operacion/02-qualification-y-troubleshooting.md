# Qualification y troubleshooting

## Tres capas de evidencia

| Capa | Qué demuestra | Qué no demuestra |
| --- | --- | --- |
| Contrato | El YAML, schema, profile y allowlists son válidos | Que exista el dispositivo |
| Software | Engine, adapter, runtime y MCP funcionan con fixtures o transports de prueba | Que el mapa físico sea correcto en una instalación real |
| Externa | Endpoint, hardware, commissioning, firmware y proveedor externo funcionan | Que otro modelo o versión sea compatible automáticamente |

`ConnectorQualificationService` genera un informe secret-free con
`software_status` y gates externas. El digest del informe permite detectar
cambios en la evidencia.

## Gates habituales

- `modbus_device`: comprobar registros, escalas, límites y readback.
- `knx_bus`: comprobar bus, group addresses, DPTs y readback.
- `matter_commissioning`: comisionar nodo y verificar endpoints declarados.
- `oauth_registration`: registrar la app y confirmar redirect URI externamente.
- `real_device`: usar cuando el protocolo no tiene una gate más específica.

Una gate pendiente no se convierte en `pass` porque los tests de software sean
verdes.

## Diagnóstico por síntoma

### `connector check` devuelve `incomplete`

El scaffold fue creado, pero todavía no se inyectó o implementó el transport
requerido. Revisa el provider generado y suministra la fábrica del despliegue.

### `recipe_transport_unsupported`

El registry no tiene un engine callable para `transport_family`. Comprueba que
la receta usa un family soportado y que el provider generado conserva la
configuración del engine.

### `recipe_discovery_unsupported`

La receta pide una estrategia no registrada. Usa `manual` o implementa un
discovery bounded que devuelva `ConnectionCandidate` del mismo connector.

### La receta se valida pero el dispositivo no responde

Comprueba endpoint, firewall, puerto, TLS o gateway, y después ejecuta la
prueba externa correspondiente. La validación del mapping no sustituye la
conectividad real.

### El estado llega pero el comando no se confirma

Comprueba el readback, la dirección o atributo de estado, el codec/DPT, la
semántica del dispositivo y el tiempo de propagación. No elimines el readback
para ocultar el problema.

### Matter expone menos capacidades de las esperadas

Revisa `node_ids`, endpoint, device type, cluster, attribute y command
allowlist. El filtrado es deliberado; los nodos o comandos no declarados no se
publican al agente.

### OAuth preflight devuelve `manual_required`

La configuración pública local es válida, pero falta registrar la aplicación
en el proveedor externo. Completa ese registro y guarda el secreto sólo en el
secret store.

## Checklist de cierre

- [ ] La receta no contiene secretos.
- [ ] El `connector_id` es estable y coincide en recipe, manifest y scaffold.
- [ ] Todas las capacidades físicas están declaradas y tienen semántica.
- [ ] Toda escritura tiene readback.
- [ ] El provider/engine está disponible.
- [ ] Los tests de contrato e integración pasan.
- [ ] Se ha probado el endpoint real.
- [ ] La gate externa está registrada con su evidencia y alcance.

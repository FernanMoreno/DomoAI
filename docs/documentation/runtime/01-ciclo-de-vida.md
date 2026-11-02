# Ciclo de vida de una conexión

Una receta describe el conector, pero el runtime conserva la autoridad sobre
su activación y su ciclo de vida.

## Secuencia

```text
1. Cargar manifest y recipe
2. Validar schema y profile
3. Resolver el provider/engine
4. Discover candidates
5. Seleccionar candidate
6. Probe
7. Validate ConnectionSetup
8. Build Adapter
9. Registrar en ConnectionManager
10. Activar en ManagedAdapterSet
11. Sincronizar DeviceRegistry y StateStore
12. Exponer por MCP
```

## Responsabilidades

### Recipe y engine

Validan el contrato y traducen el mapping. No persisten conexiones, no
autorizan comandos por su cuenta y no hacen activación implícita.

### ConnectionManager

Controla creación, replacement, reauth, reconnect y detach. También aplica el
flujo de configuración y las referencias a secretos.

### ManagedAdapterSet

Posee el conjunto de adapters activos, sus tareas de polling/eventos y su
apagado. El adapter de una receta no crea un ciclo de vida paralelo.

### Registry y StateStore

El registry mantiene la identidad y las capacidades descubiertas. StateStore
mantiene snapshots y freshness. Los cambios deben atravesar los mismos
contratos que los adapters nativos.

## Estados de error

- Error de recipe: falla antes de construir el transport.
- Engine ausente: el provider queda inválido o `incomplete`.
- Candidato incompatible: `probe` devuelve diagnóstico sin activación.
- Setup inválido: `validate` devuelve `valid=False`.
- Fallo de transporte: se propaga como health/diagnóstico del adapter.
- Readback fallido: la operación no se considera confirmada.

Los errores no se convierten en una conexión “verde” para que el agente pueda
ejecutar a ciegas.

## Idempotencia y replacement

La receta no cambia las garantías existentes: el manager y el adapter aplican
la identidad, el replacement y la idempotencia definidos por el runtime. Si se
reemplaza el mapping o el endpoint, debe seguirse el flujo de cambio de
conexión, no editar silenciosamente el adapter activo.

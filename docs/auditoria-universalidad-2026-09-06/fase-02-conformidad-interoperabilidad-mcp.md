# Fase 02 — Conformidad e interoperabilidad MCP

[Índice](README.md) · [Automatizaciones](fase-01-automatizaciones-autoridad-ejecucion.md) · [Privacidad](fase-03-privacidad-ciclo-datos.md)

Estado: U-05 y U-13 reproducidos; U-06 identificado por inspección. Entrada: fase 00. Salida consumida por instalación y qualification.

## 1. Objetivo

Que un cliente MCP pueda descubrir, interpretar y usar DomoAI bajo un perfil de transporte/autenticación documentado, sin conocimiento de formatos internos no publicados. La corrección de protocolo no concede por sí misma autoridad física: identidad, consentimiento y ejecución siguen separados.

La expresión «cualquier cliente» debe referirse a capacidades verificables del host: transporte, versión, gestión de certificados, bearer u OAuth y soporte de resultados estructurados. No basta con probar cuatro instancias de la misma biblioteca y llamarlas cuatro productos distintos.

## 2. Baseline y archivos

`src/domoai/mcp/unified_server.py` construye FastMCP y registra ambas familias. `gateway.py` administra la aplicación HTTP y sesiones. `stdio.py` usa el builder configurado. `auth.py` verifica tokens estáticos con hash; `token_lifecycle.py` gestiona su ciclo administrativo. `errors.py` crea envelopes propios. `domotics_server.py` y `ortools_server.py` definen tools y schemas derivados de firmas.

La baseline tiene un servidor público y controles de Host/Origin del SDK. El middleware que devuelve 405 para GET sin SSE precede a esos controles. También existen anotaciones y salidas estructuradas; no todas las entradas anidadas publican sus campos canónicos.

## 3. U-05: orden de validación en GET

Reproducción anterior: GET `/mcp` con Origin inválido devuelve 405 sin alcanzar la validación inferior. No expone datos ni ejecuta acciones en esa reproducción.

El cierre exige que la decisión sobre Origin ocurra antes de responder por ausencia de SSE. No basta con cambiar cualquier 405 por 403: GET legítimo sin SSE debe seguir siendo válido conforme al perfil soportado.

| Caso | Resultado contractual a verificar |
|---|---|
| GET, Origin válido, SSE desactivado | 405 sin crear stream ni tarea persistente. |
| GET, Origin inválido | Rechazo 403 antes de abrir sesión. |
| POST/DELETE, Origin inválido | Rechazo sin ejecutar handler funcional. |
| Origin ausente | Aplicar el perfil permitido, autenticación y protección de Host; no inventar una obligación general de enviar Origin. |
| Host fuera de allowlist | Rechazo por protección de transporte. |
| URL pública tras proxy | Host/Origin concuerdan con la topología declarada. |

Los códigos y requisitos de referencia proceden del [perfil de transporte usado en la auditoría](../auditoria-integral-universalidad-2026-09-06.md#13-referencias-normativas-y-límites-del-término-estándar). Antes de cambiar dependencias, comprobar la versión efectiva del SDK y el perfil normativo seleccionado.

## 4. U-13: errores y autodescripción

La reproducción con ClientSession devolvió `structuredContent.error.code=validation_error` e `isError=false` para `prepare_command({"command": {}})`. La causa localizada es que un diccionario de error ordinario se serializa como resultado exitoso.

Hay tres casos distintos que no deben mezclarse:

1. Mensaje JSON-RPC o llamada de protocolo inválida: error de protocolo correspondiente.
2. Ejecución de tool que falla: señal MCP de error y detalle sanitizado.
3. Tool de preview/validación que devuelve un diagnóstico negativo como dato: puede ser una operación correcta cuyo resultado dice que el plan no es válido.

El contrato debe describir cuál se aplica a cada tool. No convertir toda respuesta con la palabra `invalid` en un error de transporte ni ocultar errores funcionales detrás de HTTP 200 sin su señal MCP.

En `tools/list`, el objeto `command` observado es abierto y no publica campos como `id`, `device_id`, `command` e `idempotency_key`. La validación interna sigue protegiendo el runtime, pero el cliente debe adivinar o consultar documentación externa al schema. Mejorar esa autodescripción sin cambiar accidentalmente los formatos aceptados por clientes existentes.

## 5. U-06: perfil de autenticación

La implementación revisada usa bearer aprovisionado por el operador. Los metadatos del SDK no prueban un authorization server completo. La auditoría no realizó login OAuth desde hosts comerciales.

Decisión de producto pendiente: soportar explícitamente el perfil bearer en los hosts capaces de configurarlo, o incorporar un flujo OAuth compatible si se necesita cubrir hosts que lo exigen. No añadir endpoints de OAuth simulados que anuncien un flujo imposible.

| Elemento | Evidencia requerida |
|---|---|
| Descubrimiento de autenticación | Metadatos coherentes con el issuer y servidor efectivamente disponibles. |
| Token válido | Acceso sólo al hogar, roles y operaciones concedidos. |
| Token vencido/revocado | Denegación también en nuevas llamadas de una sesión existente. |
| Rotación | Nuevo secreto utilizable; anterior deja de valer según política explícita. |
| Roles y scopes | Comprobar la combinación, no sólo un scope de lectura/escritura. |
| Gesto humano | Un bearer válido no fabrica una aprobación. |
| Certificado local | Host de prueba confía explícitamente en la CA prevista. |

Si se elige OAuth, definir su arquitectura y validación en una especificación separada; no inferir de esta fase autorización para desplegar un proveedor externo.

## 6. Ciclo de vida y catálogo

- Inicialización y versión negociada deben ser trazables.
- El catálogo debe declarar operaciones opcionales según el runtime, sin ofrecer una escritura sin servicio que la respalde.
- Todos los tools públicos deben seguir siendo semánticos; no exponer servicios HA o topics MQTT.
- Cierre de sesión, reconexión y cancelación no pueden dejar streams, workers ni ownership colgados.
- Cancelar la espera del cliente no demuestra que no ocurrió una escritura; el resultado físico depende del executor.
- Cada resultado estructurado debe concordar con su output schema y con el texto visible al host.
- Las descripciones y datos de dispositivos son entradas no confiables para el agente; no constituyen instrucciones para saltarse policy.

## 7. Tareas propuestas

### F02-T01 — Contrato de transporte

- [ ] Reproducir U-05 a través del gateway y no sólo del middleware aislado.
- [ ] Cubrir la matriz GET/POST/DELETE, Host/Origin y SSE.
- [ ] Corregir el orden en la frontera responsable preservando respuestas legítimas.
- [ ] Verificar cierre de streams y sesiones bajo desconexión/reintento.

### F02-T02 — Contrato de resultados

- [ ] Inventariar errores capturados por cada familia de tools.
- [ ] Definir la separación entre diagnóstico de preview y fallo de ejecución.
- [ ] Comprobar `isError`, contenido estructurado y texto mediante ClientSession.
- [ ] Conservar códigos, causas útiles y redacción de secretos.

### F02-T03 — Esquemas y descubribilidad

- [ ] Comparar schemas publicados con modelos canónicos de comandos, planes y escenas.
- [ ] Publicar campos requeridos, unidades, enums y estructura cuando corresponda.
- [ ] Comprobar compatibilidad de entradas anteriores.
- [ ] Probar que un consumidor nuevo construye una petición válida a partir del catálogo.

### F02-T04 — Perfil por host

- [ ] Registrar transporte/auth/versiones soportados por cada host elegido.
- [ ] Ejecutar discovery, lectura, preview y un rechazo de escritura con fixtures.
- [ ] Probar expiración y rotación de token.
- [ ] Mantener como no verificado todo host no ejecutado realmente.

## 8. Pruebas existentes para ampliar

```bash
uv run --frozen pytest tests/unit/mcp/test_gateway.py tests/unit/mcp/test_gateway_auth.py
uv run --frozen pytest tests/contract/test_unified_mcp_contract.py tests/integration/test_mcp_gateway_http.py tests/integration/test_mcp_transport_parity.py
```

Estos comandos no fueron repetidos para redactar esta fase. Las nuevas regresiones deben usar ClientSession/HTTP para detectar diferencias que una llamada directa a `server.call_tool` pueda ocultar.

## 9. Cierre

- [ ] U-05 y U-13 resueltos con prueba de protocolo.
- [ ] U-06 resuelto como perfil limitado documentado o flujo adicional probado, sin promesa universal no comprobada.
- [ ] Cero rutas públicas de ejecución alternativas.
- [ ] Errores útiles sin secretos y schemas coherentes.
- [ ] Matriz real de hosts identificados, no nombres comerciales asignados a instancias del mismo SDK.

Salida: contrato público estable y evidencia de interoperabilidad para la fase 07. Riesgo residual: cambios futuros de SDK o host requieren volver a cualificar esa combinación.

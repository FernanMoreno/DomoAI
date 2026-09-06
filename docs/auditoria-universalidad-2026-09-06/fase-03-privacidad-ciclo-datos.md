# Fase 03 — Privacidad y ciclo de vida del dato

[Índice](README.md) · [MCP](fase-02-conformidad-interoperabilidad-mcp.md) · [Cobertura](fase-04-cobertura-semantica-conectores.md)

Estado: U-04 reproducido; U-10 requiere verificación adicional. Entrada: baseline; coordinar autoridad con fase 01 y schemas públicos con fase 02.

## 1. Objetivo

Garantizar que exportar, borrar y retener datos tenga una semántica operativa precisa en un runtime vivo. Una respuesta exitosa debe describir lo que efectivamente ocurrió, incluidas categorías preservadas y límites.

No se promete aquí cumplimiento legal ni borrado de toda huella física. Los backups, auditoría de seguridad, caches y operaciones en curso requieren tratamiento explícito.

## 2. Componentes y propietarios

| Componente | Función actual | Frontera a revisar |
|---|---|---|
| `application/privacy.py` | Autoriza categorías, exporta y borra | Operación completa, resumen y excepciones parciales. |
| `domain/privacy.py` | Modelos y límites | Límite 4.096 y contrato de paginación/artefacto futuro. |
| `persistence/privacy.py` | Lectura y DELETE por hogar | Transacciones y selección por autoridad del payload. |
| `persistence/repositories.py` | Histórico y snapshots | Retención al insertar, metadatos y ownership del dato. |
| `runtime/state_store.py` | Caché y estado corriente | Qué permanece visible tras borrar filas. |
| `mcp/configured.py` | Política de categorías | Categorías exportables, borrables e inmutables. |
| `mcp/domotics_server.py` | Tools de privacidad | Scopes, errores, tamaño de resultado y respuesta. |
| `persistence/backup.py` | Copias y restauración | Datos históricos conservados y posible reintroducción. |

Las rutas son relativas a `src/domoai/`. No se ejecutaron borrados sobre bases operativas en la auditoría.

## 3. U-04: export que no termina en una vivienda con histórico

El servicio reúne todas las filas mediante lecturas completas y construye un `PrivacyExport` limitado a 4.096 registros. Con 4.097 registros se reprodujo `ValidationError: too_long` en `records`. El tool no recibe cursor.

Subir simplemente el máximo mueve el fallo a una vivienda mayor y mantiene la acumulación en memoria. El requisito de cierre es completar la exportación con recursos acotados y explicar si representa un snapshot consistente o una colección que puede cambiar durante su lectura.

### Contrato por decidir

Dos diseños posibles: páginas con cursor estable, o generación de un artefacto acotado/streaming con manifest. Elegir uno por los consumidores MCP reales y la escala prevista; no implementar ambos por anticipación.

En ambos casos se necesita: identidad de export, hogar, categorías, versión de schema, orden estable, límites de página/bloque, caducidad, errores de continuación y semántica ante escrituras concurrentes.

## 4. U-10: borrado y retención

La auditoría constató que la retención se aplica al histórico al insertar observaciones. No demostró una purga periódica universal de categorías. El store de privacidad hace commit por categoría y no invalida en ese método el StateStore ni administra backups.

Son límites y preguntas pendientes, no una prueba ejecutada de fuga o corrupción. Deben verificarse a través de la API y del runtime activo.

### Qué significa borrar estado

Definir si se borra sólo histórico, snapshot durable, caché o todos ellos. Si el dispositivo sigue conectado puede producir una nueva observación legítima inmediatamente. El contrato debe distinguir «dato eliminado hasta un corte temporal» de «no volver a recolectar datos de este equipo».

No borrar la evidencia necesaria para saber si una acción física incierta ocurrió. Tampoco prometer que eliminar un plan de una tabla cancela un coroutine que ya está ejecutándolo.

### Qué significa retener

Precisar unidad temporal, timestamp usado, categorías incluidas y momento de purga. Probar el hogar sin nuevos eventos: una política sólo activada al insertar puede conservar filas vencidas indefinidamente en reposo.

Separar retención de datos operativos, de auditoría y de backups. Una configuración única de días no acredita automáticamente las tres.

## 5. Invariantes

1. Ninguna lectura/export/borrado cruza tenant u hogar autorizado.
2. Los registros legacy sin household se tratan según una política explícita, nunca como pertenecientes a cualquier hogar nominal.
3. El resultado de export se puede completar aunque exceda 4.096 filas.
4. Una interrupción no se comunica como export completo.
5. Un borrado parcial no se comunica como eliminación total.
6. Las lecturas tras borrado respetan la semántica declarada de caché y nuevas observaciones.
7. Los grants, planes en vuelo e intenciones desconocidas mantienen la evidencia de seguridad necesaria.
8. La redacción de secretos se aplica a todos los formatos de salida, incluidos artefactos.
9. Restaurar un backup informa que puede reintroducir datos anteriores al borrado.

## 6. Matriz de pruebas propuesta

| Escenario | Resultado esperado |
|---|---|
| Export 0, 1, 4.096 y 4.097 registros | Resultado válido/completable en todos los tamaños. |
| Export con varias categorías | Identidad y tipo de cada registro interpretables. |
| Escrituras mientras se pagina | Comportamiento conforme al snapshot/cursor elegido; sin pérdidas silenciosas. |
| Cursor manipulado o de otro hogar | Rechazo antes de devolver datos. |
| Fallo de almacenamiento a mitad | Error y progreso parcial explícitos; continuación definida. |
| Borrado de hogar A con B presente | B permanece intacto. |
| Borrado y lectura MCP inmediata | Caché/SQL concuerdan con la semántica publicada. |
| Borrado y reinicio | No reaparece dato eliminado salvo fuente o restore explícitos. |
| Borrar plan en ejecución | Resultado y autoridad de cancelación definidos; sin duplicar ni ocultar efectos. |
| Retención sin eventos nuevos | Purga o límite documentado observable. |
| Restore posterior | Evidencia explícita del corte y datos restaurados. |

## 7. Paquetes de trabajo propuestos

### F03-T01 — Export acotado

- [ ] Reproducir U-04 con SQLite temporal y ClientSession.
- [ ] Elegir paginación o artefacto y especificar consistencia/caducidad.
- [ ] Evitar `fetchall` de todo el histórico para una respuesta acotada.
- [ ] Probar interrupción, duplicación de página y hogar ajeno.

### F03-T02 — Borrado coherente

- [ ] Inventariar tablas, caches y consumidores de cada categoría.
- [ ] Definir transacción o progreso parcial por categoría.
- [ ] Resolver interacción con tareas en curso y nuevas observaciones.
- [ ] Revalidar lectura, reinicio y auditoría después de borrar.

### F03-T03 — Retención y recuperación

- [ ] Documentar timestamp y periodicidad por categoría.
- [ ] Probar caducidad con reloj controlado y hogar inactivo.
- [ ] Definir impacto del restore y de copias antiguas.
- [ ] Mostrar al operador qué categorías se preservan y por qué.

## 8. Pruebas de partida

```bash
uv run --frozen pytest tests/unit/application/test_privacy_service.py tests/contract/test_phase4_privacy_mcp_contract.py tests/integration/test_phase4_privacy_persistence.py
uv run --frozen pytest tests/unit/runtime/test_state_store.py tests/integration/test_backup_restore_lifecycle.py
```

No lanzar un tool de borrado contra el gateway doméstico para probar esta fase. Usar bases temporales, estado sintético y operaciones controladas; un ensayo sobre datos reales requeriría alcance concreto.

## 9. Criterios de cierre

- [ ] Export superior a 4.096 registros completado y verificable.
- [ ] Uso de memoria/tiempo acotado bajo el tamaño elegido para aceptación.
- [ ] Aislamiento entre hogares y tratamiento legacy probado.
- [ ] Borrado, cache, retención y restore descritos sin contradicciones.
- [ ] Se preserva evidencia de seguridad sin presentarla como dato borrado.
- [ ] Contratos MCP y schema actualizados cuando corresponda.

Salida: ciclo del dato verificable. Riesgo residual: obligaciones legales y custodia de copias dependen del despliegue; no se resuelven únicamente pasando tests.

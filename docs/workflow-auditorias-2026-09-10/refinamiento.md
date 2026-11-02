# Continuación: precisiones del piloto

Se refinan tres paquetes existentes sin crear nuevas features, código ni tests. Spec → plan/contratos → tasks → Graphify → fuente → decisión TDD registrados en secuencia.jsonl. Posterior refinamiento de compatibilidad documentado, sin implementación.

- **P-07**: perfil inicial de ensayo, cooldown, cuota por hogar, umbral neto, precedencia manual, digest y presupuesto del solver. Valores propuestos, actuación no activada.
- **P-10**: objetivos Codex CLI/Claude Code/genérico, registro de versión exacta, combinaciones separadas por transporte/autenticación, escenarios mínimos/completos y estados de cualificación. Filas pendientes; sin inventar versiones ni soporte externo.
- **P-12**: retención por clases propuesta, protección de ledger/inmutables, copias cifradas, recuperación aislada, revocaciones posteriores al backup y medición RPO/RTO con límites explícitos. No se alteran datos ni configuración reales.

Fuente: presupuesto en application/optimization_worker.py y process_optimization_worker.py; backup en persistence/backup.py; privacidad usa actualmente plazo único. Las semillas iniciales optimizer/worker.py y admin/backup.py no existen: búsqueda y consulta específica Graphify corrigieron ubicación antes de cerrar conclusiones. Grafo guía búsqueda, no sustituye fuente.

135 requisitos trazados, 336 tareas pendientes, 24 paquetes. Tres precisiones de diseño concretadas; calibración, cualificación de clientes y ensayo del piloto todavía pendientes. No se ejecutó el gate postimplementación de composición porque no se implementó ninguna feature.

Pruebas existentes de worker/privacidad/restore se ejecutan como baseline local de reutilización. Resultado en baseline-refinamiento.txt; no prueba el perfil nuevo ni acredita clientes reales. Prerrequisitos Spec Kit de los tres paquetes refinados: 3/3 exit 0. Verificación estructural y preservación en verificacion.json.

Resultado observado del baseline: **16 passed in 13.64s**, exit 0. Verificación documental posterior: 24 paquetes, 135 requisitos, 336 tareas pendientes, ningún enlace/artefacto/DAG inválido y 975 archivos protegidos sin cambios. Estos resultados no validan las nuevas funciones.

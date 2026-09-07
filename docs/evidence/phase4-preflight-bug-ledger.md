# Registro de errores — preflight Fase 4

Fecha: 2026-09-06. El registro cubre el nuevo preflight y sus pruebas; los
errores históricos del laboratorio multi-host están en
[`multihost-lab-v2-bug-ledger.md`](multihost-lab-v2-bug-ledger.md).

| ID | Síntoma | Causa | Corrección | Verificación |
| --- | --- | --- | --- | --- |
| PF-001 | La primera ejecución del contrato falló durante collection con `ModuleNotFoundError` para `domoai.domain.phase4_preflight`. | Se ejecutó deliberadamente el test RED antes de crear el modelo. | Añadidos `Phase4Gate` y `Phase4PreflightReport` estrictos. | Contrato: 5 passed. |
| PF-002 | La primera versión del validador trataba `process_lab` como un gate `software` genérico y rechazaba el reporte válido. | La validación comparaba `kind` contra una sola clase en vez de mapear cada `gate_id`. | Mapa explícito `digital_twin → software`, `process_lab → process_lab`. | Unit/contract: 10 passed. |
| PF-003 | El test de no-hardware fallaba aunque el scope era correcto. | La aserción buscaba la palabra `hardware` en todo el JSON y también coincidía con `hardware_not_available`. | La aserción comprueba el campo prohibido `evidence_scope=hardware`, no el texto diagnóstico. | Contract: 5 passed. |
| PF-004 | Ruff detectó import ordering y una línea >100 caracteres. | Formato inicial del nuevo CLI/test. | Ruff fix + ajuste manual de la firma larga. | Ruff: All checks passed. |
| PF-005 | El nuevo contrato de orden falló inicialmente al buscar un comando en una sola línea. | El script usa una continuación Bash válida en varias líneas. | La prueba inspecciona el tramo de comando delimitado por `>/dev/null`, sin imponer formato. | Contratos multi-host: 5 passed. |
| PF-006 | La primera aserción de orden encontró la declaración de `wait_for_database_writer` en vez de su llamada. | La búsqueda textual no distinguía definición y uso. | La prueba ancla la búsqueda a la llamada posterior al arranque base. | Contratos multi-host: 5 passed. |
| PF-007 | Una orden auxiliar de verificación no pudo lanzar `pytest` por una ruta de workspace duplicada. | Error de invocación del agente (`OneDrive/OneDrive`), no del proyecto. | Reejecución con la ruta correcta. | Contratos multi-host: 5 passed. |

La regresión de composición encontró además `LAB-013`, registrado y corregido
en el [ledger multi-host](multihost-lab-v2-bug-ledger.md): el runner separa
ahora el arranque de infraestructura y hosts, y el Docker lab posterior pasó
8/8 escenarios y 20/20 carreras.

## Resultado

- Preflight real: gemelo digital **passed**, proceso **80 passed/1 skipped**.
- Gates físicas: las tres **blocked_external_dependency**.
- Evidencia: `phase4-preflight-latest.json` y `phase4-preflight-latest.md`.
- No se generó evidencia `hardware`, ni se invocaron adapters, leases o
  approvals.
- Regresión final: 1906 passed, 18 skipped; composición 530 passed, 18
  skipped; Ruff/mypy/docs/shell/diff limpios.
- Graphify estructural actualizado: 8279 nodos y 24629 enlaces; la extracción
  semántica documental quedó limitada por ausencia de API key y el parser SQL
  opcional no instalado, sin afectar al código ni a los gates.

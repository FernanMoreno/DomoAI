# Evidencia de verificación

Fecha: 2026-09-10. Estado de trabajo auditado: el commit indicado en el informe más cambios locales. No se corrigieron defectos de producción.

## Reproducciones

Desde la raíz del repositorio, con el entorno del proyecto:

```bash
uv run python docs/auditoria-integral-2026-09-10/reproducciones/ejecucion.py
uv run python docs/auditoria-integral-2026-09-10/reproducciones/cancelacion.py
uv run python docs/auditoria-integral-2026-09-10/reproducciones/readiness.py
uv run python docs/auditoria-integral-2026-09-10/reproducciones/superficie.py
uv run python docs/auditoria-integral-2026-09-10/reproducciones/energia.py
```

Los cinco archivos fueron ejecutados sobre el código actual. Resultados: [salida conservada](reproducciones-resultados.txt). Los scripts usan datos inventados, memoria, SQLite temporal y simulación; no requieren credenciales de la vivienda. El código de salida 0 significa que la demostración terminó; en estos casos demuestra anomalías, no aprobación del sistema.

| Evidencia | Hallazgos |
|---|---|
| `ejecucion.py` | A-01, A-03; incluye también una demostración básica de la cola cancelada |
| `cancelacion.py` | A-02 con executor y adaptador simulado reales |
| `readiness.py` | A-04 con runtime completo sobre fixture |
| `superficie.py` | A-05 a A-08, ASGI/proyecciones/normalizador reales y dependencias simuladas |
| `energia.py` | A-09 a A-12 con solver CP-SAT real |

## Controles generales

| Comando | Resultado |
|---|---|
| `uv run ruff check .` | PASS inicial y final, incluidos los archivos de reproducción |
| `uv run mypy src` | PASS: 245 archivos de código fuente |
| `uv lock --check` | PASS: 75 paquetes resueltos |
| `uv run python scripts/check_architecture_contracts.py` | PASS |
| Import Linter, invocado por el gate de composición | PASS: 4 contratos, 245 archivos, 926 dependencias |
| `uv run python scripts/check_runtime_contract_docs.py` | PASS |
| `uv run --with pip-audit pip-audit --local` | PASS: no se encontraron vulnerabilidades conocidas |
| `uv build --out-dir /tmp/domoai-audit-build-20260910` | PASS: sdist y wheel construidos; wheel incluye migraciones, esquema PostgreSQL y 9 skills |
| `uv run pytest -q -m 'not composition'` | PASS: 2.295 passed, 17 skipped, 118 deselected; 690,50 s |
| `project-composition-check "$(cat .ai/project-name)"` | PASS: arquitectura y 660 passed, 18 skipped; pytest 669,83 s |
| `scripts/ci/run_mandatory_docker_composition.sh` | PASS: 2 passed, 0 skipped; YAML y Caddy válidos |

El filtro `-m 'not composition'` excluye el marcador, no el directorio. Existen pruebas de integración que arrancan laboratorios Docker; no es una suite exclusivamente unitaria. El gate de composición vuelve a recorrer integración, por lo que hay solapamiento: **no sumar ambas cifras como pruebas distintas**. Se ejecutaron simultáneamente; los tiempos no constituyen un benchmark aislado de rendimiento.

Las 17/18 omisiones no se consideran aprobadas. La salida conservada del smoke obligatorio está representada además por [JUnit](junit-docker-composition.xml). Caddy emitió avisos de formato y de caducidad del certificado temporal de un día creado por el propio script; terminó con `Valid configuration` y código 0. Eso no valida los certificados del despliegue doméstico.

## Dependencias reales y límites

Docker estaba disponible, versión 29.7.2. Se ejecutaron dependencias desechables mediante las suites y un contenedor Mosquitto del digest declarado en despliegue. La hipótesis de que ese Mosquitto solo escuchara en loopback quedó descartada: publicación por loopback y por interfaz del contenedor funcionaron. No se incluye como defecto.

La validación de CI local no prueba configuración del proveedor Git remoto, protección efectiva de ramas ni que una release concreta haya superado GitHub Actions. `pip-audit` detecta vulnerabilidades conocidas en su base de datos; no garantiza ausencia de vulnerabilidades nuevas.

No se efectuó cualificación de radio, cableado, firmware o dispositivos domésticos; tampoco piloto energético con facturas y mediciones reales. No se verificó cada combinación de cliente MCP, transporte, proveedor y sistema operativo. Los casos nuevos de carrera y transición se reprodujeron principalmente en SQLite/simulación; su equivalente PostgreSQL y multi-host requiere pruebas dedicadas, aunque existan suites generales para esos backends.

## Composition Review Report

- **Subsistemas modificados:** ninguno de producción; solo documentación y reproducciones.
- **Subsistemas revisados:** Management/MCP, autoridad, recursos, normalización HA, automatización, cola, executor, ledger, recuperación, lifecycle/readiness, proveedores energéticos y solver; despliegue y CI adyacentes.
- **Contratos/invariantes:** alcance, tiempo de disparo, cancelación previa al envío, cierre durable, disponibilidad, aislamiento de datos inválidos, conservación de energía y coherencia monetaria.
- **Arquitectura:** gate de fuente e Import Linter aprobados.
- **Escenarios cruzados:** descritos en los doce hallazgos y las cinco reproducciones.
- **Contratos e integración:** suites y cifras finales en la tabla anterior; no inferir aprobación física de pruebas con doubles.
- **Fallos y causas:** A-01 a A-12 en el informe principal; código de producción intacto.
- **Riesgos residuales:** hardware, matrices de interoperabilidad, recuperación de estos casos específicos en otros backends y medición de ahorro real.
- **Veredicto:** **FAIL** para aceptación integral. Una suite verde no invalida reproducciones adicionales de defectos.

## Revisión final y conocimiento durable

La revisión de cambios se limita a `docs/auditoria-integral-2026-09-10/`; no se hizo commit, push ni cambio de despliegue. Se conserva este informe en el repositorio como evidencia compartida. No se copian hechos generados de Graphify ni una instantánea de defectos aún no resueltos al vault: el estado y las decisiones posteriores deben actualizarse donde se implementen y verifiquen.

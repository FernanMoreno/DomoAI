# Evidencia y límites de la auditoría de producto

Fecha: 2026-09-10. Base: `b9f5094fc1a28b0f209ca7f3d4f332f8d8a789f3` más cambios locales preexistentes. La auditoría añade únicamente documentos bajo esta carpeta.

## Método

Se contrastó la visión del usuario con código de producto, interfaz de Management, contratos MCP, skills, pruebas, documentación de instalación y auditorías anteriores. Se aplicó brainstorming como evaluación de enfoques, sin iniciar implementación. Graphify orientó las relaciones entre `ProductSummary`, `CommissioningService`, `SceneCommitRequest` y `ConnectionManager`; esas relaciones se contrastaron con las fuentes.

Se distinguieron tres niveles: capacidad implementada, recorrido de producto parcial y resultado externo no demostrado. «No localizado» expresa una limitación de la revisión de las superficies indicadas, no una prueba matemática de ausencia en todo el repositorio.

No se creó una nueva especificación de implementación: esta entrega es un diagnóstico y un backlog propuesto. Cada cambio de comportamiento posterior deberá seguir el flujo Spec Kit y sus gates pertinentes.

## Referencias internas

Las rutas siguientes son relativas a la raíz del repositorio; las líneas corresponden al estado auditado.

| Evidencia | Qué sustenta | Qué no demuestra |
|---|---|---|
| `src/domoai/management/ui.py:8` | Interfaz de instalación centrada en conexiones | Dashboard cotidiano completo |
| `src/domoai/management/ui.py:29` | Campos técnicos de OAuth expuestos | Que el usuario no técnico pueda completarlos sin ayuda |
| `src/domoai/management/ui.py:47` y `:66` | Estados como etiquetas y feedback genérico success/failure | Recorridos completos de recuperación, retirada o explicación |
| `tests/integration/test_management_ui.py:11` | Test de HTML y presencia de textos | Navegación real en navegador, accesibilidad o comprensión humana |
| `src/domoai/mcp/domotics_server.py:508` y `:541` | Estado e histórico por herramientas | Análisis económico posterior listo para usuario |
| `src/domoai/mcp/domotics_server.py:751` | Aprobación ligada a plan y evidencia humana confiable | Pantalla universal de consentimiento en cualquier host |
| `src/domoai/application/scene.py:14` | Escena tipada, ordenada y ligada a digests | Biblioteca de rutinas familiares con editor y preferencias |
| `src/domoai/domain/automation.py:29` y `:87` | Reglas de estado/hora, condiciones, cooldown y plantilla | Intención doméstica general que se adapta a nuevas circunstancias |
| `src/domoai/domain/product.py:17` y `src/domoai/optimizer/product.py:19` | Resumen tipado de propuestas y próximo paso | Ahorro observado o interfaz doméstica completa |
| `src/domoai/skills/catalog.py:10` | Catálogo de nueve procedimientos | Calidad real de actuación de todos los agentes |
| `src/domoai/skills/workflow.py:108` y `:216` | Flujo que recibe un escenario estructurado | Interpretación universal de peticiones naturales |
| `src/domoai/runtime/control_takeover.py:70` y `:732` | Coordinación y supervisión de batería/EV | Gestión familiar general de intervención manual en cualquier dispositivo |
| `src/domoai/application/commissioning.py:47` | Evaluación de activos sin conceder autoridad | Cualificación física automática de todos los aparatos |
| `src/domoai/domain/federation.py:23` | Intenciones firmadas entre hogares | Modelo de objetivos/preferencias del habitante |
| `docs/unified-mcp.md:32` | Transporte, bearer estático, consentimiento y cobertura declarados | Funcionamiento con todo cliente remoto o soporte OAuth entrante nativo |
| `README.md:5`, `:133` y `:222` | Recorridos de entorno, energía y proveedor HA | Instalación observada sin conocimientos técnicos |

Las brechas de consumo base, comparación y métricas energéticas se documentan con reproducciones en la [auditoría técnica anterior](../auditoria-integral-2026-09-10/README.md). Esta evaluación no repitió esas reproducciones ni declaró corregidos los defectos.

## Verificación ejecutada en este turno

```bash
uv run pytest -q \
  tests/contract/test_skill_catalog.py \
  tests/contract/test_skill_contract.py \
  tests/contract/test_unified_mcp_contract.py \
  tests/integration/test_energy_skill_workflow.py \
  tests/integration/test_management_ui.py \
  tests/integration/test_unified_mcp_compatibility.py \
  tests/integration/test_phase2_scene_execution.py \
  tests/unit/application/test_product_summary.py
```

**Resultado: 65 passed in 24.90s; exit 0.** Prueban contratos de catálogo, flujo de energía, escenas, proyecciones y protocolo. No constituyen un estudio de UX ni la ejecución de todos los hosts comerciales. En particular, parte de compatibilidad usa `ClientSession` sobre streams en memoria.

No se modificó código de producción; no se repitieron las suites completas, los laboratorios Docker ni las cualificaciones físicas de la auditoría técnica. Se verificaron los documentos añadidos, sus enlaces locales y su diff. Los resultados de aquella auditoría no se presentan como una nueva ejecución de pruebas en este turno.

Una revisión independiente contrastó las afirmaciones sobre UI, proyecciones y escenarios estructurados. Identificó una ambigüedad circular entre instalación y catálogo; se corrigió distinguiendo cobertura/conexión mínimas de su integración posterior en el recorrido de instalación.

## Referencias externas consultadas

Se consultaron fuentes oficiales el 2026-09-10 para evitar recomendar como diferenciación algo ya documentado por otros proyectos. No se instalaron ni compararon experimentalmente esos productos:

- [Home Assistant MCP](https://www.home-assistant.io/integrations/mcp_server): interfaz de agentes, entidades expuestas, contexto y opciones de conexión.
- [Home Assistant Energy](https://www.home-assistant.io/home-energy-management/): gestión energética doméstica.
- [EMHASS: conceptos principales](https://emhass.readthedocs.io/en/latest/main_core_concepts.html): objetivos, cargas y planificación anticipada/adaptativa.
- [Predbat: funciones](https://springfall2008.github.io/batpred/what-does-predbat-do/): planificación de batería a partir de previsiones y consumo histórico.

La combinación propuesta como diferenciación de DomoAI es una inferencia estratégica de esta auditoría. No se afirma que otros productos carezcan de toda esa combinación ni que DomoAI sea el más completo o el más seguro.

## Incertidumbres que requieren validación externa

- Segmento prioritario, facilidad de instalación y uso familiar: hipótesis, pendientes de entrevistas y tareas observadas.
- Ahorro neto: requiere mediciones, contrato y baseline defendible; no se ha medido en una vivienda durante este trabajo.
- Demanda de nuevos dispositivos: no existe aquí un ranking basado en usuarios; el orden propuesto debe corregirse con el piloto.
- Compatibilidad de agentes: pruebas de SDK no sustituyen versiones concretas de aplicaciones, autenticación y aprobación humana.
- Accesibilidad, móvil e idioma: revisión de HTML/fuente, sin auditoría visual interactiva ni pruebas con usuarios.
- Viabilidad comercial y disposición a pagar: no evaluadas; no se proponen ingresos, precios ni retorno garantizado.

## Resultado

Auditoría de producto y backlog documental entregados. **Preparación de producto para uso general no demostrada; base suficiente para diseñar un piloto acotado, una vez cerrados los bloqueos técnicos pertinentes.** Los documentos distinguen lo existente, lo propuesto y lo pendiente de medir.

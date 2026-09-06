# Fase 07 — Skills, empaquetado e instalación por host

[Índice](README.md) · [Energía](fase-06-energia-optimizacion-explicable.md) · [Qualification](fase-08-resiliencia-calidad-qualification.md)

Estado: U-12 confirmado por inspección de wheel y carga por zipimport. Entrada: contrato MCP de fase 02 y perfiles de vivienda de fase 05.

## 1. Objetivo

Permitir instalar y usar los artefactos publicados fuera del repositorio de desarrollo. El host debe conectarse al mismo runtime y las Skills deben encontrar sus recursos sin depender de la estructura del checkout.

El objetivo no es incorporar nombres de proveedores al backend ni crear configuraciones que arranquen un runtime físico independiente por cada agente.

## 2. Catálogo existente

Nueve Skills en `skills/core`: optimización general de energía, EV, confort térmico, autoconsumo solar, arbitraje de batería, modo noche, vacaciones, diagnóstico y commissioning. El catálogo determinista se encuentra en `src/domoai/skills/catalog.py`; validator y workflow viven en el mismo paquete. Hay wrappers para Claude, Codex y generic MCP.

Las Skills describen procedimientos. El runtime debe hacer cumplir policy y permisos aunque una Skill esté ausente o el cliente intente saltársela.

## 3. U-12: diferencia entre checkout y paquete

El build anterior produjo sdist y wheel válidos. El wheel tenía 208 entradas, 16 migraciones SQL y cero `SKILL.md`. `load_core_catalog()` busca `skills/core` remontando desde `__file__`, por lo que presupone una ubicación de repositorio.

La importación desde el wheel en modo aislado seguida de `load_core_catalog()` falló con `SkillContractError`. La prueba usó zipimport; no fue una instalación pip limpia. La ausencia de assets en el wheel sí es evidencia directa.

Esto no prueba que falle todo el MCP. Afecta al contrato de distribución/carga de Skills y revela que «build pasó» es un criterio insuficiente de instalación funcional.

## 4. Decisión de distribución

| Alternativa | Qué exige | Qué debe probarse |
|---|---|---|
| Assets dentro del paquete | Declaración de inclusión y carga con mecanismo de recursos adecuado | Catálogo disponible tras instalar wheel sin checkout. |
| Artefacto de Skills separado | Versionado, instalación y ubicación explícitos | Compatibilidad entre runtime y catálogo; error útil si falta instalación. |

Elegir y documentar una. No duplicar silenciosamente assets en ubicaciones divergentes ni descargar Skills de Internet al primer import sin una política de distribución explícita.

Las migraciones SQL ya aparecen en el wheel; una solución para Skills debe verificar que no rompe otros recursos empaquetados.

## 5. Perfiles de instalación

### Gateway compartido

Cada cliente conecta a una URL con su identidad y permisos. Registry, scheduler, state store y executor pertenecen al runtime compartido. El operador prepara certificados, configuración de conectores y bindings.

### Stdio

Documentar cuándo se inicia un proceso local y qué ownership adquiere. Que el protocolo sea portable no significa que todos los hosts deban arrancar una nueva autoridad sobre los mismos dispositivos.

### Laboratorio

Fixture y bootstrap de laboratorio deben estar claramente elegidos. Un despliegue sin proveedor concreto no debe aparentar controlar una casa real por caer silenciosamente en simulación.

## 6. Contrato de una Skill portable

- Descubre catálogo/cobertura y lee estado antes de proponer cuando el workflow lo requiera.
- Usa nombres de operaciones semánticas, no servicios HA ni topics de fabricante.
- Maneja falta de capability, estados stale y datos energéticos incompletos.
- Distingue preview, preparación, aprobación, ejecución y programación.
- No produce consentimientos ni credenciales inventadas.
- Interpreta rechazo, parcial y desconocido conforme a fases 01 y 02.
- No afirma ahorro ni acción física basándose sólo en propuesta del solver.
- Termina con una explicación útil cuando el perfil instalado no cubre el objetivo.

La portabilidad no elimina diferencias de soporte del host para prompts/resources/auth. Registrar esas diferencias en la matriz de instalación.

## 7. Matriz de aceptación por host

Por cada host realmente elegido, registrar producto, versión, transporte, método de autenticación, CA, configuración usada y fecha. Ejecutar:

1. Inicialización y listado de tools/resources/prompts disponibles.
2. Discovery de fixture con capability soportada y otra no soportada.
3. Lectura de estado fresco y diagnóstico de estado stale.
4. Preview sin persistencia física ni ejecución.
5. Preparación con error y tratamiento correcto de `isError`.
6. Solicitud de acción sensible sin gesto humano: no ejecuta.
7. Workflow aprobado de fixture con resultado confirmado.
8. Rechazo/partial/unknown correctamente explicado por la Skill.
9. Revocación/rotación de token y reconexión.

Usar nombres comerciales sólo si se ejecutó ese producto, no si una prueba genérica tiene un `client_id` con ese nombre.

## 8. Tareas propuestas

### F07-T01 — Instalación limpia

- [ ] Construir paquete desde el estado que se quiere validar.
- [ ] Instalar en entorno temporal fuera del checkout.
- [ ] Importar entrypoints y comprobar recursos SQL y Skills según diseño elegido.
- [ ] Ejecutar `load_core_catalog()` o instalación explícita de catálogo sin dependencia oculta del cwd.

### F07-T02 — Procedimientos y wrappers

- [ ] Validar las nueve Skills y sus operaciones contra catálogo efectivo.
- [ ] Corregir referencias a recursos/opciones no disponibles en cada perfil.
- [ ] Mantener una fuente de procedimiento core y wrappers sin divergencia de seguridad.
- [ ] Probar fallos y estados parciales además del camino feliz.

### F07-T03 — Kits de conexión

- [ ] Publicar configuración por perfil/host probado con placeholders inequívocos para secretos.
- [ ] Distinguir gateway compartido, stdio y laboratorio.
- [ ] Incluir preflight y diagnóstico de certificado/auth/proveedor ausente.
- [ ] Verificar que la configuración no expone la autoridad física en endpoints alternativos.

## 9. Pruebas de partida

```bash
uv run --frozen pytest tests/contract/test_skill_catalog.py tests/contract/test_skill_contract.py tests/integration/test_core_skill.py
uv run --frozen pytest tests/contract/test_gateway_deployment_assets.py tests/integration/test_gateway_deployment_configuration.py
```

La prueba de wheel instalado debe ejecutarse en otro entorno/cwd y usar el paquete construido, no el editable de `.venv`. De lo contrario puede pasar porque encuentra `skills/core` en el repositorio y ocultar U-12.

## 10. Cierre

- [ ] U-12 resuelto en instalación limpia y no sólo en zipimport.
- [ ] Runtime y catálogo tienen relación de versiones/documentación clara.
- [ ] Las nueve Skills validadas, sin autoridad paralela.
- [ ] Hosts probados identificados con evidencia real.
- [ ] Instalación sin secretos en archivos versionados ni simulación implícita.
- [ ] El usuario puede distinguir problema de conexión, cobertura y autorización.

Salida: paquete y guía operables. Riesgo residual: actualizaciones de host, certificados y SDK requieren revalidación; un wrapper Markdown no garantiza por sí solo interoperabilidad indefinida.

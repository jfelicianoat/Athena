# Resumen ejecutivo de modernización

La modernización propuesta conserva los invariantes de seguridad y completion, pero reduce el coste de evolución del sistema.

Target:
- Python 3.14 como runtime principal.
- Toolchain actual (pytest 9, mypy 2, setuptools 84.x, ruff reciente) validado y bloqueado.
- CI reproducible con quality gates.
- Persistencia versionada con migraciones.
- Módulos críticos divididos por responsabilidad.
- Observabilidad end-to-end con correlación.
- Contract tests para providers, service y channels.
- Hardening de red, recovery y concurrencia.

# Diferencias clave vs plan conservador

| Área | Conservador | Modernización |
|---|---|---|
| Python | mantener compat 3.11; CI 3.13 | mínimo 3.14 si consumidores lo permiten |
| Toolchain | stay-in-major primero | pytest 9 + mypy 2 |
| Arquitectura | refactor mínimo | separar coordinadores/adapters/stores |
| HTTP | hardening local | abstracción HTTP común o ASGI |
| SQLite | versionado mínimo | capa de DB/migraciones cohesionada |
| Observabilidad | mejorar campos | logging/tracing/metrics correlacionados |
| Tests | gates actuales | contract + failure + migration tests |
| CI | checks básicos | matrices, cache, coverage útil, security gates |

# Targets recomendados (modernización) y justificación

| Componente | Target | Justificación |
|---|---|---|
| Python | 3.14.x | Serie estable actual consultada; 3.14.7 disponible |
| pytest | 9.1.x | Major actual |
| mypy | 2.3.x | Major actual |
| setuptools | 84.x | Build backend actual consultado; PEP 639 |
| ruff | release reciente validada y bloqueada | Tooling rápido, ya adoptado |
| Packaging | PEP 639/SPDX | Elimina deprecation y contradicción |
| SQLite schema | migraciones versionadas | Necesario para lifecycle durable |
| CI | matriz + gates + cache | Reproducibilidad |
| Observabilidad | logging JSON + métricas + trace context | Diagnóstico de runs distribuidas entre adapters |

# Plan por áreas (arquitectura/observabilidad/tests/CI/deps/infra)

## Arquitectura

### A. Descomponer `AgentLoop`
Extraer:
- `IterationController`
- `ToolWaveExecutor`
- `ProgressGuard`
- `VerificationCoordinator`
- `RunFinalizer`

`AgentLoop` queda como fachada/orquestador de alto nivel.

### B. Descomponer `service/server.py`
Separar:
- transporte HTTP,
- request parser,
- auth,
- router,
- SSE writer,
- endpoint handlers.

Si se acepta una dependencia runtime, evaluar ASGI. No introducir framework solo por moda: debe reducir código y edge cases sin debilitar el modelo loopback/token.

### C. Persistencia
Crear:
- `Database`
- `Migration`
- `MigrationRunner`
- adapters/repositorios por agregado.

Mantener interfaces actuales para evitar refactor big-bang.

### D. Providers
Separar:
- transporte,
- retries/polling,
- serialización,
- parsing de decisiones.

## Observabilidad
- Campos canónicos: `run_id`, `task_id`, `session_id`, `tool_call_id`, `provider`, `operation`, `duration_ms`, `status`.
- Logger estructurado.
- Métricas de latencia y errores por boundary.
- Trazas opcionales alrededor de model call/tool call/verification/persistence.
- Redacción central antes de exportar eventos.

## Tests
- Contract tests de `ModelProvider`.
- Contract tests de channels.
- Golden tests del wire protocol.
- Migration tests desde snapshots de DB.
- Fault injection en SQLite, timeouts, cancelación y procesos.
- Property/fuzz tests para parser HTTP y path handling.
- Tests de límites de response/payload.

## CI
- Python 3.14 principal y, durante transición, 3.13.
- Cache de dependencias.
- `pytest`, `ruff`, `mypy`.
- coverage como señal, no como objetivo ciego.
- build wheel/sdist.
- secret scanning.
- dependency/security audit.
- artefactos de test en fallos.

## Dependencias
- Adoptar toolchain majors actuales.
- Mantener runtime dependencies mínimas.
- Si se introduce cliente HTTP/ASGI, hacerlo detrás de adapter y con justificación ADR.

## Infra
No existe infraestructura declarativa en el ZIP; no se inventan contenedores/Kubernetes/Terraform como requisito.

# Touchpoints

## PHASE_0

| Ruta | Símbolo/área | Cambio | Riesgo | Test |
|---|---|---|---|---|
| `pyproject.toml` | project/tool config | Python 3.14, majors tooling, PEP 639 | High | build + full gates |
| `src/athena/agent_loop.py` | `AgentLoop` | extraer coordinadores | High | tests loop/repair/verification |
| `src/athena/verification.py` | planners/policies | separar discovery/integrity/execution | High | verification suite |
| `src/athena/planning.py` | modelos/validadores | modularizar | Medium | planning/graph tests |
| `src/athena/adapters/service/server.py` | `AthenaService` | separar transport/router/handlers | High | service API contract |
| `src/athena/adapters/service/orchestration.py` | `Orchestrator` | reducir responsabilidades | High | orchestration tests |
| `src/athena/adapters/service/runs.py` | registry/runs | separar storage/lifecycle | Medium | run lifecycle |
| stores SQLite | schema/connect | Database + migrations | High | migration fixtures |
| providers HTTP | adapters | transporte común + límites | Medium | provider contracts |
| `src/athena/security.py` | redaction | política export-safe | Medium | secret corpus |

## PLAN_POR_FASES

### Fase 0 — Fundación
- CI y toolchain reproducible.
- licencia/packaging.
- Python 3.14 branch.
- tests de contrato y snapshots de DB.
- logging/correlation schema.
- migration runner sin mover aún los repositorios.

### Fase 1 — Upgrades mayores
- Python 3.14.
- pytest 9.
- mypy 2.
- setuptools 84.x.
- ruff reciente bloqueado.
- resolver breaking changes.

### Fase 2 — Refactors/deuda
Orden recomendado:
1. `src/athena/adapters/service`
2. `src/athena/agent_loop.py`
3. `src/athena/verification.py`
4. `src/athena/planning.py`
5. persistencia
6. providers/channels
7. desktop

### Fase 3 — Hardening
- fuzz/property tests,
- fault injection,
- límites de recursos,
- métricas/tracing,
- recovery y rollback,
- pruebas Windows/desktop,
- revisión de seguridad completa.

## NEXT_PHASE_ASK
Para continuar con el inventario de touchpoints al 100%, seleccionar una carpeta/módulo: **`src/athena/adapters/service`**, **`src/athena` core**, **persistencia**, **providers/channels**, **desktop** o **tests**.

# Roadmap (fases claras)

## Fase 0 — Fundación
Resultado esperado: build reproducible y baseline medible.

## Fase 1 — Runtime/toolchain
Resultado esperado: Python 3.14 + toolchain actual, sin deuda de majors.

## Fase 2 — Arquitectura
Resultado esperado: módulos más pequeños, boundaries explícitos, misma semántica externa.

## Fase 3 — Hardening
Resultado esperado: mejores garantías ante inputs hostiles, fallos parciales, restart y upgrades de estado.

# TAREAS_UPGRADE_MODERNIZACION

## UGM-001 — Baseline reproducible
- **Archivos:** `pyproject.toml`, nuevo lock/constraints, CI.
- **Pasos:** fijar toolchain; añadir build/test/lint/type gates; guardar baseline.
- **Aceptación:** entorno limpio reproducible y pipeline verde.
- **Dependencias:** ninguna.
- **Prioridad:** P0
- **Severidad/riesgo:** High / 20
- **Esfuerzo:** M

## UGM-002 — Migrar a Python 3.14
- **Archivos:** `pyproject.toml`, CI, código que falle.
- **Pasos:** rama de compat; ejecutar gates; resolver deprecations; validar Tk/Windows.
- **Aceptación:** gates y smokes verdes en 3.14.
- **Dependencias:** UGM-001.
- **Prioridad:** P0
- **Severidad/riesgo:** High / 16
- **Esfuerzo:** M

## UGM-003 — Toolchain pytest 9 + mypy 2
- **Archivos:** `pyproject.toml`, tests/typing.
- **Pasos:** subir majors de uno en uno; corregir incompatibilidades.
- **Aceptación:** sin suppressions globales nuevas para forzar verde.
- **Dependencias:** UGM-001.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** M

## UGM-004 — Capa de migraciones SQLite
- **Archivos:** todos los stores SQLite + nuevo paquete persistence.
- **Pasos:** versionado; migrador; snapshots; transacciones; estrategia rollback de schema.
- **Aceptación:** DB de versiones anteriores migra de forma determinista.
- **Dependencias:** baseline de tests.
- **Prioridad:** P0
- **Severidad/riesgo:** High / 20
- **Esfuerzo:** L

## UGM-005 — Modularizar service
- **Archivos:** `adapters/service/server.py`, `runs.py`, `orchestration.py`.
- **Pasos:** extraer transporte/router/handlers; mantener wire contract.
- **Aceptación:** contract tests sin cambios de API no documentados.
- **Dependencias:** UGM-001.
- **Prioridad:** P1
- **Severidad/riesgo:** High / 16
- **Esfuerzo:** L

## UGM-006 — Modularizar AgentLoop/Verification
- **Archivos:** `agent_loop.py`, `verification.py`.
- **Pasos:** extraer coordinadores detrás de interfaces internas; migración incremental.
- **Aceptación:** comportamiento de completion/repair/cancellation idéntico según tests.
- **Dependencias:** UGM-001.
- **Prioridad:** P1
- **Severidad/riesgo:** High / 16
- **Esfuerzo:** L

## UGM-007 — Observabilidad estructurada
- **Archivos:** `logging.py`, `metrics.py`, events, adapters.
- **Pasos:** schema de correlación; JSON logging; timings; errores.
- **Aceptación:** un run puede reconstruirse por `run_id` sin parsear texto libre.
- **Dependencias:** ninguna estricta.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** M

## UGM-008 — Hardening HTTP
- **Archivos:** providers, Telegram, service.
- **Pasos:** límites; parser tests; transporte común o ASGI si se aprueba ADR.
- **Aceptación:** límites uniformes; errores tipados; fuzz básico.
- **Dependencias:** UGM-001.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** M

## UGM-009 — Contract tests de adapters
- **Archivos:** tests nuevos.
- **Pasos:** suite reusable para ModelProvider/channel/service wire.
- **Aceptación:** cualquier adapter cumple los mismos invariantes.
- **Dependencias:** UGM-001.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** M

# Verificación y checklist post-modernización

- [ ] wheel/sdist build limpio
- [ ] Python 3.14 gates verdes
- [ ] pytest 9
- [ ] ruff check + format
- [ ] mypy 2 strict
- [ ] contract tests providers/channels/service
- [ ] migration tests con DB antigua
- [ ] recovery/restart tests
- [ ] cancellation/process-tree tests POSIX y Windows
- [ ] fuzz/property tests de paths y HTTP
- [ ] secret scanning
- [ ] dependency/security audit
- [ ] smoke CLI
- [ ] smoke service/SSE/auth
- [ ] smoke desktop Windows
- [ ] rollback verificado
- [ ] métricas y logs correlacionables

# Supuestos y límites

- Modernización intencionalmente ambiciosa.
- Cambiar el mínimo de Python exige confirmar consumidores externos.
- No se presupone que adoptar ASGI/cliente HTTP sea obligatorio; debe justificar su coste.
- No se ejecutaron gates en esta auditoría.
- No se atribuyen vulnerabilidades no demostradas al proyecto.

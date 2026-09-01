# Resumen ejecutivo

> **Fuente actual verificada (2026-09-01):** repositorio público `jfelicianoat/Athena`, rama `main`: https://github.com/jfelicianoat/Athena  
> El contraste web confirma que `pyproject.toml`, `LICENSE`, `README.md` y `docs/CURRENT_STATE.md` mantienen los puntos materiales usados en este informe. `docs/CURRENT_STATE.md` declara estar reconciliado con el source tree el 2026-08-23.


- Athena es un runtime de agentes autónomos en Python con diseño explícitamente provider-neutral, límites de permisos deterministas, verificación antes de completar, persistencia SQLite, recuperación, delegación y varias interfaces (CLI, desktop, servicio HTTP/SSE y Telegram).
- La arquitectura conceptual está bien documentada mediante ADRs y el código refleja varias de esas fronteras: `ModelProvider`, `Tool`, `PermissionEngine`, `Workspace`, `EventBus` y stores.
- **High — Packaging/Legal:** `pyproject.toml` declara `license = { text = "Proprietary" }`, mientras `LICENSE` contiene MIT. La metadata distribuida y la licencia del repositorio se contradicen.
- **High — Reproducibilidad/Toolchain:** no hay lockfile ni configuración CI en el ZIP. Los gates del README (`pytest`, `ruff check`, `ruff format --check`, `mypy`) dependen por tanto de ejecución manual y de rangos de versiones amplios.
- **Medium — Mantenibilidad:** varios módulos críticos son demasiado grandes y concentran múltiples responsabilidades: `agent_loop.py` (~1450 líneas), `planning.py` (~1044), `adapters/service/server.py` (~946), `adapters/service/runs.py` (~942), `orchestration.py` (~855) y `verification.py` (~842).
- **Medium — Persistencia:** la capa SQLite está repartida entre varios módulos (`session_store`, `stores`, `graph_store`, `run_event_log`, `project_memory`, `metrics`, `identity`) con creación de esquemas local a cada módulo y sin mecanismo de migraciones/versionado visible.
- **Medium — Transporte HTTP:** el servicio local implementa un parser HTTP/1.1 manual. Está protegido por loopback, token bearer y límites de tamaño, pero aumenta la superficie de edge cases y responsabilidad dentro de `server.py`.
- **Medium — Compatibilidad futura:** el proyecto declara `Python >=3.11` sin límite superior ni matriz de compatibilidad visible. La base mínima está en fase de “security fixes only”; conviene validar 3.13/3.14 y mover el target de desarrollo.
- **Low/positivo — Seguridad:** hay varias defensas explícitas y verificables por lectura: confinamiento de workspace, clasificación de comandos, rechazo de shell metacharacters, escalado de operaciones destructivas, tokens comparados con `hmac.compare_digest`, redacción de secretos y servicio restringido a loopback.
- El análisis es **estático por lectura**. No se ejecutaron tests, linters, mypy, binarios externos, red real ni UI; por tanto no se afirma que los gates estén verdes.

# Mapa del sistema

## Inventario resumido

| Área | Rutas principales | Rol |
|---|---|---|
| Core runtime | `src/athena/*.py` | Loop, herramientas, permisos, eventos, verificación, estado, memoria, recuperación |
| Providers | `src/athena/adapters/ai_broker.py`, `openai_compatible.py` | Adaptación a proveedores de modelo |
| Servicio local | `src/athena_service.py`, `src/athena/adapters/service/*` | HTTP/SSE loopback, runs, approvals, projections |
| Desktop | `src/athena_desktop/*` | GUI Tk, lifecycle del servicio |
| Telegram | `src/athena_telegram/*` | Canal Telegram |
| Persistencia | `session_store.py`, `stores.py`, `graph_store.py`, `run_event_log.py`, `project_memory.py`, `metrics.py`, `identity.py` | SQLite y estado durable |
| Ejecución y mutación | `process_tools.py`, `mutation_tools.py`, `repository_tools.py`, `git_tools.py` | I/O, procesos, edición y git local |
| Tests | `tests/*.py` | 68 ficheros de test |
| Documentación | `docs/*`, `docs/adr/*` | Estado actual, seguridad y decisiones de arquitectura |
| Build | `pyproject.toml` | Packaging, Python mínimo y toolchain dev |

## Entrypoints

- `athena = athena.cli:main`
- `athena-service = athena_service:main`
- `athena-desktop = athena_desktop.app:main`
- `python -m athena_telegram`
- `python -m athena_desktop`

## Árbol lógico

```text
interface (CLI/Desktop/Service/Telegram)
        |
        v
  orchestration / AgentLoop
        |
        +--> ModelProvider
        +--> ToolRegistry / ToolExecutor
        |       +--> PermissionEngine
        |       +--> Workspace
        |       +--> process/mutation/repository/git tools
        |
        +--> Verification
        +--> Events / RunEventLog
        +--> Tasks / Delegation / GraphExecutor
        +--> SQLite state stores
```

# Cómo funciona

1. Una interfaz crea/configura una ejecución.
2. El runtime prepara workspace, capacidades, presupuesto y proveedor de modelo.
3. `AgentLoop` itera decisiones del modelo y llamadas a herramientas.
4. Las herramientas pasan por validación, permisos y límites de workspace.
5. Los efectos y estados se proyectan mediante eventos.
6. La finalización requiere verificación; si hay regresión, existe ciclo de reparación acotado.
7. El estado relevante puede persistirse en SQLite para reanudación y recuperación.
8. El servicio local expone esta maquinaria por HTTP/SSE y protege las rutas, salvo health, con bearer token.

# Hallazgos por fichero

## `pyproject.toml`

### Rol del fichero
Fuente principal de metadata, Python mínimo, build backend, entrypoints y herramientas de desarrollo.

### Hallazgos

| Severidad | Tipo | Impacto | Prob. | Riesgo | Evidencia | Recomendación | Cambio sugerido |
|---|---|---:|---:|---:|---|---|---|
| High | Config/Correctness | 5 | 4 | 20 | `license = { text = "Proprietary" }` pero `LICENSE` es MIT | Unificar la licencia real | Si MIT es la intención: `license = "MIT"` y `license-files = ["LICENSE"]` |
| Medium | Dependency | 4 | 4 | 16 | No existe lockfile; deps dev son rangos amplios | Añadir entorno reproducible | Generar lock para desarrollo/CI o constraints versionadas |
| Medium | Obsolescence | 3 | 4 | 12 | `requires-python = ">=3.11"` y tool targets `py311` | Añadir matriz 3.11/3.13/3.14 y elevar target | Conservador: mantener compat 3.11 pero desarrollar/CI en 3.13; modernización: target 3.14 |
| Medium | Dependency | 3 | 5 | 15 | `pytest>=8.3,<9` impide pytest 9; `mypy>=1.11,<2` impide mypy 2 | Planificar majors de toolchain | Probar pytest 9 y mypy 2 en rama separada |
| Low | Build | 2 | 4 | 8 | `setuptools>=77` sin cota ni lock; formato `license` legacy | Adoptar metadata PEP 639 | Ver cambio de licencia anterior |

## `LICENSE`

### Rol del fichero
Licencia legal incluida en el repositorio.

### Hallazgos
- **High / Correctness / Riesgo 20:** contiene texto MIT completo y contradice la metadata `Proprietary` de `pyproject.toml`. Resolver antes de publicar artefactos.

## `README.md`

### Rol del fichero
Descripción operativa, arquitectura y comandos de desarrollo.

### Hallazgos
- **Medium / Testing / Riesgo 12:** define cuatro quality gates como obligatorios, pero no hay CI visible en el ZIP que los haga cumplir.
- **Low / Documentation:** la explicación del modelo de seguridad está bien alineada con `docs/security-model.md` y con los módulos revisados.

## `src/athena/agent_loop.py`

### Rol del fichero
Orquestación central de una ejecución: iteraciones, contexto, progreso, tool calls, verificación, reparación y finalización.

### Hallazgos
- **Medium / Architecture / Riesgo 15:** ~1450 líneas y ~31 funciones; concentra demasiadas responsabilidades de lifecycle.
- El riesgo no es un bug demostrado, sino mayor coste de cambio, pruebas y razonamiento.
- Cambio sugerido: extraer al menos `VerificationCoordinator`, `ToolWaveExecutor`, `ProgressGuard` y `RunFinalizer` manteniendo el contrato público.

## `src/athena/planning.py`

### Rol del fichero
Planificación y estructuras asociadas.

### Hallazgos
- **Medium / Maintainability / Riesgo 12:** ~1044 líneas, 18 clases y >50 funciones detectadas por AST. Es un candidato claro a división por dominio (modelo, validación, serialización/estado, helpers).

## `src/athena/adapters/service/server.py`

### Rol del fichero
Servidor HTTP/1.1 + SSE loopback y routing del API local.

### Hallazgos
- **Positivo / Security:** `ServiceConfig` rechaza hosts fuera de `127.0.0.1`, `::1` y `localhost`; exige token no vacío.
- **Positivo / Security:** bearer token comparado con `hmac.compare_digest`; `/v1/health` es la excepción explícita.
- **Positivo / Reliability:** límites visibles de headers (16 KiB) y body (4 MiB).
- **Medium / Architecture / Riesgo 15:** ~946 líneas y parser HTTP manual. Mezcla transporte, auth, routing, validación, SSE y lógica de endpoints.
- **Low/Medium / Correctness / Riesgo 8:** valores `Content-Length` malformados/negativos pueden caer en la ruta genérica de error en lugar de responder 400 de forma intencional. Añadir validación explícita.
- Cambio sugerido conservador: encapsular parseo/validación de request sin cambiar servidor. Modernización: usar una capa ASGI ligera solo si se acepta dependencia runtime.

## `src/athena/workspace.py`

### Rol del fichero
Boundary de filesystem.

### Hallazgos
- **Positivo / Security:** resolución canónica y boundary explícito; es una de las defensas estructurales más importantes del sistema.
- Mantener tests específicos de traversal, symlinks/junctions, inexistentes internos y rutas externas inexistentes.

## `src/athena/process_tools.py`

### Rol del fichero
Clasificación y ejecución de comandos locales.

### Hallazgos
- **Positivo / Security:** clasificación previa del comando, rechazo de metacaracteres de shell, timeout y terminación del árbol de procesos.
- **Medium / Maintainability / Riesgo 12:** ~646 líneas y política + parsing + spawn + lifecycle en un mismo módulo.
- Cambio sugerido: separar `CommandPolicy`, parser/plataforma y `ProcessRunner`.

## `src/athena/security.py`

### Rol del fichero
Redacción determinista de secretos en observabilidad/eventos.

### Hallazgos
- **Positivo / Security:** redacción recursiva por claves y patrones de texto.
- **Medium / Security / Riesgo 10:** toda redacción regex es necesariamente parcial frente a formatos de credenciales desconocidos. Tratarla como defensa secundaria, nunca como barrera primaria.
- Añadir corpus de tests con formatos de tokens propios del sistema y valores falsos positivos.

## `src/athena/adapters/openai_compatible.py`

### Rol del fichero
Provider HTTP(S) compatible con APIs estilo OpenAI.

### Hallazgos
- **Positivo / Reliability:** timeout configurable y cierre de conexión ligado a cancelación.
- **Medium / Reliability / Riesgo 12:** `response.read()` carga la respuesta completa en memoria sin límite explícito en este adapter. Un endpoint controlado o defectuoso puede devolver payloads grandes.
- Cambio sugerido: límite de bytes de respuesta y error específico al excederlo.
- **Low / Architecture:** `asyncio.to_thread` envuelve I/O HTTP bloqueante; válido como diseño sin dependencias, pero limita observabilidad/cancelación fina comparado con un cliente async.

## `src/athena/adapters/ai_broker.py`

### Rol del fichero
Adapter a AI Broker y traducción de contratos/tool decisions.

### Hallazgos
- **Medium / Maintainability / Riesgo 12:** ~658 líneas; transporte, polling/retry y traducción estructurada viven juntos.
- Separar transporte de traducción de decisiones mejoraría testabilidad sin cambiar contratos.

## `src/athena/verification.py`

### Rol del fichero
Descubrimiento de checks, baseline, integridad de cambios y criterio de completion.

### Hallazgos
- **Medium / Architecture / Riesgo 15:** ~842 líneas y 17 clases; es una política de negocio crítica y merece boundaries más estrechos.
- **Positivo / Correctness:** el diseño distingue baseline, evidencia y completion; evita considerar “verde” un check que ya estaba rojo.

## `src/athena/session_store.py`
## `src/athena/stores.py`
## `src/athena/graph_store.py`
## `src/athena/run_event_log.py`
## `src/athena/project_memory.py`
## `src/athena/metrics.py`
## `src/athena/identity.py`

### Rol del fichero
Persistencia SQLite de distintos agregados.

### Hallazgos
- **Medium / Architecture / Riesgo 16:** cada módulo gestiona su conexión/esquema localmente; no se observó `PRAGMA user_version`, tabla de migraciones o framework de migración.
- **Medium / Reliability:** a medida que evolucione el schema, el riesgo principal será abrir bases antiguas con código nuevo.
- Cambio sugerido: introducir una única capa `Database`/`MigrationManager`, versión de schema y migraciones idempotentes antes de ampliar más las tablas.
- **Positivo:** varios stores habilitan WAL; `identity` y `session_store` activan foreign keys.

## `src/athena_desktop/service.py`

### Rol del fichero
Arranque/parada del servicio gestionado por la GUI.

### Hallazgos
- **Low/Medium / Reliability / Riesgo 8:** usa threads daemon para drenar stdout/stderr. Revisar lifecycle y cierre en tests de proceso real, especialmente en Windows.
- La verificación manual de UI/proceso sigue siendo necesaria.

## `src/athena_telegram/api.py`

### Rol del fichero
Cliente de Telegram.

### Hallazgos
- **Low / Maintainability:** cliente HTTP implementado con stdlib, coherente con el objetivo “sin dependencias”, pero duplica preocupaciones de transporte ya presentes en adapters de modelo.
- Modernización: factorizar una abstracción HTTP interna mínima o adoptar cliente común si se permite dependencia.

# Hallazgos transversales

## Arquitectura
- Buenas fronteras conceptuales y ADRs abundantes.
- El principal problema arquitectónico actual es **concentración de responsabilidades dentro de módulos grandes**, no ausencia de diseño.
- La persistencia es el segundo foco: múltiples stores SQLite sin mecanismo de evolución de schema visible.

## Fiabilidad/resiliencia
- Hay cancelación, timeouts y estado de recovery explícitos.
- Deben añadirse pruebas de fallo de almacenamiento/migraciones y payloads HTTP grandes/malformados.
- Los adapters HTTP deberían tener límites explícitos de respuesta, no solo timeout.

## Rendimiento
- No se identifica un hotspot demostrable sin ejecución/profiling.
- Los SQLite stores abren conexiones localmente; antes de optimizar se necesita medir latencia/contención.
- No se recomienda introducir pooling sin evidencia.

## Seguridad
Fortalezas observadas:
- workspace como boundary,
- tool permissions,
- comandos sin shell,
- R4 deny,
- token bearer del servicio,
- redacción de secretos,
- approval explícita.

Riesgos a vigilar:
- coherencia de todos los endpoints nuevos con auth,
- límites de payloads en clientes HTTP,
- persistencia de datos sensibles en artifacts/eventos,
- evolución segura del identity store.

## Observabilidad
- Existe módulo `logging.py`, eventos, `metrics.py` y run event log.
- Modernización recomendada: correlación uniforme `run_id/task_id/tool_call_id`, logging estructurado consistente y métricas de latencia/error por boundary.

## Tests
- 68 ficheros de tests indican inversión relevante.
- No se ejecutaron; no se afirma cobertura ni estado verde.
- Falta automatización CI visible en el ZIP.

## Configuración
- Configuración por entorno y dataclasses en varias interfaces.
- Debe existir una fuente única/documentada para settings compartidos para evitar divergencia entre CLI, service, desktop y Telegram.

## Dependencias
- No hay dependencias runtime de terceros declaradas.
- Toolchain dev está versionado por rangos, no reproducido por lock/constraints.

# Estándares recomendados

- Límite orientativo de 400–600 líneas por módulo crítico; extraer responsabilidades cuando el cambio obliga a tocar varias políticas a la vez.
- Formato/lint/type-check como gates automáticos de PR.
- Añadir CI con Python mínimo y target recomendado.
- Adoptar un lock o constraints para toolchain.
- Versionar schema SQLite y mantener migraciones forward-only probadas con fixtures de versiones anteriores.
- Logging estructurado con correlación de run/task/tool.
- ADR para cambios de wire protocol y schema persistente.
- Threat tests para workspace escape, command classification, auth y redacción.

# Roadmap

## Quick wins
1. Corregir conflicto de licencia.
2. Añadir CI para los cuatro gates documentados.
3. Añadir lock/constraints de desarrollo.
4. Validar explícitamente `Content-Length`.
5. Añadir límite de respuesta a adapters HTTP.
6. Añadir matriz Python 3.11 + target nuevo.

## Medio plazo
1. Introducir versionado/migraciones SQLite.
2. Dividir `server.py`, `agent_loop.py`, `verification.py` y `planning.py`.
3. Centralizar configuración compartida.
4. Unificar primitivas de transporte HTTP si aporta ahorro real.

## Largo plazo
1. Target Python 3.14.
2. Observabilidad estructurada end-to-end.
3. Contract tests de providers/channels.
4. Hardening de recovery, persistencia y pruebas de fallos.

# Tareas para ejecución

| ID | Título | Archivos | Prioridad | Severidad/riesgo | Esfuerzo |
|---|---|---|---|---|---|
| AUD-001 | Resolver licencia contradictoria | `pyproject.toml`, `LICENSE` | P0 | High / 20 | S |
| AUD-002 | Automatizar quality gates | nuevo workflow CI, `pyproject.toml` | P0 | High / 16 | S |
| AUD-003 | Fijar toolchain reproducible | lock/constraints, docs | P1 | Medium / 16 | S |
| AUD-004 | Añadir estrategia de migraciones SQLite | stores SQLite | P1 | Medium / 16 | M |
| AUD-005 | Limitar respuestas HTTP de providers | adapters HTTP | P1 | Medium / 12 | S |
| AUD-006 | Endurecer parsing de request HTTP | `server.py` | P1 | Medium / 8 | S |
| AUD-007 | Descomponer módulos oversized | loop/planning/service/verification | P2 | Medium / 15 | L |
| AUD-008 | Matriz Python moderna | CI, `pyproject.toml` | P1 | Medium / 12 | M |

# Supuestos y límites del análisis

- Entrada tratada como **modo proyecto**.
- Se inspeccionaron estáticamente los 153 ficheros Python; todos fueron parseables por AST.
- No se ejecutaron `pytest`, `ruff`, `mypy`, procesos externos, red, SQLite real ni interfaces gráficas.
- No se midió cobertura, rendimiento ni comportamiento en Windows.
- No se encontraron lockfiles, Dockerfiles ni configuración CI en el contenido del ZIP.
- Los documentos de aceptación se consideran evidencia histórica; `docs/CURRENT_STATE.md` se tomó como índice de estado, coherente con su propio texto.

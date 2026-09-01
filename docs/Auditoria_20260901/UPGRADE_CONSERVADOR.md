# Resumen ejecutivo de actualización (conservador)

> **Fuente actual verificada (2026-09-01):** repositorio público `jfelicianoat/Athena`, rama `main`: https://github.com/jfelicianoat/Athena  
> El contraste web confirma que `pyproject.toml`, `LICENSE`, `README.md` y `docs/CURRENT_STATE.md` mantienen los puntos materiales usados en este informe. `docs/CURRENT_STATE.md` declara estar reconciliado con el source tree el 2026-08-23.


Objetivo: recuperar reproducibilidad, compatibilidad y soporte con el menor cambio posible, sin re-arquitecturar el runtime.

Prioridades:
1. Resolver metadata de licencia.
2. Mantener compatibilidad con Python 3.11, pero introducir Python 3.13 como runtime de desarrollo/CI recomendado.
3. Actualizar toolchain de forma controlada sin saltar majors innecesariamente en el primer lote.
4. Automatizar los cuatro gates ya documentados.
5. Añadir versionado básico de schema SQLite antes de cambios persistentes futuros.
6. Corregir pequeños hardenings del transporte sin cambiar la API.

# Fuentes de versiones (FUENTES_DE_VERSION)

| Componente | Versión detectada | Fuente | Confianza | Comentario |
|---|---|---|---|---|
| Python | `>=3.11` | `pyproject.toml` | Alta | Mínimo, no runtime exacto |
| Ruff target | `py311` | `pyproject.toml` | Alta | Alineado al mínimo |
| setuptools | `>=77` | `pyproject.toml` | Alta | Sin máximo/lock |
| pytest | `>=8.3,<9` | `pyproject.toml` | Alta | Major 9 bloqueado |
| mypy | `>=1.11,<2` | `pyproject.toml` | Alta | Major 2 bloqueado |
| ruff | `>=0.8,<1` | `pyproject.toml` | Alta | Rango amplio |
| Runtime deps | ninguna declarada | `pyproject.toml` | Alta | Stdlib-first |
| Lockfile | no detectado | proyecto | Alta | Reproducibilidad limitada |
| CI | no detectado | proyecto | Alta | Gates solo documentados |
| SQLite | stdlib | código | Alta | Varios stores/esquemas |

# Referencias (EOL/soporte/CVEs/breaking changes) con fecha consultada

Fecha consultada: **2026-09-01**.

| Afirmación | Fuente | Evidencia |
|---|---|---|
| Python 3.11 está en fase de security-fixes-only hasta octubre de 2027 | Python.org / PEP 664 | 3.11 dejó bugfix regulares y binarios; security releases source-only hasta ~octubre 2027 |
| Python 3.14 es la serie estable más reciente; 3.14.7 fue publicada 2026-08-05 | Python.org | Release 3.14.7 |
| pytest 9.1.1 es release actual consultada | PyPI | Publicada 2026-06-19; el proyecto bloquea `<9` |
| mypy 2.3.1 es release actual consultada | PyPI | Publicada 2026-08-15; el proyecto bloquea `<2` |
| setuptools 84.0.0 es release actual consultada | PyPI | Publicada 2026-08-08 |
| El formato `project.license = {text=...}` está deprecado desde setuptools 77 y PEP 639 recomienda SPDX string | documentación setuptools/PyPA | Migración a `license = "MIT"` + `license-files` |
| ruff continúa en rama 0.x y la release consultada fue publicada 2026-08-20 | PyPI | El rango `<1` aún permite actualización dentro del major |

No se atribuyen CVEs a dependencias runtime del proyecto porque no hay dependencias runtime de terceros declaradas. Tampoco se infieren CVEs de la stdlib sin vincularlos a una versión concreta instalada.

# Matriz de obsolescencia (MATRIZ_DE_OBSOLESCENCIA)

| Área | Estado | Evidencia | Consecuencia | Acción |
|---|---|---|---|---|
| Python 3.11 mínimo | Riesgo | Security-only; fin previsto 2027-10 | Menos bugfixes y sin binarios regulares | Mantener compatibilidad temporal y probar 3.13 |
| pytest 8.x | Riesgo | Proyecto `<9`, latest 9.1.1 | Deuda de major | No saltar aún; abrir tarea dedicada |
| mypy 1.x | Riesgo | Proyecto `<2`, latest 2.3.1 | Deuda de major | No saltar aún; validar 2.x aparte |
| ruff 0.x | OK/Riesgo | `<1`, latest sigue 0.x | Puede variar mucho sin lock | Fijar versión en CI/lock |
| setuptools | Riesgo | `>=77` sin lock, latest 84 | Builds no reproducibles; metadata legacy | Pin/constraints + PEP 639 |
| Runtime deps | OK | ninguna | Superficie de supply chain baja | Mantener |
| CI | Obsoleto/ausente | no config visible | Gates no forzados | Añadir workflow |
| Persistencia | Riesgo | schemas locales sin versión | Upgrade de datos frágil | Añadir schema version |
| Packaging license | Obsoleto/incorrecto | Proprietary vs MIT | Riesgo legal/distribución | Corregir inmediatamente |

# Targets recomendados (mínimo viable) y justificación

| Componente | Target conservador | Justificación |
|---|---|---|
| Python | compat `>=3.11`, CI principal 3.13 | Evita romper consumidores 3.11 y sale de una base únicamente security-only para desarrollo |
| Ruff target | `py311` mientras siga compat 3.11 | No introducir sintaxis incompatible |
| pytest | última 8.x soportada por el rango inicialmente | Minimiza breaking changes; después evaluar 9.x |
| mypy | última 1.x inicialmente | Minimiza cambios de typing |
| ruff | versión concreta reciente `<1` mediante lock/constraints | Reproducibilidad |
| setuptools | pin/constraints de una 84.x probada | Compatible con PEP 639; evita build drift |
| Packaging license | SPDX `MIT` + `license-files = ["LICENSE"]`, si MIT es la intención legal | Alinea metadata con fichero |
| SQLite schema | versión 1 explícita + runner de migraciones | Fundación mínima sin rediseño |

# Plan de cambios por área (runtime/deps/toolchain/config/CI/infra)

## Runtime
- Mantener `requires-python >=3.11` en esta fase.
- Añadir CI en 3.11 y 3.13.
- Ejecutar todos los gates en ambas versiones antes de elevar mínimo.

## Dependencias
- Crear constraints/lock de desarrollo.
- Actualizar dentro de majors actuales primero.
- Abrir pruebas separadas para pytest 9 y mypy 2.

## Toolchain/build
- Corregir PEP 639 y licencia.
- Fijar setuptools probado.
- Mantener Ruff `target-version = "py311"` mientras se mantenga compat 3.11.

## Config
- No cambiar nombres de env ni defaults.
- Documentar qué settings son compartidos entre service/desktop/Telegram.

## CI
Pipeline mínimo:
1. install editable + dev,
2. `pytest`,
3. `ruff check .`,
4. `ruff format --check .`,
5. `mypy src tests`.

## Infra
No se detectaron Docker/Kubernetes/Terraform en el ZIP; no se propone trabajo ficticio.

# Touchpoints de cambio (TOUCHPOINTS_DE_CAMBIO)

## PHASE_0

| Ruta | Símbolo/área | Cambio esperado | Riesgo | Test |
|---|---|---|---|---|
| `pyproject.toml` | metadata | licencia PEP 639; constraints | High 20 | build metadata + install |
| `LICENSE` | licencia | confirmar MIT | High 20 | revisión legal/manual |
| `src/athena/adapters/service/server.py` | `_read_request` | validar Content-Length | Medium 8 | tests server malformed requests |
| `src/athena/adapters/openai_compatible.py` | `_request` | límite de respuesta | Medium 12 | provider fake oversized response |
| `src/athena/adapters/ai_broker.py` | `_blocking_call`/transporte | revisar mismo límite | Medium 12 | broker transport tests |
| `src/athena_telegram/api.py` | `_blocking_call` | revisar límite homogéneo | Low 8 | Telegram fake transport |
| stores SQLite | `_connect` / schema init | schema version inicial | Medium 16 | fixtures DB actuales |
| CI nuevo | gates | automatizar README | High 16 | pipeline |

## PLAN_POR_FASES para completar el 100%

- **Fase C1 — Core:** `src/athena/*.py` por paquetes funcionales: loop/tools/security/state.
- **Fase C2 — Service:** `src/athena/adapters/service/*` + `athena_service.py`.
- **Fase C3 — Providers/channels:** `adapters/ai_broker.py`, `openai_compatible.py`, `athena_telegram/*`.
- **Fase C4 — Desktop:** `athena_desktop/*`.
- **Fase C5 — Persistencia:** stores, identity, memory, metrics, logs y graph store.
- **Fase C6 — Tests/docs:** mapear cada cambio a tests y actualizar documentación.

## NEXT_PHASE_ASK
Para el siguiente lote de touchpoints al 100%, elegir una carpeta/módulo: **Core (`src/athena`)**, **Service**, **Providers/Telegram**, **Desktop**, **Persistencia** o **Tests/docs**.

# Roadmap (Quick wins / Medio / Largo)

## Quick wins
- Licencia.
- CI.
- Constraints/lock.
- Validación HTTP pequeña.
- Matriz 3.11/3.13.

## Medio
- Versionado schema.
- Evaluación pytest 9/mypy 2.
- Límite de response en adapters.

## Largo
- Elevar mínimo de Python cuando consumidores lo permitan.
- Refactors estructurales quedan fuera del conservador.

# TAREAS_UPGRADE_CONSERVADOR

## UGC-001 — Corregir metadata de licencia
- **Archivos:** `pyproject.toml`, `LICENSE`
- **Pasos:** confirmar licencia efectiva; si es MIT, usar SPDX `license = "MIT"` y `license-files`.
- **Aceptación:** metadata y fichero coinciden; build no emite deprecation por license table.
- **Dependencias:** decisión legal.
- **Prioridad:** P0
- **Severidad/riesgo:** High / 20
- **Esfuerzo:** S

## UGC-002 — Añadir CI de gates
- **Archivos:** nuevo workflow CI, `README.md`
- **Pasos:** matriz 3.11/3.13; install; pytest/ruff/mypy.
- **Aceptación:** PR no puede quedar verde con un gate rojo.
- **Dependencias:** UGC-003 recomendable.
- **Prioridad:** P0
- **Severidad/riesgo:** High / 16
- **Esfuerzo:** S

## UGC-003 — Fijar toolchain reproducible
- **Archivos:** nuevo lock/constraints, docs
- **Pasos:** resolver versiones actuales compatibles; commit del lock; CI consume ese fichero.
- **Aceptación:** dos entornos limpios instalan las mismas versiones.
- **Dependencias:** ninguna.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 16
- **Esfuerzo:** S

## UGC-004 — Matriz Python
- **Archivos:** CI, `pyproject.toml` si procede
- **Pasos:** validar 3.11 y 3.13; corregir incompatibilidades mínimas.
- **Aceptación:** gates verdes en ambas.
- **Dependencias:** UGC-002.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** M

## UGC-005 — Versionar schema SQLite
- **Archivos:** stores SQLite + nuevo helper de migraciones
- **Pasos:** introducir versión actual; migrador no destructivo; fixtures de DB previas.
- **Aceptación:** DB existente abre; nueva DB crea schema esperado; versión registrada.
- **Dependencias:** tests de persistencia.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 16
- **Esfuerzo:** M

## UGC-006 — Endurecer HTTP sin cambiar API
- **Archivos:** `server.py`, adapters HTTP
- **Pasos:** validar `Content-Length`; limitar response bytes.
- **Aceptación:** requests malformadas reciben error controlado; respuestas sobredimensionadas abortan de forma tipada.
- **Dependencias:** ninguna.
- **Prioridad:** P1
- **Severidad/riesgo:** Medium / 12
- **Esfuerzo:** S

## UGC-007 — Evaluar majors de tooling
- **Archivos:** `pyproject.toml`, tests
- **Pasos:** rama de prueba pytest 9 y mypy 2; documentar incompatibilidades; no mezclar con runtime changes.
- **Aceptación:** informe de cambios y decisión de adopción.
- **Dependencias:** CI.
- **Prioridad:** P2
- **Severidad/riesgo:** Medium / 9
- **Esfuerzo:** M

# Verificación y checklist post-upgrade

- [ ] build limpio
- [ ] install editable limpio
- [ ] `pytest`
- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `mypy src tests`
- [ ] smoke CLI
- [ ] smoke service + auth
- [ ] smoke desktop en Windows
- [ ] tests de migration de SQLite
- [ ] tests de workspace boundary
- [ ] tests de command policy
- [ ] secret scanning
- [ ] dependency audit de toolchain

# Supuestos y límites

- Plan basado en lectura estática.
- No se ejecutaron gates.
- No se conocen consumidores externos que obliguen a mantener Python 3.11.
- La licencia efectiva debe confirmarla el propietario; el plan no decide un hecho legal.

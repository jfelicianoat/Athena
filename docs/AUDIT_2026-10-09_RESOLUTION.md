# Resolución de la auditoría del 9/10-oct-2026 — Athena 0.3.1

Auditoría: `D:\Desarrollo\docs\Pruebas\Auditoria_Athena_2026-10-09` (informe principal,
registro de incidencias I-01…I-09, matriz de cobertura y mejoras propuestas). Corregida el
17-oct-2026 sobre HEAD `6dedf74`; versión **0.3.1**. Regresiones en
`tests/test_auditoria_20261009.py`.

## Incidencias

| ID | Qué decía la auditoría | Qué se hizo | Prueba |
| --- | --- | --- | --- |
| I-01 | `/v1/health` responde 200 sin token; el README decía que todo exige token | **Público por diseño** (ADR-017 lo declara; Agora y el escritorio lo usan para saber si hay algo escuchando, y solo revela `status`, `wire_version` y el número de runs). Se corrigió el README: la excepción está escrita y remite a `/v1/auth/check` para validar credenciales | `test_i01_health_is_public_by_design_and_says_so` |
| I-02 | `cancel` de un run inexistente → 400 | Run desconocido → **404** `not_found` en todas las rutas de run (`cancel`, `goal`, `resume`). Nuevo `UnknownRunError` (subclase de `ToolValidationError`, así los llamadores existentes no cambian). Cancelar un run **ya terminado** → **409** `run_finished` (antes respondía 202 «cancelling» porque los runs terminados siguen en memoria para el replay) | `test_i02_*` |
| I-03 | `GET /rollback` de un run inexistente → 200 `points: []` | Comprueba que el run existe: **404** | `test_i02_i03_an_unknown_run_is_404_on_every_run_route` |
| I-04 | Métodos no definidos → 404 | **405** con cabecera `Allow` cuando la ruta existe con otro método; 404 solo si no existe. La tabla de rutas tiene su prueba de desfase | `test_i04_*`, `test_every_route_in_the_table_is_served` |
| I-05 | Persistencia del escritorio entre reinicios sin verificar | **No era un bug.** La sonda del propio auditor (`tmp/athena_qa/probe2.log`) muestra que en la instancia instrumentada `run_athena()` nunca se llamó: sus clics no llegaron a esa ventana y el Historial que vio era de otra instancia. Reproducido con ventana real (clic en Iniciar, Alt+F4, proceso nuevo, clic en Historial): el trabajo aparece. Se añade la prueba de integración que pedía la auditoría | `test_i05_the_history_survives_the_process_that_wrote_it` + arnés de ventana |
| I-06 | Se puede elegir un modelo sin `completion` y el run muere a los 6 s | «Probar conexión» lee el catálogo del broker, **rechaza** un modelo sin `completion` explicando por qué y **rellena el desplegable** (ahora `Combobox` editable) con los aptos. Al pulsar Iniciar se valida igual antes de crear el run. Medido en vivo: el broker reenruta `tev1:4b` (solo `decision`) a `qwen3.8:27b` sin avisar y rechaza `clef-flash` con `MODEL_CAPABILITY_MISMATCH`: un modelo sin `completion` nunca es el que contesta. Un modelo que el catálogo no lista no se bloquea | `test_i06_*`, `tests/test_desktop.py` |
| I-07 | El tiempo máximo parece por turno | La ayuda dice que es del trabajo completo y cuántos turnos suele necesitar un cambio. Si el primer turno tarda más de un tercio del tiempo máximo, la actividad lo avisa | `test_i07_*` |
| I-08 | Botón «Copiar» no verificado | Verificado con clic real: el token del servicio llega al portapapeles | arnés de ventana |
| I-09 | El stream entrega aprobaciones ya resueltas/caducadas; contestarlas da 404 y un cliente ingenuo abandona el run | El stream reescribe un `permission.requested` cuya aprobación ya no está pendiente a `awaiting_decision: false, stale: true`, tanto en el replay de `Last-Event-ID` como si esperó en la cola mientras otro la resolvía. El 404 de `ack`/decisión dice ahora que la pregunta ya no está abierta y que se siga leyendo el stream. Documentado en README y ADR-017. ChatyGPT ya solo contesta si `awaiting_decision` es `true`, así que se beneficia sin cambios | `test_i09_*` + prueba en vivo |

## Mejoras propuestas

| # | Estado |
| --- | --- |
| 1 (I-05) | Hecha: ver I-05 |
| 2 (I-09) | Hecha: ver I-09 |
| 3 Avisar antes de un cambio que no se podrá verificar | Hecha. `verification_warning()` usa el mismo `VerificationPlanner` que el run: sin checks, o con «Ejecutar código» en «No», la ventana lo explica y pregunta antes de empezar (`test_mejora3_*`) |
| 4 (I-06) | Hecha |
| 5 (I-07) | Hecha |
| 6 Estado de la cola del broker en la UI | Hecha sin tocar el contrato del núcleo: la ventana lee la cola pública del broker (`GET /api/v1/queue`, `AiBrokerModelProvider.queue_load`) cada 15 s mientras hay un trabajo y lo dice en la fila «Cola del modelo» («Libre…» u «N esperando y M en curso: el broker está ocupado…»). Sin broker o sin respuesta no se inventa nada (`test_mejora6_*`; visto en la ventana real) |
| 7 (I-02/03/04) | Hecha |
| 8 (I-01) | Hecha |
| 9 (I-08) | Verificada |
| 10 Deudas System-1 | Hechas las dos. **Cota de entrada**: `ATHENA_SYSTEM1_MAX_INPUT_CHARS` (32 000 por defecto) para todo juicio; el broker no juzga recortes y da `INPUT_TOO_LARGE` hacia los 40 000, así que una entrada mayor no se envía: `system1.judged` con `INPUT_TOO_LARGE` y `fallback: true`, sin gastar la llamada; cada juicio registra `input_chars`. **Caso B**: `verification_failures_after_context_filtering` cuenta los fallos de verificación de runs a los que el filtro quitó contexto (sospecha medible, no causa). Documentado en `SYSTEM1_INTEGRATION.md` (`test_mejora10_*`) |

## Hallazgos nuevos durante las pruebas (corregidos)

| ID | Qué pasaba | Qué se hizo | Prueba |
| --- | --- | --- | --- |
| N-01 | **Agora→Athena (A03)**: el modelo escribió bien el entregable, se quedó reescribiéndolo igual y el broker cortó con `PROMPT_ECHOED` (error permanente). Athena dio el run por fallido sin mirar el disco, y Agora marcó como error un trabajo hecho | Un fallo del modelo (`ModelPermanentError` o transitorio agotado) después de haber escrito algo pasa por el mismo `_salvage` que el estancamiento y el fin de iteraciones: **una** verificación de lo que hay en disco, y solo se completa si la evidencia lo permite; el resultado dice que el modelo dejó de responder | `test_a03_*` (la primera falla sin el arreglo) |
| N-02 | Tras una caída (proceso muerto a mitad de run), el Historial mostraba el trabajo «En curso» y «Reanudar» nunca se activaba: la marca de interrumpido solo se aplicaba al lanzar otro run | `list_runs` marca antes de listar las sesiones cuyo proceso dueño ya no existe (la misma regla A22: nunca las de otra instancia viva) | `test_a_run_whose_process_died_is_resumable_as_soon_as_the_history_is_read` (falla sin el arreglo) |

## Evidencia (17/18-oct-2026)

**Puertas:** suite completa **1279 aprobadas** (21 nuevas), `ruff check` y `ruff format --check`
limpios, `mypy` estricto sin errores en 201 ficheros. Wheel
`build/desktop-wheel/athena_agent_runtime-0.3.1-py3-none-any.whl` regenerado y comprobado por
dentro; la ventana muestra «Athena Desktop v0.3.1» (también abierta con `iniciar-athena.bat`).

**Servicio en vivo** (puerto 8791, estado aislado, broker `192.168.1.52:8765`, `qwen3.8:27b`):

- Batería HTTP 8/8: health 200 sin token; run inexistente 404 en GET, `cancel`, `rollback` y
  `goal`; `DELETE /v1/runs` → 405 `Allow: GET, POST`; ack inexistente 404 con el mensaje nuevo.
- I-09, el escenario de la auditoría: el cliente ve la petición viva, se desconecta sin
  contestar, caduca a los 30 s; al reconectar con `Last-Event-ID` el replay la entrega con
  `awaiting_decision: false, stale: true`; contestarla da 404; el cliente sigue escuchando,
  llega la nueva petición viva, ack 200 + allow 200, archivo escrito, `agent.completed`.
- Aprobación remota normal: ack 200, allow 200, entregable escrito, `agent.completed`.

**Agora→Athena (A03)**: `Dispatcher` + `AthenaHarness` del repo de Agora contra el servicio:
tarjeta en `done/`, entregable de 5 líneas correcto. El run pasó por el rescate N-01
(`model.failed` → `verification.completed` → `agent.completed`). Antes del arreglo, el mismo
caso acabó `failed`. Dos problemas del **fixture del auditor**, no de Athena: el perfil anidaba
`athena:` bajo `metadata:` (Agora lo lee en el primer nivel) y `exec: off` sin comillas, que YAML
lee como booleano.

**Ventana con entrada real** (clics y teclas del sistema sobre coordenadas de Tk, LOCALAPPDATA
aislado, broker real):

- I-05: pregunta por clic en Iniciar → completada → Alt+F4 → proceso nuevo → clic en Historial:
  el trabajo está («Completado»).
- I-06: «Probar conexión» lista los 18 modelos aptos en el desplegable.
- I-08: «Iniciar servicio» → «Copiar» → el portapapeles tiene el token → «Detener».
- Documento con «Preguntar»: «Cambiar objetivo…» aplicado en marcha → diálogo de permiso con
  el contenido → «Permitir una vez» → «Terminado y comprobado», `nota.txt` con el objetivo
  **revisado** → «Deshacer cambios» lo elimina.
- Caída a mitad de run → ventana nueva → Historial «Interrumpido (se puede reanudar)» →
  «Reanudar» → terminado (tras N-02; antes no se podía).
- Cerrar con un trabajo en marcha (Alt+F4 + «Sí») lo cancela, como dice el diálogo.

Arneses: `ui_i05.py`, `ui_flows.py`, `live_service.py`, `agora_round.py` en el scratchpad de
la sesión.

# Resolución de la auditoría del 19/20-nov-2026 — Athena 0.3.8

Auditoría: `docs/Auditoria 20261119/` (informe `INFORME_AUDITORIA_2026-11-19.md`, revisión del
árbitro `REVISION_ARBITRO.md`, pruebas en `pruebas/` y evidencias en `evidencias/`). Hecha
sobre 0.3.7 (`7b723ae`). Corregida el 20-nov-2026 en **0.3.8**, sin commit: los commits los
hace el dueño.

Regresiones nuevas:
- `tests/test_auditoria_20261119.py`;
- `tests/test_agent_loop.py::test_a_tool_command_timeout_is_told_to_the_model_and_the_run_goes_on`;
- `tests/test_process_tools.py::test_an_interpreter_with_no_arguments_sees_end_of_input_and_exits`;
- `tests/test_graph_executor.py::test_a_running_task_is_persisted_as_running_before_it_works`.

Cada una se comprobó fallando sin su corrección: se retiraba el cambio con `git stash`, se
ejecutaba la prueba y se restauraba.

## Hallazgos

| ID | Qué pasaba | Qué se hizo | Prueba |
| --- | --- | --- | --- |
| H-09 | El plazo agotado de **un** comando del modelo (`process_timeout`) se trataba como fin de la sesión: run `failed` sin verificar, aunque el trabajo estaba hecho (R06 con el Broker real). En Windows, `python` a secas se colgaba porque `NUL` cuenta como terminal | `RecoveryPolicy.decide(..., from_tool=True)` desde `_record`: el plazo de una herramienta usa la rama `LIMITED_RETRY` que ya existía y se le cuenta al modelo. `classify_outcome` no cambia (sus dos pruebas siguen igual). `_spawn_process` en Windows da al hijo una tubería ya cerrada en vez de `NUL` | `test_a_tool_command_timeout_*`, `test_an_interpreter_*`; `h09_plazo_de_un_comando.py` sale con 0; R06 repetido con el Broker real (P6): `completed` |
| H-13 (nuevo, Broker real) | Un plan cuya tarea estaba trabajando cuando murió el proceso quedaba guardado como `pending`. Reanudar la repetía a ciegas (202), cuando lo documentado es negarse y nombrar la tarea | `GraphExecutor._run_task` guarda el grafo justo después de pasar la tarea a `RUNNING`, antes de que trabaje | `test_a_running_task_is_persisted_as_running_before_it_works`; `h13_plan_interrumpido.py`: 400 «…interrupted with an unknown outcome: whole» |
| H-10 | El escritorio mandaba el modelo como preferencia blanda: el Broker atendió con `gpt-oss-120b` (nube, FreeLLMapi) mientras la ventana enseñaba `qwen3.8:27b` | Un modelo escrito es una elección del run (`RunOptions.model` más un catálogo con ese modelo), estricta como en el servicio (ADR-034). Vacío = lo elige el Broker. Reanudar acepta también el modelo guardado en el manifiesto. Etiqueta: «Modelo (vacío: lo elige el broker)» | `test_h10_*` |
| H-05 | Reanudar desde Historial presentaba el resultado con el tipo de tarea del formulario («no se ha cambiado ningún archivo» encima de la lista de archivos cambiados) | `_resume_selected` fija el tipo guardado y no deja que el permiso del formulario impida reanudar (los permisos reales salen del manifiesto) | `test_h05_*`; `h05_reanudar.py` sale con 0 |
| H-01 | «Probar conexión» exigía el token aunque Core lo diera | La misma regla que `validate()`; el mensaje dice «El token lo da AI OS Core» | `test_h01_*`; recorrido de la ventana (U04) |
| H-04 | README y docstring decían que sin `ATHENA_CORE_URL` nada cambia; con Core parado el error no decía de dónde salía | README y docstring dicen que el fichero de conexión activa Core. El error nombra la URL, su procedencia y cómo arreglarlo. **La conducta no cambia** | `test_h04_*`; e2e C-implicito y C-implicito-caido |
| H-03 | Core con 200 que no es un objeto JSON → 500 | → 503 `core_unavailable`; un recibo nunca lanza | `test_h03_*`; e2e C-html y C-lista |
| H-02 | Bearer con caracteres no ASCII → 500 | Se compara en bytes → 401 | `test_h02_*`; e2e E04 |
| H-06 | «Configuración» decía «archivos: Preguntar» en una consulta | Dice «archivos: no (consulta)» | `test_h06_*` |
| H-07 | El cuadro del encargo parecía editable durante el trabajo. Ya estaba deshabilitado: el defecto era solo visual | Fondo gris mientras trabaja | `test_h06_*` (estado) y captura `correcciones/ventana/11_*` |
| H-08 | URL por defecto `http://localhost:8000` | `https://192.168.1.52:8765`, decisión del dueño del 20-oct para todo el ecosistema | `test_h08_*` |
| H-11 | «El proveedor del modelo no respondió» cuando respondió sin una decisión válida | Mensaje propio y sugerencia de cambiar de modelo | `test_h11_*` |
| R-04 | El motivo del fallo del Broker solo estaba en `details` | Va también en el mensaje («…: MODEL_ERROR: sin memoria») | `test_r04_*` |
| R-05 | `resume` dejaba la entrada de `_flows` si Core lo negaba | Se retira, como en `start` | inspección (camino sin observable externo) |
| CLI-1 (nuevo) | El ejemplo del README (`athena <repo> -o "Explain…"`) en un proyecto sin checks acababa `verification_inconclusive`, código 1 y **sin imprimir la respuesta** | Un run de solo lectura (`--writes off --exec off`, lo de por defecto) se verifica como consulta (`AnswerVerificationPolicy`), igual que el perfil `questions`. Si no se completa, imprime el motivo | `test_cli_una_consulta_*`; `cli_e2e.py` |
| CLI-2 (nuevo) | `--resume` con un id desconocido → traza de Python | Mensaje y código 2 | `test_cli_reanudar_*` |

## Corregidos en la segunda ronda (20-nov, tras «realiza las correcciones y pruebas que faltan»)

| ID | Qué pasaba | Qué se hizo | Prueba |
| --- | --- | --- | --- |
| R-01 | Repetir una `Idempotency-Key` con **otro** cuerpo devolvía en silencio el run del primero (Agora repetía claves con tarjetas de igual nombre) | La clave guarda la huella del cuerpo. Mismo cuerpo → replay como antes (200 `idempotent_replay`). Otro cuerpo → **422 `idempotency_key_reused`**. Un intento fallido retira también la huella. **Cambio de contrato**: un cliente que reutilice claves verá ahora el 422 en vez de un run equivocado | `test_service_adapter.py::test_the_same_key_with_another_body_is_refused` y las cuatro de idempotencia anteriores |
| H-12 | El catálogo solo contaba `available`; el Broker real marca `online` los modelos de LM Studio y de la nube (708 de 736) | Cuentan `available` y `online`, salvo `deployment: cloud`. Contra el catálogo real guardado: de 28 a 91 modelos locales (68 aptos); `gpt-oss-120b` (nube) sigue fuera; quien quiera uno de la nube lo escribe | `test_h12_*` |
| TG-1 (nuevo) | Con la concesión por defecto de Telegram (ejecución `off`), casi todo run acaba `verification_inconclusive`, y el canal lo anunciaba como «Athena stopped» | «Athena finished but could not verify the result: …», sin vestirlo de éxito (sigue siendo `ERROR`). Se ajustó el texto en `test_channels.py`, cuyo comentario pide justo que lleguen los tres hechos | `test_tg1_*`; `telegram_e2e.py` |
| A11Y-1 (nuevo) | El verde de éxito (`#1a7f37`) daba 4,46:1 sobre el fondo, por debajo de WCAG AA (4,5:1) | `#116329` en los cuatro sitios: 6,49:1 | `test_a11y_*`; `accesibilidad.py` |

## Sin corregir

| ID | Motivo |
| --- | --- |
| R-02 prioridad de cola en `ContextVar` | Correcto con el transporte actual (`Connection: close`). Solo importaría si se pasara a keep-alive; no hay daño que corregir hoy |
| R-03 ciclos de reparación tras una aprobación denegada | Diseño del bucle: tras un «no», el modelo puede buscar otra vía. Con el Broker real no se observó gasto anómalo; sin evidencia de daño no se cambia |

## Evidencia

- Puertas: ver el final de este documento.
- Recorridos con dobles, repetidos tras las correcciones (`evidencias/correcciones/`):
  - e2e del servicio: 29 casos, 0 en FALLA (2 observados sin regla);
  - H-05, H-09 y H-13 salen con 0;
  - ventana: H-01 «El token lo da AI OS Core», v0.3.8, «archivos: no (consulta)», encargo
    en gris.
- Con el Broker real (20-nov), en `evidencias/pendientes_real/`:
  - P1, aprobación remota por SSE como ChatyGPT (`ack` + permitir): PASA;
  - P6, R06 con H-09 corregido: PASA (`completed`, y aguantó dos cortes del Broker);
  - P5, catálogo crudo guardado (`broker_models.json`).
  - P2 (System-1), P3 (delegación) y P4 (plan): **BLOQUEADOS**. El Broker dejó de contestar
    (`TimeoutError`) a mitad del recorrido; P4 sí llegó a mostrar H-13 antes.
- Pestaña «Servicio para otras apps» (`evidencias/servicio_ventana/`): Iniciar → Copiar (el
  portapapeles tiene el token: 200 con él, 401 sin él) → Detener (puerto cerrado). PASA.
- CLI (`evidencias/cli2/`): consulta 0 con respuesta; `--writes ask` con «n» no escribe y con
  «y» escribe; `--list-sessions`; `--resume` desconocido → 2.
- Wheel `build/desktop-wheel/athena_agent_runtime-0.3.8-py3-none-any.whl` regenerado y
  comprobado por dentro (versión y las correcciones H-05, H-09, H-10 y CLI).

## Puertas (20-nov-2026, con todo el código final)

- `pytest`: **1315 aprobadas** (1295 de partida + 20 regresiones nuevas).
- `ruff check` y `ruff format --check` limpios en `src`, `tests` y la carpeta de la auditoría.
- `mypy` estricto sin errores en 206 ficheros.
- Wheel 0.3.8 regenerado tras la última corrección (H-13 incluida).

## Pruebas reales completadas tras el reinicio del Broker (20-nov, ~03:45)

El Broker se colgó hacia las 03:00 (ping al PC IA bien; 8765 sin respuesta) y el dueño lo
reinició. Justo después rechazaba el token anterior (`model_authentication_required`):
Athena lo dijo así y no reintentó a ciegas, y en cuanto Core republicó el token nuevo todo
volvió. Resultados (`docs/Auditoria 20261119/evidencias/pendientes_real3/` y `ventana_real3/`):

| ID | Resultado |
| --- | --- |
| P4 plan interrumpido | **PASA**. Reanudar se niega con 400 y nombra la tarea `T1` de un plan real de varias tareas. Confirma H-13 con el Broker real |
| H-10 en la ventana | **PASA**. Los dos trabajos del escritorio los atendió `qwen3.8:27b`, el modelo elegido; antes, `gpt-oss-120b` |
| P2 System-1 en sombra | **PASA** para Athena: eventos `system1.*`, alternativa conservadora y run `completed`. El juez del Broker (`laya_mcp`, `multilingual`) respondió `INPUT_TOO_LARGE` con una entrada de 1336 caracteres: es el mismo límite del Broker que ya se vio en Model_Drift (B-5), no un defecto de Athena |
| P3 delegación | **OBSERVADO**. `delegate_task` se ofrece en todo run (`_build` → `_delegation_tool`, fuera del filtro del perfil); `qwen3.8:27b` contestó sin delegar, y el run quedó `completed`. Los delegados sí corrieron con el Broker real dentro de los planes (R08, P4: sub-agente `coder` por tarea) |

Con esto no queda ninguna prueba bloqueada. Lo que sigue sin cubrir es lo declarado:
Telegram (requiere un bot y su token), MCP (no tiene transporte propio: NO APLICA), y
accesibilidad medida (Tk 8.6 no expone UIA).

## Pruebas que faltaban, ejecutadas (20-nov, segunda ronda)

| Prueba | Cómo | Resultado |
| --- | --- | --- |
| Telegram (`evidencias/telegram/`) | `athena_telegram.serve` real, con `TelegramApi` apuntando a un doble HTTP de la Bot API y el Broker falso | T1: un usuario permitido → run creado, «Started» y final «finished but could not verify», **PASA**. T2: un desconocido → «not authorised», sin run, **PASA**. T3: sin lista de permitidos el bot no arranca, **PASA**. El canal no reenvía la respuesta del modelo, por diseño (`render_event`) |
| MCP (`evidencias/mcp/`) | `mcp_tools` real en un `AgentLoop` real, con un `McpClient` en proceso (Athena no trae transporte MCP, ADR-014) | M1: R3 pregunta y, permitido, se ejecuta y el modelo lee el resultado. M2: denegado, no se llama al servidor. M3: servidor colgado → `process_timeout` contado al modelo, run sigue (confirma H-09 también en MCP). **PASAN** |
| Accesibilidad (`evidencias/accesibilidad/`) | Ventana real | Ctrl+Intro empieza; Escape en el permiso deniega y no escribe; con Tab se llega a Elegir…, Probar conexión, Iniciar y Limpiar; contraste WCAG ≥ 4,5:1 en todos los estilos de texto tras A11Y-1. **PASAN**. Un lector de pantalla no se puede medir: Tk 8.6 no expone UI Automation |

## Puertas finales (20-nov-2026, tras la segunda ronda)

- `pytest`: **1319 aprobadas** (1295 de partida + 24 regresiones nuevas).
- `ruff check` y `ruff format --check` limpios (225 ficheros, incluida la carpeta de la
  auditoría); `mypy` estricto sin errores en 206 ficheros.
- Wheel 0.3.8 regenerado y comprobado por dentro (R-01, H-12, TG-1, A11Y-1, H-13, H-09,
  H-10, H-05, CLI).

## Con piezas reales, no dobles (20-nov, cierre)

| Prueba | Resultado |
| --- | --- |
| MCP con un servidor **real de terceros** (`evidencias/mcp_real/`) | `tradingview-mcp` 2.0.0 por stdio, con un `McpClient` mínimo (JSON-RPC: `initialize`, `tools/list`, `tools/call`). `mcp_tools` adaptó sus **84** herramientas. El bucle real pidió `pine_analyze`: permiso R3 preguntado, llamada real al servidor, y el modelo recibió `success: true, issue_count: 0`; run `completed`. **PASA** |
| Lector de pantalla (`evidencias/accesibilidad/uia_lector.json`) | Medido con UI Automation, lo que leen Narrador y NVDA: la ventana expone **75 elementos «Pane» y ninguno con nombre**. **A11Y-2 confirmado**: el escritorio no se puede usar con un lector de pantalla |
| Telegram con la Bot API real | **Pendiente por decisión del dueño** (20-nov): aún no tiene bot preparado. Probado solo con un doble de la Bot API (T1-T3 PASAN). Para repetirlo con el real: `ATHENA_TELEGRAM_TOKEN_FILE` con el token, `ATHENA_TELEGRAM_ALLOWED_IDS` con su id, `ATHENA_TELEGRAM_WORKSPACES=<id>:<carpeta>`, Broker real, `python -m athena_telegram` y escribirle al bot |

**A11Y-2, qué se hizo y qué no.** Tk 8.6 no tiene API de accesibilidad, así que no se puede
arreglar sin cambiar de toolkit. Es un cambio estructural que queda **propuesto, con la
medición como justificación, y no hecho**. Hoy:
- el README lo dice;
- dice también qué funciona: teclado y contraste;
- remite al CLI, que imprime texto plano en la consola y que esta ronda dejó funcionando
  para consultas.

Que Narrador lea bien la consola es lo normal en Windows, pero **no se midió**.

# Escenarios de aceptación

Este catálogo tiene dos mitades y la distinción entre ellas es lo primero que hay que
leer:

- **MASTER_E2E** — los diez escenarios acordados en el prompt maestro. Vienen del
  encargo.
- **DERIVED / REGRESSION** — escenarios descubiertos durante la implementación. Cubren
  defectos reales y propiedades que alguien podría creerse mal, pero **no tienen la
  autoridad del encargo original** y no se presentan como si la tuvieran.

Los derivados **no se sustituyen** por los originales. Un escenario como «un `LiveRun`
huérfano», «credencial válida no es lo mismo que `/health` responde» o «una aprobación
caduca no sobrevive al reinicio» sale de haber roto algo de verdad, y por eso se queda
para siempre: **la suite de aceptación crece por descubrimiento, no repitiendo el prompt
inicial.**

```
Acceptance Scenarios
│
├── MASTER E2E                       (Origin: MASTER_PROMPT)
│   ├── E2E-01  Simple Direct
│   ├── E2E-02  Hierarchical
│   ├── E2E-03  Forced One-Node Graph
│   ├── E2E-04  Auto One-Node Plan
│   ├── E2E-05  Continuable Explorer
│   ├── E2E-06  Non-Developer Profile
│   ├── E2E-07  Capability / Visibility / Authority
│   ├── E2E-08  Multi-Channel Race
│   ├── E2E-09  Provider Fallback
│   └── E2E-10  Crash Recovery
│
└── DERIVED / REGRESSION SCENARIOS
    ├── DER-01  Un respaldo que no cumple no es un respaldo
    ├── DER-02  Lo que hizo un run sobrevive al proceso, con su autor
    ├── DER-03  Una tool cumple lo que declara
    ├── DER-04  No haber podido comprobar no es haber fallado
    ├── DER-05  Athena termina un encargo sin tests que pasar
    ├── DER-06  Revisar el encargo no hereda la evidencia vieja
    ├── DER-07  Un delegado contesta dos veces sin renovar su presupuesto
    ├── DER-08  Athena aprende de la evidencia, y lo viejo se nota
    ├── DER-09  Un chat escribe pero no ejecuta
    ├── DER-10  Deshacer toca lo del run y respeta lo ajeno
    ├── REG-01  Un delegado publica y nadie lo recibe
    ├── REG-02  Credencial válida ≠ `/health` responde
    ├── REG-03  Una aprobación no sobrevive al proceso
    ├── REG-04  Un `LiveRun` que ya no existe no se cuenta como vivo
    ├── REG-05  Un run sin historia no es un run vacío
    ├── REG-06  Un reintento no es una segunda petición
    ├── REG-07  `read_range` decía devolver cadenas y devuelve objetos
    ├── REG-08  `delegate_task` moría siempre por el reloj de quien llamaba
    ├── REG-09  Una delegación que fue bien devolvía «(sin resultados)»
    ├── REG-10  El bytecode obsoleto invalidaba la verificación
    ├── REG-11  El redactor del bus tachaba los contadores de tokens
    ├── REG-12  El estado operativo registraba intención, no hecho
    └── SEC-01  Descubrir una tool no la autoriza
```

Cada escenario lleva:

| Campo | Qué dice |
| --- | --- |
| **ID** | Identificador estable |
| **Origin** | `MASTER_PROMPT`, `IMPLEMENTATION_DISCOVERY`, `REGRESSION` o `SECURITY` |
| **Purpose** | Qué afirmación defiende |
| **Preconditions** | Qué tiene que ser cierto antes |
| **Steps** | La secuencia |
| **Expected result** | Cómo debe acabar |
| **Assertions** | Qué se comprueba, no qué se observa |
| **Automated tests** | Dónde vive, con nombre de fichero y de test |
| **Status** | `PASS`, `PARTIAL` (con el hueco nombrado) o `NOT COVERED` |

---

## MASTER E2E

### E2E-01 — Simple Direct

- **Origin**: `MASTER_PROMPT`
- **Purpose**: un objetivo sencillo con `execution_mode=AUTO` se ejecuta de una pieza, y
  **no se paga un TaskGraph que no aporta nada**. Comprobar sólo que el run acaba bien
  dejaría pasar una versión que planifica, monta el ejecutor de grafos y ejecuta una sola
  tarea: mismo resultado, una llamada al modelo y un ejecutor entero de más.
- **Preconditions**: un repositorio con checks propios que pasan; despliegue con
  planificación **encendida** (si estuviera apagada, no descomponer no probaría nada).
- **Steps**: `POST /v1/runs` con `execution_mode=auto` → `DecompositionPolicy` responde →
  `AgentLoop` → tools → verificación.
- **Expected result**: run `completed`, verificación `passed`.
- **Assertions**:
  - `plan.decided` dice `executed_as=direct` y `execution_mode=auto`;
  - **no** se publica `graph.started` ni `task.started`;
  - el planificador no llegó a llamarse ni una vez.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_01_un_objetivo_simple_se_hace_de_una_pieza_y_sin_grafo`
  - `tests/test_acceptance_hierarchical.py::test_a_simple_goal_never_reaches_the_graph`
  - `tests/test_service_orchestration.py::test_one_verifiable_output_stays_on_the_loop`
- **Status**: **PASS**

### E2E-02 — Hierarchical

- **Origin**: `MASTER_PROMPT`
- **Purpose**: el encargo completo —analizar un repositorio, encontrar la causa,
  corregirla, adaptar las pruebas y demostrar que funciona— recorrido de punta a punta:
  Goal → política → Planner → TaskGraph → GraphExecutor → Explorer → evidencia → Coder →
  resultados canónicos → Verifier → fallo → diagnóstico → reparación → verificación PASS
  → verificación del objetivo → completado.
- **Preconditions**: repositorio git real con un fallo real y un comando de verificación
  ejecutable; planificación encendida.
- **Steps**: los del encargo, sobre un solo workspace.
- **Expected result**: el objetivo se da por cumplido **por su propia verificación**, no
  porque las tareas estén de acuerdo entre sí.
- **Assertions**: dependencias respetadas; permisos pedidos a través del run que posee la
  tarea; capacidades exigidas antes de gastar una llamada; autoridad del hijo ⊆ la del
  padre; `RunEventLog` con procedencia; contexto con su origen; métricas comparables;
  cancelación que alcanza todos los niveles.
- **Automated tests**:
  - `tests/test_acceptance_hierarchical.py::test_a_complex_goal_is_planned_delegated_executed_and_proved`
  - `…::test_the_goal_is_not_granted_by_the_tasks_agreeing`
  - `…::test_a_failing_verification_is_diagnosed_before_anyone_is_asked_to_fix_it`
  - `…::test_a_child_can_never_do_more_than_its_parent`
  - `…::test_stopping_a_run_reaches_every_level`
  - `…::test_a_model_plan_with_a_cycle_never_becomes_a_run`
  - `…::test_a_run_leaves_enough_behind_to_be_compared`
  - `tests/test_service_orchestration.py::test_a_task_asks_for_permission_through_the_run_that_owns_it`
  - `tests/test_acceptance_deepseek.py::test_2_lo_que_hizo_un_run_sobrevive_al_proceso_con_su_autor`
- **Status**: **PASS** (repartido entre varios tests; no hay uno solo que lo recorra
  entero, y eso es deliberado: el fixture completo tarda y cada propiedad se rompe por su
  cuenta)

### E2E-03 — Forced One-Node Graph

- **Origin**: `MASTER_PROMPT`
- **Purpose**: **validez estructural y conveniencia de descomponer son conceptos
  separados.** `TaskGraph.build` responde a «¿es esto un plan?»; `DecompositionPolicy`, a
  «¿merece la pena?». Juntarlas sale caro en las dos direcciones.
- **Preconditions**: `execution_mode=hierarchical`; el Planner devuelve una única tarea
  válida.
- **Steps**: construir el grafo de un nodo; pedir el run en modo jerárquico.
- **Expected result**: `TaskGraph.build()` **acepta**, y el `GraphExecutor` lo ejecuta.
- **Assertions**: el grafo tiene un nodo y su frontera inicial lo contiene; el run publica
  `graph.started` y `task.started`; `plan.decided` dice `executed_as=hierarchical`.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_03_un_grafo_de_un_nodo_es_valido_aunque_no_convenga`
  - `tests/test_acceptance_master_e2e.py::test_e2e_03_pedir_jerarquico_ejecuta_el_grafo_de_un_nodo`
  - `tests/test_service_orchestration.py::test_hierarchical_runs_a_single_task_plan_through_the_graph`
- **Status**: **PASS**

### E2E-04 — Auto One-Node Plan

- **Origin**: `MASTER_PROMPT`
- **Purpose**: con `AUTO`, un plan de una sola tarea no justifica el coste del ejecutor de
  grafos.
- **Preconditions**: planificación encendida; el Planner propone una tarea sin beneficio
  jerárquico.
- **Steps**: `POST /v1/runs` con `execution_mode=auto`.
- **Expected result**: se ejecuta en el bucle.
- **Assertions**: `plan.decided` con `executed_as=direct` y `reason_code` que lo explica
  (`plan_not_worthwhile`); no hay eventos de grafo.
- **Automated tests**:
  - `tests/test_service_orchestration.py::test_auto_runs_a_single_task_plan_on_the_loop`
  - `…::test_one_task_is_a_plan_that_buys_nothing`
  - `…::test_a_chain_of_microtasks_is_a_to_do_list_not_a_graph`
- **Status**: **PASS**

### E2E-05 — Continuable Explorer

- **Origin**: `MASTER_PROMPT`
- **Purpose**: reutilizar un Explorer —tarea A, informe, seguimiento B, informe,
  liquidación— **sin transferirle al padre su transcript**. Si el transcript subiera,
  delegar dejaría de ahorrar contexto y sólo añadiría latencia.
- **Preconditions**: perfil Explorer (el único continuable por defecto) con
  `max_follow_ups > 0`.
- **Steps**: delegar; recibir informe; `follow_up` sobre el mismo id; recibir informe.
- **Expected result**: el mismo delegado contesta dos veces dentro de un presupuesto
  compartido.
- **Assertions**:
  - la sesión del seguimiento es la del primer encargo;
  - lo que hizo el hijo se publica bajo **su** sesión;
  - en el ámbito del padre sólo constan `subagent.*` —el ciclo de vida—, nunca lo que el
    hijo leyó ni su respuesta en bruto;
  - el informe llega al padre como valor de vuelta, que es un resumen.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_05_al_padre_no_le_llega_el_transcript_del_delegado`
  - `tests/test_continuable_subagents.py::test_un_seguimiento_es_el_mismo_delegado_y_no_uno_nuevo`
  - `…::test_el_delegado_recibe_lo_que_ya_habia_averiguado`
  - `…::test_el_presupuesto_es_del_delegado_y_no_de_cada_pregunta`
  - `…::test_no_se_puede_preguntar_indefinidamente`
- **Status**: **PASS**

### E2E-06 — Non-Developer Profile

- **Origin**: `MASTER_PROMPT`
- **Purpose**: **el núcleo de Athena es independiente del dominio.** Un perfil
  no-desarrollador que conservara `bash` o git no demostraría nada.
- **Preconditions**: fixture `financial_analyst` —una carpeta de estados financieros y
  notas— **sin git, sin código fuente, sin pytest y sin estado propio de desarrollador**.
- **Steps**: crear el run con ese perfil y un entregable declarado; leer; escribir el
  entregable; verificar.
- **Expected result**: run `completed` y verificación `passed` **por artefactos**. Un
  dominio sin comandos ejecutables no puede ser un dominio donde siempre falla.
- **Assertions**: el entregable existe y no está vacío; la carpeta sigue sin `.git`;
  ni `bash` ni `git_commit` aparecen nunca ante el modelo (filtro **estructural** del
  perfil, no una denegación).
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_06_athena_trabaja_en_un_dominio_sin_nada_de_desarrollador`
    (define `FINANCIAL_ANALYST` como fixture del propio test)
  - `tests/test_acceptance_deepseek.py::test_5_athena_termina_un_encargo_sin_tests_que_pasar`
  - `tests/test_service_adapter.py::test_a_profile_decides_which_tools_a_run_can_even_name`
- **Status**: **PASS**
- **Nota honesta**: `financial_analyst` **no es un perfil que Athena ofrezca como
  producto**: el despliegue registra `software_engineering` y `documents`. El fixture se
  construye en el test porque lo que el escenario demuestra es que el núcleo no necesita
  nada de desarrollador, y para eso basta con que el perfil exista ahí. Registrarlo por
  defecto sólo para que la prueba pasara sería fabricar el sujeto de la prueba.

### E2E-07 — Capability / Visibility / Authority

- **Origin**: `MASTER_PROMPT`
- **Purpose**: **Capability ≠ Visibility ≠ Authority.** Confundirlas produce dos errores
  simétricos y los dos malos: creer que ocultar algo lo protege, y creer que enseñarlo lo
  autoriza.
- **Preconditions**: un registro de tools montable a voluntad y motores de permisos con
  políticas distintas.
- **Steps**: recorrer la matriz fila por fila.

  | Capability | Visible | Authority | Resultado esperado |
  | --- | --- | --- | --- |
  | NO | — | — | `UnsupportedCapability` |
  | YES | NO | YES | no accesible directamente al modelo |
  | YES | YES | DENY | bloqueado |
  | YES | YES | ASK | espera aprobación |
  | YES | YES | ALLOW | ejecuta |
  | YES | DEFERRED | ALLOW | descubrir → autorizar → ejecutar |
  | YES | YES | padre ALLOW / hijo DENY | hijo bloqueado |

- **Expected result**: las siete filas se comportan como dice la tabla.
- **Assertions**: pedir una tool no montada es un error de validación, no una denegación;
  una tool diferida no aparece en los esquemas del turno pero sí en `names()`; descubrirla
  la hace visible y **no** la autoriza; la autoridad del hijo es la intersección con la
  del padre, calculada como aritmética y no comprobada como regla.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_07_capability_visibility_y_authority_son_tres_preguntas`
  - `tests/test_visibility_and_authority.py` (8 pruebas hostiles)
  - `tests/test_capabilities.py::test_the_provider_is_never_called_when_a_required_guarantee_is_missing`
- **Status**: **PASS**

### E2E-08 — Multi-Channel Race

- **Origin**: `MASTER_PROMPT`
- **Purpose**: dos canales sobre el mismo run no se pisan sin enterarse, y **el conflicto
  es útil en vez de un callejón**: quien llega tarde puede releer, decidir con el objetivo
  nuevo delante y volver a escribir.
- **Preconditions**: un run vivo con el encargo en revisión N.
- **Steps**: Telegram revisa (N → N+1); ChatyGPT intenta mutar con N; ChatyGPT relee y
  vuelve a escribir sobre N+1.
- **Expected result**: la segunda escritura recibe `goal_conflict` (el
  `StaleRevisionError` del encargo) con el objetivo actual dentro; la tercera se acepta.
- **Assertions**: el conflicto trae `current_revision` y `current`; **nada se fusiona y
  nada se pisa**; el objetivo tras el conflicto sigue siendo el de Telegram; la revisión
  siguiente sale de la vigente y llega a N+2.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_08_tras_el_conflicto_se_puede_escribir_sobre_la_revision_nueva`
  - `tests/test_service_adapter.py::test_a_stale_revision_is_a_conflict_that_hands_back_the_current_goal`
  - `…::test_revising_without_saying_which_version_is_refused`
  - Lado cliente: `apps/desktop/src-tauri/src/athena/pruebas.rs::un_conflicto_de_revision_es_una_respuesta_y_no_un_error`,
    `…::si_la_relectura_falla_se_contesta_con_lo_que_traia_el_conflicto`,
    `apps/desktop/src/AthenaEncargo.test.tsx` (6 pruebas: no reintenta solo, «escrito» no
    es «aplicado», repetir es una decisión)
- **Status**: **PASS**
- **Nota honesta**: la carrera se ejercita con **dos escrituras sobre el mismo
  `RunRegistry`**, no arrancando el canal de Telegram. Es la misma superficie —Telegram
  comparte el registro del servicio a propósito— pero decir «probado con Telegram» sería
  decir más de lo que se hizo.

### E2E-09 — Provider Fallback

- **Origin**: `MASTER_PROMPT`
- **Purpose**: un respaldo que no da la garantía pedida **no es un respaldo**. Caer a él
  sería degradar con otro nombre, y en silencio.
- **Preconditions**: un router con un primario que falla y respaldos declarados.
- **Steps**: el primario falla; se evalúan los respaldos contra lo que la petición exige.
- **Expected result**: un respaldo compatible continúa la ejecución; uno que no satisface
  las capacidades REQUIRED **falla ruidosamente**. Nunca degradación silenciosa.
- **Assertions**: las capacidades se comprueban **antes** de gastar una llamada; el cambio
  queda registrado con de quién a quién y por qué; una capacidad preferida que falta se
  anuncia y no bloquea.
- **Automated tests**:
  - `tests/test_acceptance_deepseek.py::test_1_un_respaldo_que_no_cumple_no_es_un_respaldo`
  - `tests/test_service_orchestration.py::test_a_required_capability_fails_loud_and_an_optional_one_falls_back`
  - `tests/test_capabilities.py::test_a_missing_preferred_capability_does_not_block`
  - `…::test_requiring_says_which_guarantee_was_missing`
- **Status**: **PASS**

### E2E-10 — Crash Recovery

- **Origin**: `MASTER_PROMPT`
- **Purpose**: tras una caída, lo que sobrevive es coherente y lo que no debe sobrevivir,
  no sobrevive.
- **Preconditions**: estado persistido de runs en distintos momentos.
- **Steps**: provocar la caída durante ejecución de grafo, subagente, permiso pendiente y
  verificación; reiniciar.
- **Expected result**: estado duradero correcto; activaciones locales del proceso **no**
  aparecen falsamente vivas; recuperación coherente; aprobaciones caducas invalidadas;
  sin procesos huérfanos.
- **Assertions**:
  - un run vivo en cualquiera de esos estados queda `recovery_pending`, nunca `completed`
    ni `failed`;
  - un run terminado **no** se reabre al reiniciar;
  - `live_ids()` sale vacío aunque haya trabajo por recuperar;
  - un `request_id` de antes del reinicio no existe en el registro nuevo;
  - matar un proceso de fondo no deja nietos huérfanos.
- **Automated tests**:
  - `tests/test_acceptance_master_e2e.py::test_e2e_10_un_run_vivo_al_caerse_queda_por_recuperar_y_no_terminado` (3 estados)
  - `…::test_e2e_10_un_run_terminado_no_se_reabre_al_reiniciar`
  - `…::test_e2e_10_lo_que_solo_vivia_en_el_proceso_no_sobrevive_a_el`
  - `…::test_e2e_10_una_aprobacion_de_antes_del_reinicio_ya_no_vale`
  - `tests/test_service_adapter.py::test_recovery_pending_runs_are_listed_after_a_restart`
  - `tests/test_service_orchestration.py::test_a_plan_stopped_between_tasks_carries_on_where_it_stopped`
  - `…::test_a_plan_interrupted_mid_task_is_not_resumed_on_a_guess`
  - `tests/test_tasks.py::test_killing_a_background_process_leaves_no_orphan`
  - `tests/test_process_tools.py::test_cancellation_leaves_no_orphan_grandchild`
- **Status**: **PARTIAL**
- **Hueco nombrado**: la caída se simula **por el estado persistido**, no matando el
  proceso a mitad de una tarea real. Lo que se prueba es que el estado que sobrevive se
  interpreta bien; lo que **no** se prueba es que una caída dura en ese instante deje el
  disco en un estado legible. Cerrarlo pide un arnés que arranque el servicio en un
  proceso aparte y lo mate; el disparador para escribirlo sería una corrupción observada,
  y todavía no se ha observado ninguna.

---

## DERIVED / REGRESSION SCENARIOS

Estos **no vienen del prompt maestro**. Se conservan porque cada uno defiende algo que se
rompió de verdad o que alguien podría creerse mal.

### Derivados de la integración (Origin: `IMPLEMENTATION_DISCOVERY`)

Los diez de `tests/test_acceptance_deepseek.py`. Criterio de selección: **una frase que,
si se rompe, hace que Athena mienta en vez de fallar.**

| ID | Escenario | Test | Status |
| --- | --- | --- | --- |
| DER-01 | Un respaldo que no cumple no es un respaldo | `test_1_…` | PASS |
| DER-02 | Lo que hizo un run sobrevive al proceso, con su autor | `test_2_…` | PASS |
| DER-03 | Una tool cumple lo que declara y se explica al modelo | `test_3_…` | PASS |
| DER-04 | No haber podido comprobar no es haber fallado | `test_4_…` | PASS |
| DER-05 | Athena termina un encargo sin tests que pasar | `test_5_…` | PASS |
| DER-06 | Revisar el encargo no hereda la evidencia vieja | `test_6_…` | PASS |
| DER-07 | Un delegado contesta dos veces sin renovar su presupuesto | `test_7_…` | PASS |
| DER-08 | Athena aprende de la evidencia, y lo viejo se nota | `test_8_…` | PASS |
| DER-09 | Un chat escribe pero no ejecuta | `test_9_…` | PASS |
| DER-10 | Deshacer toca lo del run y respeta lo ajeno | `test_10_…` | PASS |

Los que además se comprobaron contra el broker real lo dicen en su propio docstring:
«probado con un modelo de mentira» y «probado con uno de verdad» no son la misma
afirmación.

### REG-01 — Un delegado publica y nadie lo recibe

- **Origin**: `REGRESSION`
- **Purpose**: en un run jerárquico las tareas publican con el id de la tarea y los
  delegados con el suyo. El fan-out entregaba **sólo** lo publicado con el id del run, así
  que `subagent.started`, `subagent.completed` y todo lo que hacía un delegado se
  publicaba bien y **no llegaba a nadie**. Es la enfermedad recurrente del proyecto:
  subsistemas construidos, probados, exportados y conectados a nada.
- **Steps**: `task.started` en el ámbito del run; `subagent.started` en el de la tarea;
  una tool en el del delegado.
- **Assertions**: los tres llegan al cliente suscrito al run; una sesión que nadie
  reclamó **no** se adopta por cercanía.
- **Automated tests**:
  `tests/test_service_adapter.py::test_what_a_delegate_does_reaches_the_client_watching_the_run`,
  `…::test_a_session_nobody_claimed_does_not_borrow_a_run`
- **Status**: **PASS** (corregido en esta tanda)

### REG-02 — Credencial válida ≠ `/health` responde

- **Origin**: `REGRESSION`
- **Purpose**: `/v1/health` es público a propósito. Un cliente que dedujera de su 200 que
  está autenticado se anunciaría «conectado» mientras todo lo demás le devuelve 401. Se
  reprodujo contra el servicio real.
- **Assertions**: con `/health` 200 y `/auth/check` 401 el estado es `credencial_invalida`
  y **sigue constando** que hay credencial guardada: lo que falla es que valga.
- **Automated tests**:
  `apps/desktop/src-tauri/src/athena/area.rs::pruebas::salud_200_con_credencial_invalida_no_es_estar_conectado`
- **Status**: **PASS**

### REG-03 — Una aprobación no sobrevive al proceso

- **Origin**: `SECURITY`
- **Purpose**: las peticiones de permiso viven sólo en memoria, y eso es la garantía, no un
  descuido: una respuesta guardada en disco autorizaría tras un reinicio una acción que
  nadie ha vuelto a plantear, sobre un workspace que puede haber cambiado.
- **Automated tests**:
  `tests/test_acceptance_master_e2e.py::test_e2e_10_una_aprobacion_de_antes_del_reinicio_ya_no_vale`
- **Status**: **PASS** (también es una fila de E2E-10; se cataloga aparte porque la
  propiedad es de seguridad y sobrevive aunque E2E-10 cambie)

### REG-04 — Un `LiveRun` que ya no existe no se cuenta como vivo

- **Origin**: `REGRESSION`
- **Purpose**: «hay algo que decidir» y «hay algo corriendo» son cosas distintas.
  Confundirlas hace que un cliente espere eventos de un bucle que ya no existe.
- **Automated tests**:
  `tests/test_acceptance_master_e2e.py::test_e2e_10_lo_que_solo_vivia_en_el_proceso_no_sobrevive_a_el`
- **Status**: **PASS**

### REG-05 — Un run sin historia no es un run vacío

- **Origin**: `REGRESSION`
- **Purpose**: devolver 200 con una lista vacía haría pasar la **ausencia** de historia por
  historia completa.
- **Automated tests**:
  `tests/test_service_adapter.py::test_a_deployment_without_a_log_says_so_instead_of_inventing_a_history`,
  `apps/desktop/src-tauri/src/athena/area.rs::pruebas::un_run_del_que_no_consta_historia_no_se_enseña_como_run_vacio`,
  `apps/desktop/src/AthenaHistorial.test.tsx`
- **Status**: **PASS**

### REG-06 — Un reintento no es una segunda petición

- **Origin**: `REGRESSION`
- **Purpose**: dos agentes sobre un workspace es exactamente el resultado que un cliente
  reintentando un POST caído intenta evitar.
- **Automated tests**: `tests/test_service_adapter.py::test_a_repeated_create_run_makes_one_run`,
  `…::test_two_concurrent_retries_of_one_key_still_make_one_run`,
  `…::test_a_failed_create_does_not_poison_the_key`
- **Status**: **PASS**

### REG-07 — `read_range` decía devolver cadenas y devuelve objetos

- **Origin**: `REGRESSION`
- **Purpose**: 835 pruebas verdes preguntaban qué hace cada tool; ninguna preguntaba si eso
  es lo que dijo que haría. Lo encontró un run real.
- **Automated tests**: `tests/test_tool_output_schemas.py` (ejecuta cada tool contra su
  esquema; añadir una tool y no meterla ahí rompe la suite)
- **Status**: **PASS**

### REG-08 — `delegate_task` moría siempre por el reloj de quien llamaba

- **Origin**: `REGRESSION`
- **Purpose**: `ToolExecutor` aplicaba un techo único de 30 s a toda tool, y una delegación
  es un bucle entero. El fallo se atribuía al delegado. Los proveedores guionizados
  responden al instante, así que la suite estaba verde.
- **Automated tests**: cubierto por `ToolSpec.timeout_seconds` y
  `tests/test_continuable_subagents.py::test_el_modelo_puede_pedir_un_seguimiento_por_su_nombre`
- **Status**: **PASS**

### REG-09 — Una delegación que fue bien devolvía «(sin resultados)»

- **Origin**: `REGRESSION`
- **Purpose**: la proyección por defecto coge la primera lista del resultado
  —`files_changed`, vacía en un explorer— como lo enumerable. Regla que dejó: toda tool
  cuyo resultado tenga listas accesorias necesita su propia `project()`.
- **Automated tests**:
  `tests/test_continuable_subagents.py::test_al_modelo_no_se_le_devuelve_vacio_una_delegacion_que_fue_bien`
- **Status**: **PASS**

### REG-10 — El bytecode obsoleto invalidaba la verificación

- **Origin**: `REGRESSION`
- **Purpose**: la cabecera del `.pyc` guarda el mtime truncado a segundos; dos ediciones en
  el mismo segundo hacían que pytest juzgara la versión anterior del código. Se manifestaba
  como un test intermitente.
- **Automated tests**: `tests/test_verification_policy.py::test_verification_leaves_no_bytecode_cache_to_go_stale`
- **Status**: **PASS**

### REG-11 — El redactor del bus tachaba los contadores de tokens

- **Origin**: `REGRESSION`
- **Purpose**: `redact_sensitive` emparejaba por nombre de clave contra `token`, así que
  `input_tokens` llegaba `[REDACTED]` y se contaba como cero — un cero que parece dato.
  Ningún secreto es un entero suelto.
- **Automated tests**: `tests/test_metrics.py::test_a_token_count_is_not_a_token`,
  `…::test_the_loop_delivers_token_counts_all_the_way_to_the_metrics`
- **Status**: **PASS**

### REG-12 — El estado operativo registraba intención, no hecho

- **Origin**: `REGRESSION`
- **Purpose**: `_record_tool_use` corría **antes** de ejecutar, así que una escritura
  denegada aparecía igualmente en `files_modified`. Eso alimenta la atribución de la
  verificación, la recuperación y lo que lee una persona tras un fallo.
- **Automated tests**: `tests/test_repair_and_recovery.py::test_a_refused_tool_call_is_not_recorded_as_work_done`
- **Status**: **PASS**

### SEC-01 — Descubrir una tool no la autoriza

- **Origin**: `SECURITY`
- **Purpose**: es la trampa del catálogo diferido: si descubrir concediera permiso,
  bastaría con buscar para escalar, y la carga diferida —que existe para no gastar
  contexto— se convertiría en un agujero.
- **Automated tests**: `tests/test_visibility_and_authority.py::test_discovering_a_tool_does_not_authorise_it`,
  `…::test_an_mcp_server_announcing_a_tool_grants_nothing`, y la fila `DEFERRED` de E2E-07
- **Status**: **PASS**

---

## Cómo se corre esto

```
cd "D:/Desarrollo/Proyectos TFM/Athena"
./.venv/Scripts/python.exe -m pytest tests/test_acceptance_master_e2e.py -q     # MASTER
./.venv/Scripts/python.exe -m pytest tests/test_acceptance_deepseek.py -q       # DERIVED
./.venv/Scripts/python.exe -m pytest -q                                          # todo
```

Lado cliente (ChatyGPT):

```
cd "D:/Desarrollo/Proyectos TFM/ChatyGPT"
cargo test --lib --manifest-path apps/desktop/src-tauri/Cargo.toml
./node_modules/.bin/vitest.CMD run
python -m unittest discover -s tests
```

## Cómo crece este catálogo

Un escenario entra aquí cuando **defiende una afirmación que alguien podría creerse mal**,
no cuando añade cobertura. Al añadirlo:

1. se le pone `Origin`, y si no viene del prompt maestro **no se le atribuye**;
2. se escribe qué se comprueba, no qué se observa;
3. si sólo está cubierto a medias, el `Status` dice `PARTIAL` **y nombra el hueco**.

Un escenario derivado no se borra porque los originales ya cubran «lo mismo»: lo que un
derivado defiende suele ser el caso concreto que rompió algo, y ese caso no está en
ninguna lista escrita de antemano.

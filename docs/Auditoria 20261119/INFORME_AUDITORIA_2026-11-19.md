# Auditoría integral de Athena — 19-nov-2026

Instrucciones: `Instrcciones.md` (misma carpeta). Fase **solo de auditoría**: no se ha
modificado el producto. Todo lo creado está en esta carpeta (`pruebas/`, `evidencias/`).

| Dato | Valor |
| --- | --- |
| Versión | `athena.__version__ = 0.3.7`, commit `7b723ae` (árbol limpio salvo esta carpeta) |
| Plataforma | Windows 11 Pro 10.0.26200, Python 3.14.0 (.venv), Tk 8.6 |
| Componentes | runtime `athena` (stdlib), servicio HTTP/SSE `athena_service`, escritorio Tk `athena_desktop`, CLI `athena`, adaptador Telegram `athena_telegram` |
| Dependencias externas | AI_Broker (modelo), AI OS Core (director, opcional/implícito), endpoint OpenAI-compatible (opcional) |
| Modalidad | Inspección de código + pruebas ejecutadas contra **dobles** de Broker y Core (`pruebas/dobles.py`); servicio real como proceso; ventana real en proceso |
| Broker real | Primera sesión: no (dobles). Segunda sesión (§8, a petición del dueño): sí, con `qwen3.8:27b`; Core real solo leído (token del Broker y paso H-01), nunca escrito |
| Aislamiento | Sin costes ni efectos externos en la primera sesión. Todo arnés fija `AI_OS_CONNECT_DIR` y `LOCALAPPDATA` a carpetas temporales: en este equipo existe `%LOCALAPPDATA%\AIOSCore\conexion\athena.json` y sin ese aislamiento Athena habla con el Core real |

## 1. Resumen ejecutivo

**Estado funcional dentro del alcance probado: NO APROBADA** (hay defectos confirmados de
gravedad media en flujos principales), aunque el núcleo es sólido: las cuatro puertas están
verdes (1295 pruebas, ruff, formato, mypy estricto). De los 29 casos de extremo a extremo
del servicio, 23 PASAN, 4 FALLAN (3 de código: E04, C-html, C-lista; 1 documental:
C-implicito) y 2 se registran sin regla (E10, C-implicito-caido). Todos los recorridos de la
ventana salvo dos se comportan como dicen los documentos (consulta, documento con permiso,
deshacer, cancelar, caída y reanudación, gobierno de Core con ALLOW/DENY/caído, token del
Broker servido por Core).

**Límite de la primera sesión:** con los dobles, el servicio se probó con `ATHENA_PLANNING=0`
(bucle directo). El modo de serie, con la planificación encendida, se verificó después con
el Broker real (R07, R08 en §8). Solo **reanudar un plan interrumpido** queda NO VERIFICADO.

Problemas que mandan:

1. **H-05 (media)** — Reanudar desde *Historial* presenta el resultado con el tipo de tarea
   que haya en el formulario, no con el del trabajo. Reproducido: un trabajo de documentos
   reanudado escribe `informe.txt` y la ventana dice «✔ Respuesta dada — no se ha cambiado
   ningún archivo» justo encima de «Archivos cambiados: informe.txt».
2. **H-01 (media)** — «Probar conexión» exige el token del Broker aunque Core lo dé, que es
   precisamente lo que la propia ventana dice al lado del campo («Con AI OS Core no hace
   falta»). *Iniciar* sí funciona sin token. Consecuencia: el desplegable de modelos aptos no
   se puede rellenar sin pegar el token que el ecosistema quería eliminar.
3. **H-03 (baja-media)** — Si Core contesta 200 con algo que no es un objeto JSON (HTML de un
   proxy, una lista), el servicio responde **500 internal_error** en vez de 503
   `core_unavailable`. Falla cerrado (no llega nada al Broker), pero el cliente recibe un
   error que no explica nada.
4. **H-04 (media, documental)** — README: «Without `ATHENA_CORE_URL` nothing changes». Falso:
   basta el fichero de conexión que Core deja en `%LOCALAPPDATA%` para que Athena se someta a
   Core y, si Core está parado, **no arranque ningún trabajo** (503). La conducta es una
   decisión del ecosistema (tokens sin pegar); lo que está mal es la documentación, y eso
   hace muy difícil diagnosticar «Athena no funciona» cuando Core está parado.
5. **H-02 (baja)** — Un `Authorization: Bearer` con un carácter no ASCII da **500** en vez de
   401 (`hmac.compare_digest` con `str` no ASCII lanza `TypeError`).

**Segunda sesión, con el Broker real (19-nov, noche; §8):** confirma H-01 y H-05 con el
Broker y el Core reales y añade tres defectos que los dobles no podían mostrar:

6. **H-09 (media-alta)** — Si un comando del modelo agota su plazo, todo el run termina `failed`
   como si se hubiera acabado el tiempo de la sesión, sin verificar el trabajo hecho. Visto
   con `qwen3.8:27b`: arregló el test, sus propias pruebas pasaban, pidió `python` a secas
   (que en Windows se queda colgado) y el run acabó fallido. Reproducido después de forma
   determinista.
7. **H-10 (media, incoherencia de diseño)** — El escritorio no adoptó el ADR-034: el modelo
   elegido viaja como preferencia, y el Broker atendió **todos** los trabajos de la ventana
   con `gpt-oss-120b` (que no estaba entre los 18 aptos que la ventana listó), mientras la
   ventana enseñaba `qwen3.8:27b`.
8. **H-11 (baja)** — «El proveedor del modelo no respondió» para un modelo que sí respondió,
   pero sin una decisión válida.

Prioridades: H-09 y H-05 primero, después H-10 y H-01 (afectan al uso diario del escritorio y son baratos),
después H-04 (documentación + mensaje), H-03 y H-02 (robustez del servicio).

**Calidad de la auditoría:** suficiente para los flujos principales del escritorio y del
servicio, para la integración con Core y, en la segunda sesión, para el Broker real con
un modelo local (incluida la planificación). **No** cubre System-1, delegación, reanudar un
plan, CLI, Telegram, MCP ni la aprobación remota por SSE (ver §3.3 y §8.4). No se presenta
como revisión completa.

## 2. Mapa funcional y de flujos

### 2.1 Usuarios y objetivos

| Usuario | Objetivo | Entrada |
| --- | --- | --- |
| Persona en el escritorio | Preguntar sobre un proyecto, modificarlo con permisos, generar documentos, deshacer, retomar trabajo | `athena-desktop` / `iniciar-athena.bat` |
| Apps cliente (ChatyGPT, Agora, Core) | Crear y seguir runs, contestar aprobaciones, deshacer | Servicio `127.0.0.1:8770`, bearer por arranque |
| Desarrollador | Investigar / cambiar un repo desde consola | CLI `athena` |
| Canal Telegram | Mismo runtime por mensajería | `athena_telegram` |
| Director (AI OS Core) | Autorizar cada run, ver recibos y saltos en el monitor | `ATHENA_CORE_URL` o `conexion/athena.json` |

### 2.2 Objetivo → recorrido → reglas → componentes → pruebas → resultado

| Objetivo | Recorrido | Reglas implicadas | Componentes | Pruebas de esta auditoría | Resultado |
| --- | --- | --- | --- | --- | --- |
| Primera ejecución | Abrir ventana → elegir proyecto → conexión → tipo → encargo → Iniciar | Iniciar deshabilitado sin proyecto/objetivo; ruta absoluta | `app._refresh`, `RunConfiguration.validate` | U01, U02 | PASA; URL por defecto obsoleta (H-08) |
| Probar conexión | «Probar conexión» | credencial contra ruta protegida; catálogo `completion` | `runtime.check_connection` | U03–U05, `test_h01` | FALLA con Core (H-01); PASA con token |
| Rechazar modelo inútil | Modelo sin `completion` → Iniciar | I-06 | `_refuse_unusable_model` | U05b (diálogo «Athena») | PASA |
| Preguntar | Tipo «Responder» → Iniciar | perfil `questions`, writes forzado a off | `run_options`, `RunRegistry` | U06, E05; E19 (el modelo pide `write_file`) | PASA: no escribe. En la ventana, el forzado a off solo se comprobó por inspección de `run_options` |
| Aviso de consulta que parece cambio | «Corrige el fichero main.py» como consulta | `requires_workspace_change` | `_confirm_task_kind` | diálogo «¿Consulta o cambio?» → Cancelar | PASA |
| Crear documento con permiso | «Crear documentos» + Preguntar | entregables declarados, aprobación con contenido | `ApprovalDialog`, perfil `documents` | U07–U08, E06 | PASA |
| Deshacer | «Deshacer cambios» | solo ficheros del run; conflictos no se tocan | `roll_back_run`, `RollbackLedger` | U09, E07–E08 | PASA |
| Cambio sin comprobaciones | «Modificar» en proyecto sin checks | aviso previo (mejora 3 de oct.) | `verification_warning` | diálogo → Cancelar | PASA |
| Historial | Pestaña Historial | solo runs con manifiesto | `list_runs` | U02, U10 | PASA |
| Cerrar con trabajo en marcha | Cerrar → Sí | cancela y queda en Historial | `_close` | U11 + `historial_final.json` (`cancelled`) | PASA |
| Retomar tras caída | Proceso muere → ventana nueva → Historial → Reanudar | `mark_interrupted`, manifiesto | `_resume_selected`, `RunRegistry.resume` | H-05 (fases caída/reanudar) | Trabajo PASA; presentación FALLA (H-05) |
| Run por API | `POST /v1/runs` | 201/404/405/409, perfiles | servidor `transporte.py`, `endpoints_runs.py` | E01–E18 | PASA salvo E04 (H-02) |
| Aprobación sin cliente | writes=ask sin nadie conectado | sin cliente → DENY | `approvals.py` | E09 | PASA |
| Idempotencia | Repetir `Idempotency-Key` | ADR-021 | `_idempotent_start` | E10 | OBSERVADO (R-01) |
| Cancelar | `POST …/cancel` dos veces | 202 → `cancelled` → 409 | `RunRegistry.cancel` | E13 | PASA |
| Reanudar por API tras matar el proceso | kill → nuevo proceso → `resume` | `recovery_pending` | `session_store`, `resume` | E17–E18 | PASA |
| Gobierno por Core | Core ALLOW/DENY/REQUIRE/500/caído/HTML/lista | sin ALLOW ejecutable no hay llamada al modelo | `ai_os_core.py`, `ciclo.py` | C-allow…C-lista, C-caido | PASA salvo HTML/lista (H-03) |
| Core implícito | Solo `conexion/athena.json` | README dice que nada cambia | `core_settings` | C-implicito, `test_h04` | Contradicción (H-04) |
| Token del Broker vía Core | Sin `ATHENA_BROKER_TOKEN` | `CoreBrokerToken` | `broker_token_source` | C-token | PASA |
| Recibos y saltos al monitor | run con Core | `accepted` → `succeeded`; request/response | `_report_outcome`, `_model_hops` | C-allow | PASA |

### 2.3 Estados de un run

`pending → running → (waiting_permission) → verifying → completed | failed |
verification_inconclusive | cancelled`; tras caída `recovery_pending → (resume) → running`.
Observado en ejecución: `completed`, `failed`, `cancelled`, `recovery_pending→completed`.
`verification_inconclusive` y `killed` no se ejecutaron en esta auditoría (cubiertos por la
suite propia, no verificados aquí).

## 3. Matriz de pruebas y cobertura

### 3.1 Comprobaciones de partida

| ID | Comando | Resultado |
| --- | --- | --- |
| B-01 | `.venv/Scripts/python.exe -m pytest -q` | **1295 aprobadas** en 315 s |
| B-02 | `ruff@0.16.4 check .` | limpio en el producto (`.codex-test-tmp-phase-a/` con ACL rota: aviso de acceso denegado, deuda de entorno ya conocida) |
| B-03 | `ruff@0.16.4 format --check src tests` | 205 ficheros formateados (con `.` el formateador entra en pánico por la carpeta con ACL rota) |
| B-04 | `mypy` estricto (py3.11, `--with pytest`) | sin errores en 205 ficheros |

### 3.2 Casos ejecutados

Formato de cada caso en los JSON de evidencias: id, objetivo, esperado, observado, estado.
Precondiciones comunes: dobles en `127.0.0.1:<puerto libre>`, estado y `LOCALAPPDATA`
temporales, `ATHENA_PLANNING=0` (bucle directo, para que el doble responda de forma
determinista).

**Servicio (`pruebas/e2e_servicio.py` → `evidencias/e2e_servicio.json`, `.log`)**

| ID | Objetivo | Esperado (fuente) | Observado | Estado |
| --- | --- | --- | --- | --- |
| E01 | health sin token | 200 (README, I-01) | 200 | PASA |
| E02 | ruta protegida sin token | 401 (README) | 401 | PASA |
| E03 | token incorrecto | 401 | 401 | PASA |
| E04 | token con «é» | 401 (credencial incorrecta) | **500 internal_error** | FALLA → H-02 |
| E05 | consulta `questions` | 201 y `completed` | 201, completed | PASA |
| E06 | documento writes=allow | `completed` y fichero | completed, `nota.txt` | PASA |
| E07 | puntos de deshacer | 200 con punto | 200, 1 punto (`nota.txt`) | PASA |
| E08 | deshacer | fichero eliminado | `restored: [nota.txt]`, no existe | PASA |
| E09 | writes=ask sin cliente | no escribe, no `completed` | `failed`, sin fichero | PASA (ver R-03) |
| E10 | misma clave, otro objetivo | sin regla documentada (ADR-021) | 200 `idempotent_replay` con el run de «Pregunta A» | OBSERVADO → R-01 |
| E11 | modelo en prosa | `failed` acotado | `failed` tras 3 reintentos | PASA |
| E12 | fallo permanente del Broker | `failed` | `failed` | PASA (ver R-04) |
| E13 | cancelar ×2 | 202 → cancelled → 409 (I-02) | 202, cancelled, 409 | PASA |
| E14 | workspace relativo | 4xx | 403 `workspace_boundary` | PASA |
| E15 | workspace inexistente | 4xx | 403 | PASA |
| E16 | `writes: "quizas"` | 400 | 400 | PASA |
| E17 | kill a mitad de run | `recovery_pending` | `recovery_pending`, reanudable | PASA |
| E18 | `resume` | 2xx | 202 y termina | PASA |
| E19 | perfil `questions` con `writes: allow` y el modelo pidiendo `write_file` | no se crea el fichero (el perfil no tiene herramientas de escritura) | 201; `failed` («tool Athena did not offer»); sin fichero | PASA (nota: la consulta acaba `failed`, no con respuesta) |
| C-allow | Core ALLOW | 201, recibos y saltos | 201; recibos `accepted, succeeded`; saltos `request, response` | PASA |
| C-deny | Core DENY | 403, 0 tareas al Broker | 403 `core_denied`, 0 | PASA |
| C-require | REQUIRE_APPROVAL | 403 | 403 con texto de aprobación humana | PASA |
| C-500 | Core 500 | 503 | 503 `core_unavailable` | PASA |
| C-html | Core 200 HTML | 503 (docstring + README) | **500 internal_error**, 0 tareas | FALLA → H-03 |
| C-lista | Core 200 `["ALLOW"]` | 503 | **500 internal_error**, 0 tareas | FALLA → H-03 |
| C-caido | Core sin escuchar | 503 | 503 | PASA |
| C-implicito-caido | sin `ATHENA_CORE_URL`, `athena.json` apuntando a un puerto sin nadie | se registra | 503 «No se pudo consultar a AI OS Core…» sin decir de dónde salió la URL | OBSERVADO → impacto de H-04 |
| C-implicito | sin `ATHENA_CORE_URL`, con `athena.json` | README: nada cambia → 201 | 403 `core_denied` (Core activo) | FALLA documental → H-04 |
| C-token | token del Broker desde Core | 201 y completed | 201, completed | PASA |

**Ventana (`pruebas/arnes_ventana.py recorrido` → `evidencias/ventana/`)** — app real en
proceso; botones con `invoke()`; `messagebox` sustituido por registro (respuestas en el
JSON, campo `dialogos`); capturas `NN_*.png`.

| ID | Paso | Observado | Estado |
| --- | --- | --- | --- |
| U01 | primera ejecución | «Empieza eligiendo el proyecto», guía «Cómo empezar», URL `http://localhost:8000` | PASA; H-08 |
| U02 | Historial vacío | «Todavía no hay trabajos…» | PASA |
| U03 | Probar conexión, URL por defecto, sin token | «✖ Falta el token de AI_Broker» | PASA (mensaje correcto) |
| U04 | Probar conexión, Core da el token, campo vacío | «✖ Falta el token de AI_Broker» | FALLA → H-01 |
| U05 | Probar conexión con token | «✔ Conectado… 1 modelos aptos», desplegable = `[modelo-bueno]` | PASA (el «estado degradado» lo causa el doble, no Athena) |
| U05b | modelo sin `completion` → Iniciar | diálogo «El modelo «modelo-solo-decision» no sirve…» | PASA |
| U06 | consulta | «✔ Respuesta dada — no se ha cambiado ningún archivo» | PASA |
| U06b | consulta «Corrige el fichero main.py» | diálogo «¿Consulta o cambio?»; Cancelar vuelve | PASA |
| U07 | documento con «Preguntar» | diálogo de permiso con acción, riesgo y contenido (`08_dialogo_permiso.png`) | PASA |
| U08 | Permitir una vez | «✔ Terminado y comprobado», `nota.txt` existe, Deshacer activo | PASA |
| U09 | Deshacer | confirmación → «Restaurados: nota.txt»; el fichero ya no existe | PASA |
| U09b | cambio sin checks | aviso «El trabajo no se podrá comprobar» → Cancelar | PASA |
| U10 | Historial | 2 trabajos | PASA |
| U11 | trabajo en marcha | fase «Esperando al modelo»; config. dice «archivos: Preguntar» en una consulta | PASA; H-06 |
| U12 | Cerrar con trabajo → Sí | se cierra; Historial: `cancelled` | PASA |

**Reanudar (`pruebas/h05_reanudar.py` → `evidencias/h05b/`, repetido en `evidencias/h05c/` con veredicto automático: sale con código 1 mientras H-05 siga abierto)**

| ID | Paso | Observado | Estado |
| --- | --- | --- | --- |
| H05-1 | documento con Permitir, proceso muerto a los 4 s | sin fichero; Historial «Interrumpido (se puede reanudar)», Reanudar activo | PASA |
| H05-2 | ventana nueva con «Responder una pregunta» → Reanudar | trabajo termina y escribe `informe.txt`; titular «✔ Respuesta dada — no se ha cambiado ningún archivo» + «Archivos cambiados: informe.txt» | FALLA → H-05 |

**Pruebas que muerden (`pruebas/test_auditoria_20261119.py`)** — afirman lo esperado; hoy
fallan las 6, una por defecto abierto: `test_h01_*`, `test_h03_*` (×3), `test_h04_*` (comprueba
la **documentación**: README y docstring del módulo; la conducta de Core implícito se da por
buena) y `test_h08_*`. H-05 no tiene prueba unitaria: su regresión es `h05_reanudar.py`.

```
.venv/Scripts/python.exe -m pytest "docs/Auditoria 20261119/pruebas" -q -p no:cacheprovider
```

### 3.3 Cobertura pendiente (NO EJECUTADA en esta auditoría)

| Área | Motivo | Qué cubre hoy |
| --- | --- | --- |
| Delegación; reanudar un plan interrumpido | no ejecutadas (la planificación y el grafo sí: R07, R08 con el Broker real) | suite propia |
| System-1 (juicios por el Broker) | requiere Broker real; apagado por defecto | suite propia |
| Aprobación remota por SSE (ack + decisión), `Last-Event-ID`, `stale` | cubierta en vivo en la auditoría de oct.; no repetida | suite propia + resolución I-09 |
| Revisión del objetivo en marcha («Cambiar objetivo…») | no ejecutada | suite propia + resolución oct. |
| Pestaña «Servicio para otras apps» (Iniciar/Copiar/Detener) | lanzaría un servicio en 8770; no repetida | resolución oct. (I-08) |
| Memoria de proyecto, `/v1/memory`, `/v1/identity/*`, `/v1/metrics`, `/v1/models` | no ejecutadas | suite propia |
| CLI `athena`, Telegram, MCP | fuera de prioridad | suite propia |
| Modelos distintos de `qwen3.8:27b`; modelos de pago | no elegidos (sin coste). Broker real y HTTPS con su CA: **ejecutados en §8** | §8 |
| Accesibilidad (lector de pantalla, contraste medido) | Tk 8.6 no expone UIA; no medido | — |
| Concurrencia escritorio + servicio sobre la misma carpeta | límite ya documentado en `CURRENT_STATE.md` | — |

## 4. Registro de hallazgos

Gravedad = daño cuando ocurre; no es la prioridad (§6).

### H-01 — «Probar conexión» exige el token del Broker aunque Core lo dé
- **Categoría:** lógica / usabilidad. **Gravedad:** media (bloquea comprobar la conexión y
  obtener la lista de modelos aptos en la configuración que el ecosistema da por defecto;
  no pierde datos). **Estado:** confirmado y reproducido.
- **Entorno:** Core con `conexion/athena.json` y `GET /api/v1/credentials/broker` con token;
  campo «Token de este proveedor» vacío.
- **Pasos:** abrir la ventana → URL del Broker → token vacío → «Probar conexión».
- **Esperado:** conectar (lo dice la ayuda del propio campo: «Con AI OS Core no hace falta: Core
  da el token del broker…», y `RunConfiguration.validate` acepta el campo vacío con Core).
- **Observado:** «✖ Falta el token de AI_Broker» (`evidencias/ventana/04_conexion_token_de_core.png`,
  `test_h01_*`). Iniciar, en cambio, funciona sin token: **ejecutado en el servicio** (C-token);
  **en el escritorio, por inspección** (`validate`, `build_provider` y `_refuse_unusable_model`
  usan `broker_token_source`); el arnés no lanzó Iniciar con Core y el campo vacío.
- **Causa raíz confirmada:** `src/athena_desktop/runtime.py`, `check_connection`: comprueba
  `configuration.token.strip()` antes de mirar `broker_token_source()`, al revés que
  `validate()` y `build_provider()`.
- **Corrección propuesta:** usar la misma regla que `validate()` (`not token and
  broker_token_source() is None`) y decir en el mensaje de éxito que el token lo da Core.
- **Regresión:** `test_h01_probar_conexion_acepta_el_token_que_da_core` debe pasar; recorrido U04.

### H-02 — Token con caracteres no ASCII → 500 en lugar de 401
- **Categoría:** técnico / robustez de autenticación. **Gravedad:** baja (no da acceso; un
  cliente con un token mal copiado recibe «Athena failed to handle that request»).
  **Estado:** confirmado (E04).
- **Pasos:** `GET /v1/auth/check` con `Authorization: Bearer tokén-con-tilde`.
- **Esperado:** 401 `unauthorized` (cualquier credencial distinta es «Bad token»).
- **Observado:** 500 `internal_error` y traza en el log.
- **Causa raíz confirmada:** `transporte.py::_authorised` → `hmac.compare_digest(str, str)`
  lanza `TypeError` si alguna cadena no es ASCII.
- **Corrección:** comparar bytes (`presented.encode("utf-8")` contra el token en bytes).
- **Regresión:** E04 en `e2e_servicio.py`.

### H-03 — Core que contesta 200 sin un objeto JSON → 500 en vez de 503
- **Categoría:** integración. **Gravedad:** baja-media (el run no se crea y no llega nada al
  Broker —falla cerrado—, pero el cliente recibe un 500 opaco en lugar de «Core no
  disponible»). **Estado:** confirmado (C-html, C-lista, `test_h03_*`).
- **Esperado:** el adaptador promete que sin ALLOW ejecutable el run no se crea y el README
  documenta 503 `core_unavailable` cuando Core «no contesta».
- **Observado:** `JSONDecodeError` (HTML) o `AttributeError: 'list' object has no attribute
  'get'` (lista) se escapan de `AiOsCoreGovernor.authorize` → 500. El mismo `JSONDecodeError`
  en `receipt` no está suprimido: el recibo se pierde con «exception was never retrieved»
  en segundo plano (el run no se ve afectado).
- **Causa raíz confirmada:** `ai_os_core.py::_post` solo captura `HTTPError`, `URLError`,
  `TimeoutError`, `OSError`; no valida que la respuesta sea un objeto.
- **Corrección:** en `_post`, `ValueError` o respuesta que no sea `dict` → `CoreUnavailableError`
  («Core respondió algo que no es JSON»); en `receipt`, suprimir también esa clase.
- **Regresión:** `test_h03_*` y C-html/C-lista.

### H-04 — El README dice que sin `ATHENA_CORE_URL` nada cambia; el fichero de conexión activa Core
- **Categoría:** contradicción documentación ↔ código. **Gravedad:** media (con Core parado,
  *todo* trabajo del escritorio y del servicio falla con «No se pudo consultar a AI OS Core»
  aunque el usuario nunca configuró Core en Athena; la documentación lleva a buscar el fallo
  en otro sitio). **Estado:** confirmado (C-implicito, C-implicito-caido, `test_h04_*`).
- **Observado:** `core_settings()` usa `%LOCALAPPDATA%\AIOSCore\conexion\athena.json` si no
  hay variable; `governor_from_environment()` devuelve un director. **Ejecutado en el
  servicio** con el fichero apuntando a un Core parado: 503 «No se pudo consultar a AI OS
  Core…», sin decir de dónde salió la URL (C-implicito-caido). En el escritorio el bloqueo es
  **por inspección** (`build_registry` usa el mismo `governor_from_environment`).
- **Contradicciones documentales afectadas:** README «AI OS Core as director» («Without
  `ATHENA_CORE_URL` nothing changes»); docstring del módulo `ai_os_core.py`, líneas 3-4
  («sin `ATHENA_CORE_URL` todo sigue igual»); README, sección de escritorio, líneas ~98-101,
  que solo nombra el token pegado o `ATHENA_BROKER_TOKEN` y no que lo pueda dar Core.
- **Causa:** decisión deliberada del ecosistema (tokens sin pegar, 19-oct) que no se trasladó
  al README ni a `CURRENT_STATE.md`.
- **Corrección propuesta (sin cambiar conducta):** README/`CURRENT_STATE`: «Core se activa con
  `ATHENA_CORE_URL` **o** con el fichero de conexión que Core escribe»; y que el mensaje de
  `CoreUnavailableError` diga de dónde salió la URL y cómo arrancar Core
  («Arrancar AI OS Core.bat») o desactivarlo. Si el dueño quiere que se pueda apagar,
  un `ATHENA_CORE=off` explícito.
- **Regresión:** `test_h04_*` comprueba que el README nombre el fichero de conexión y que el
  docstring no repita la frase falsa; pasa corrigiendo **solo la documentación**. Si el dueño
  añade un interruptor de apagado, se añade su prueba.

### H-05 — Reanudar desde Historial presenta el resultado con el tipo de tarea del formulario
- **Categoría:** lógica / usabilidad. **Gravedad:** media (el mensaje final contradice lo que
  pasó: dice que no se cambió ningún archivo cuando sí se cambió, y el «Qué puedes hacer»
  es el de otra tarea). **Estado:** confirmado y reproducido (H05-2,
  `evidencias/h05b/02_reanudado_resultado.png`, repetido en `h05c`). El caso inverso
  —consulta reanudada con el formulario en «Modificar», anunciada como «Terminado y
  comprobado»— es **por inspección**, no ejecutado. También por inspección: el tipo del
  formulario decide si se activa «Cambiar objetivo…» (`app.py::_on_handle`), así que al
  reanudar un documento con el formulario en «Responder» ese botón queda desactivado.
- **Pasos:** trabajo «Crear documentos» interrumpido → abrir Athena → marcar «Responder una
  pregunta» → Historial → seleccionar → Reanudar → Sí.
- **Esperado:** el resultado se presenta como «Crear documentos» (tipo guardado en el manifiesto,
  que el Historial ya enseña: «Tipo: Documentos»).
- **Observado:** «✔ Respuesta dada — no se ha cambiado ningún archivo» + «Archivos cambiados:
  informe.txt».
- **Causa raíz confirmada:** `app.py::_resume_selected` fija carpeta y objetivo pero no
  `task_kind`; `_start` → `_configuration_from_form` toma el tipo del formulario y `_begin`
  lo guarda en `self.active`, que `_on_result` pasa a `present_result`. Además el formulario
  se **valida** con su tipo actual: con «Crear documentos» y «Cambios en archivos:
  Desactivado» en pantalla, reanudar se niega aunque el run use sus permisos guardados
  (inspección, no ejecutado).
- **Corrección:** en `_resume_selected`, `self.task_kind.set(summary.task_kind)` antes de
  `_start` (y no aplicar la validación de permisos del formulario a una reanudación).
- **Regresión:** `h05_reanudar.py` (sale con código 0 cuando el titular deja de negar los
  ficheros cambiados; hoy sale con 1).

### H-06 — «Estado del trabajo» dice «archivos: Preguntar» en una consulta
- **Categoría:** usabilidad. **Gravedad:** baja. **Estado:** confirmado
  (`evidencias/ventana/11_trabajo_en_marcha.png`).
- **Observado:** la fila «Configuración» muestra el desplegable («archivos: Preguntar») aunque
  `run_options()` fuerza `writes=off` en las consultas.
- **Consecuencia:** quien lee el estado cree que una consulta podría pedir permiso para escribir.
- **Corrección:** en `_begin`, mostrar «archivos: no (consulta)» cuando `task_kind == "question"`.

### H-07 — El cuadro «¿Qué quieres que haga?» sigue editable durante el trabajo
- **Categoría:** usabilidad (heurística). **Gravedad:** baja. **Estado:** observado en la captura
  11 (fondo blanco, editable); no se probó escribir en él.
- **Consecuencia probable:** editar ahí no cambia el trabajo en marcha (eso es «Cambiar
  objetivo…», solo para cambios y documentos); quien lo edite puede creer que sí.
- **Corrección:** deshabilitarlo mientras `_running()` o añadir una línea que remita a
  «Cambiar objetivo…».

### H-08 — URL del Broker por defecto obsoleta en una instalación nueva
- **Categoría:** configuración / primera ejecución. **Gravedad:** baja (solo instalaciones
  nuevas; la configuración real del usuario ya tiene `https://192.168.1.52:8765`).
  **Estado:** confirmado (U01, `test_h08_*`).
- **Observado:** `DEFAULT_URLS["ai_broker"] = "http://localhost:8000"`. El 8000 de este equipo
  lo llegó a ocupar ComfyUI (5-nov).
- **Fuente de lo esperado:** decisión del dueño del 20-oct-2026, **no escrita en el repo de
  Athena**: el Broker pasó a servir solo HTTPS en `192.168.1.52:8765` y Model_Drift 0.4.4,
  Local AI Lab 0.1.6, KO 0.4.8, ChatyGPT 0.4.11 y Trading 1.3.4 cambiaron a ese valor por
  defecto. Es una propuesta que el dueño debe confirmar, no un requisito de Athena.
- **Corrección:** valor por defecto `https://192.168.1.52:8765`; `test_h08` compara el valor
  exacto.

### H-09 — Un plazo agotado en un comando del modelo termina todo el run como fallido
- **Categoría:** lógica / recuperación. **Gravedad:** media-alta: un trabajo **hecho y
  correcto** se da por fallido y no se verifica, así que la persona lo descarta o lo repite.
  El trabajo queda en disco: no se pierde. Hoy aparece con programas interactivos o con
  plazos cortos que pide el propio modelo. El «uno de cada seis runs» del comentario de
  `process_tools.py` es **histórico**: se midió con el plazo anterior de 30 s y no es la
  frecuencia actual. **Estado:** confirmado con el Broker real (R06) y reproducido de forma
  determinista (`pruebas/h09_plazo_de_un_comando.py`, sale con 1; repetido por el árbitro).
- **Entorno:** servicio, perfil `software_engineering`, `exec: allow`, proyecto con
  `python -m unittest` en verde.
- **Observado (R06, real):** el modelo corrigió `calc.py` (`return sum(items)`) y ejecutó dos
  veces `python -m unittest` con salida 0. Después pidió `bash` con `command: "python"` y
  `timeout_seconds: 30`. Ese proceso no termina (abajo) → `tool.failed process_timeout` →
  `recovery.action: cancelled, "The session ran out of time."` → `agent.failed`. Run `failed`,
  `verification: {}`. Con el doble: igual, con **una sola** llamada al modelo.
- **Causa raíz confirmada:** `state.py::classify_outcome` clasifica cualquier
  `ProcessTimeoutError` como `ExecutionOutcome.TIMED_OUT`, el resultado de agotar el plazo
  de la **sesión**. `RecoveryPolicy` lo trata como parada deliberada (`CANCELLED`) y no
  llama a `_salvage`. El plazo de una herramienta y el de la sesión son cosas distintas (el
  de la sesión llega por otra ruta: `bucle.py` captura `TimeoutError`).
- **Intención original frente a la conducta fijada:** `recovery.py` (líneas ~148-153) tiene
  una rama para el plazo de una herramienta: `LIMITED_RETRY`, «The command timed out; retry
  briefly, then change strategy». Esa rama **nunca se ejecuta**, porque `classify_outcome`
  intercepta antes. Pero dos pruebas del proyecto fijan la conducta actual:
  `tests/test_cancellation_semantics.py:57` (`TIMED_OUT`) y `:103` (`CANCELLED`). Ningún ADR
  exige que el plazo de una herramienta pare el run. Corregirlo obliga a cambiar esas pruebas.
- **Causa del cuelgue (confirmada aparte):** `_spawn_process(("python",))` no termina ni en
  10 s, mientras que `python -c "print(1)"` acaba en 0,07 s. En Windows, `stdin=DEVNULL` es
  `NUL`, que cuenta como terminal (dentro del hijo, `sys.stdin.isatty()` es `True`), así que
  Python abre su consola interactiva. Cualquier intérprete o programa interactivo que pida el
  modelo consume su plazo entero (600 s por defecto). Esto es **solo de Windows**; en POSIX
  `python` lee fin de fichero y termina. El fallo de clasificación, en cambio, es de todas las
  plataformas.
- **Corrección propuesta:** separar los dos casos **en la frontera de la herramienta**:
  - que el `ProcessTimeoutError` que lanza `BashTool` llegue al modelo como resultado fallido
    de esa herramienta y use la rama `LIMITED_RETRY` ya escrita;
  - no tocar `classify_outcome` en general, porque también clasifica los
    `ProcessTimeoutError("Operation timed out")` de `await_cancellable` (contexto, MCP,
    System-1, autorización, finalización) y el grafo (`graph_executor.py:237`, donde es
    probable que pase lo mismo; no verificado).
  - Si aun así se para, pasar por `_salvage`, como con el estancamiento y los fallos del
    modelo (N-01 de octubre).
  - Opcional: arrancar los comandos sin que `NUL` cuente como terminal, para que un
    intérprete interactivo termine al momento.
- **Regresión:** `h09_plazo_de_un_comando.py` debe salir con 0 (en otras plataformas,
  cambiar `python` por un comando que duerma más que su plazo). En R06 el esperado es «no
  acaba `failed` por el `process_timeout` de una herramienta y se ejecuta la verificación»;
  que acabe `completed` depende también del modelo.

### H-10 — El escritorio no adoptó el ADR-034: el modelo elegido es solo una preferencia
- **Categoría:** incoherencia de diseño (no es una regla rota). **Gravedad:** media.
  **Estado:** conducta confirmada con el Broker real.
- **Por qué no es un defecto sin más:**
  - es deliberado: la etiqueta dice «Modelo o preferencia» y su comentario «se puede escribir
    cualquier preferencia» (`app.py:427-429`);
  - el ADR-034 dice que una preferencia viaja con `fallback_allowed: true` a propósito;
  - la resolución I-06 de octubre ya midió que el Broker reenruta sin avisar.
- **Por qué es incoherente:**
  - el ADR-034 solo trata el servicio y ChatyGPT y deja fuera el escritorio, pese a que dice
    «A selector the broker may silently ignore is not a selector»;
  - la ventana valida el modelo elegido (I-06) y lo muestra en «Configuración», pero puede
    trabajar otro.
- **Observado:** con `qwen3.8:27b` en «Modelo o preferencia», la Actividad de los tres
  trabajos del escritorio nombra «auto» y `gpt-oss-120b` (V04, V06, H05-real2). Mientras,
  «Configuración» dice `qwen3.8:27b` (captura `h05_real2/02_reanudado_resultado.png`). En el
  servicio, con el mismo Broker y el mismo modelo, contestó `qwen3.8:27b` en todas las
  llamadas (R01), aunque a otra hora. `gpt-oss-120b` **no está entre los 18 modelos aptos
  que listó la ventana** (V02, guardado). Que tampoco esté entre los 28 del sondeo es
  **inferido**: esa salida no se guardó, y `model_capabilities` descarta los modelos que no
  están `available`.
- **Estado de la cola: NO VERIFICADO** durante V04 y V06; un turno tardó 5 min. En H05-real2
  la fila «Cola del modelo» decía «Libre» al final (captura). Que la cola influyera en el
  enrutado no está probado; el mecanismo (`fallback_allowed`) sí.
- **Riesgos asociados (no gravedad):** en la tirada `h05_real` el modelo sustituto no dio
  decisiones válidas y el run falló. Si el Broker enruta a la nube, podría haber coste (no
  comprobado).
- **Causa raíz confirmada:** `athena_desktop/runtime.py`. `build_provider` pasa el modelo como
  `preferred_model` y `run_options` no rellena `RunOptions.model`. En
  `AiBrokerModelProvider._submit` eso es `fallback_allowed: true`, así que el Broker puede
  sustituirlo. El servicio sí manda el modelo como elección del run, que es estricta
  (ADR-034).
- **Matiz:** la etiqueta dice «Modelo o preferencia», así que la sustitución está insinuada.
  Pero nada en la ventana avisa de que ha ocurrido, salvo la línea de Actividad.
- **Corrección propuesta:** que el escritorio mande el modelo como elección del run
  (`RunOptions.model`, estricta), con una casilla «dejar que el Broker elija» para quien lo
  quiera blando. Y que «Configuración» o el resultado digan qué modelo trabajó de verdad.
- **Regresión:** repetir `ventana_broker_real.py`: las líneas «El modelo (…)» deben nombrar
  el modelo elegido.

### H-11 — «El proveedor del modelo no respondió» cuando sí respondió, pero mal
- **Categoría:** usabilidad. **Gravedad:** baja. **Estado:** confirmado con el Broker real
  (`h05_real/02_reanudado_resultado.png`).
- **Observado:** el modelo contestó cinco veces; las últimas sin una decisión válida
  (`AI_Broker returned an empty tool decision`). El resultado dice «✖ No se ha podido
  completar. El proveedor del modelo no respondió.»
- **Causa:** `presentacion.py` traduce todo `model_transient_error` por esa frase.
- **Consecuencia:** quien lo lee busca un Broker caído o la red, cuando el problema es el
  modelo elegido (o el que el Broker puso, H-10).
- **Corrección:** distinguir «no respondió» (red, plazo) de «respondió sin una decisión
  válida» y nombrar el modelo.

### Riesgos e hipótesis (no confirmados como defecto)

| ID | Riesgo | Evidencia | Qué falta para decidir |
| --- | --- | --- | --- |
| R-01 | Repetir una `Idempotency-Key` con **otro** objetivo devuelve en silencio el run del primero | E10; ADR-021 solo promete «como mucho un run por clave»; la auditoría de oct. ya vio a Agora reutilizar la clave al repetir una tarjeta con el mismo nombre | Que el dueño decida la regla: 422 si el cuerpo cambia (práctica habitual) o seguir así y documentarlo |
| R-02 | La prioridad de cola que da Core viaja en una `ContextVar` fijada dentro de `authorize` | Inspección: correcto hoy porque cada petición HTTP es una tarea nueva (`Connection: close`) y el escritorio usa `asyncio.run` por trabajo | Si el transporte pasa a keep-alive, una petición heredaría la prioridad de otra |
| R-03 | Tras una aprobación denegada, el run sigue dos ciclos de reparación llamando al modelo | E09: `Repair cycle 1/2` tras `permission_denied` | Si es deseable (buscar alternativa) o gasto inútil cuando la persona dijo «no»; medir con modelo real |
| R-04 | El motivo concreto de un fallo permanente del Broker no llega al resumen del run | E12: mensaje «AI_Broker could not complete the task»; el detalle va en `details` | Comprobar si `/history` o la ventana lo enseñan |
| R-05 | `resume` deja una entrada en `_flows` si Core niega | Inspección de `ciclo.py::resume` (en `start` sí se limpia) | Fuga menor de memoria por run denegado |

## 5. Propuestas de mejora

### Lógica

| # | Propuesta | Antes → después | Componentes | Coste | Aceptación |
| --- | --- | --- | --- | --- | --- |
| L-1 (H-05) | Reanudar con el tipo y permisos del run | resultado con el tipo del formulario → con el del manifiesto | `app._resume_selected` | bajo | H05-2 dice «Terminado y comprobado» con `informe.txt` |
| L-2 (H-03) | Core con respuesta no-JSON = no disponible | 500 → 503 `core_unavailable` | `ai_os_core._post`, `receipt` | bajo | `test_h03_*` en verde |
| L-3 (H-02) | Comparar tokens en bytes | 500 → 401 | `transporte._authorised` | muy bajo | E04 en verde |
| L-4 (R-01) | Idempotencia con huella del cuerpo | replay silencioso → 422 `idempotency_key_reused` | `_idempotent_start` | bajo; depende de decisión | E10 con 422; misma carga sigue dando 200 replay |
| L-5 (H-04) | Decir de dónde viene Core en el error | «No se pudo consultar a AI OS Core» → «…(configurado por %LOCALAPPDATA%\AIOSCore\conexion\athena.json). Arranca Core o…» | `ai_os_core`, README | bajo | mensaje visible en C-caido con fichero |

### Usabilidad

| # | Recorrido que cambia | Propuesta | Coste | Aceptación |
| --- | --- | --- | --- | --- |
| D-1 (H-01) | Primera configuración con Core: «Probar conexión» → hoy ✖ «Falta el token»; propuesto ✔ «Conectado; el token lo da AI OS Core» y desplegable lleno | regla única de credencial | bajo | U04 en verde |
| D-2 (H-08) | Instalación nueva: la URL ya apunta al Broker real | valor por defecto | muy bajo | `test_h08` |
| D-3 (H-06) | Consulta en marcha: «archivos: no (consulta)» | texto de configuración | muy bajo | captura U11 |
| D-4 (H-07) | Trabajo en marcha: el encargo no parece editable | deshabilitar el cuadro | muy bajo | prueba de estado del widget |
| D-5 | «Probar conexión» no depende de «Iteraciones»/«Tiempo» (hoy un valor no numérico allí impide probar la conexión, inspección de `_test_connection`) | leer solo proveedor/URL/modelo | muy bajo | prueba unitaria |

## 6. Plan priorizado

| Orden | Qué | Gravedad | Prioridad | Esfuerzo | Dependencias |
| --- | --- | --- | --- | --- | --- |
| 0 | H-09: un plazo de herramienta no es el plazo de la sesión | media-alta | máxima (da por fallido un trabajo hecho y no lo verifica) | bajo-medio (frontera de la herramienta + 2 pruebas que cambian) | — |
| 0b | H-10: aplicar el ADR-034 al escritorio y mostrar el modelo real | media | alta | bajo | decisión del dueño (¿estricto por defecto?, casilla «dejar elegir al Broker») |
| 1 | H-05 / L-1 | media | alta (mensaje final falso en un flujo de recuperación) | bajo | — |
| 2 | H-01 / D-1 | media | alta (uso diario con Core) | bajo | — |
| 3 | H-04 / L-5 | media | alta (diagnóstico cuando Core está parado) | bajo | decisión del dueño sobre `ATHENA_CORE=off` |
| 4 | H-03 / L-2 | baja-media | media | bajo | — |
| 5 | H-02 / L-3 | baja | media | muy bajo | — |
| 6 | R-01 / L-4 | — | media | bajo | decisión de contrato; avisar a Agora/ChatyGPT |
| 7 | H-08, H-06, H-07, H-11, D-5 | baja | baja | muy bajo | — |
| 8 | R-02…R-05 | — | baja | medir antes | modelo real para R-03 |

## 7. Cierre de verificación

- **Modalidad real:** inspección de código, pruebas ejecutadas contra dobles (servicio real
  como proceso, ventana real en proceso con `invoke()` y diálogos registrados) y un **árbitro
  independiente** (agente separado sin el contexto del auditor) que revisó el informe, el
  código y las evidencias JSON.
- **Rondas:** 3 de las 3 permitidas.
  - Ronda 1: informe con dobles y 9 objeciones del árbitro.
  - Ronda 2: las 9 aceptadas y corregidas; e2e y H-05 repetidos.
  - Ronda 3 (tras la sesión con el Broker real, §8): 8 objeciones sobre H-09, H-10, H-11 y
    §8, aceptadas y corregidas.

  Detalle en `REVISION_ARBITRO.md`.
- **Objeciones descartadas:** ninguna.
- **No realizado:** ver §3.3 y §8.4 (System-1, delegación, reanudar un plan, SSE con cliente
  real, pestaña Servicio, CLI, Telegram, MCP, modelos de pago, accesibilidad medida). El árbitro no volvió a ejecutar la suite completa ni los
  arneses de la ventana ni miró las capturas; sí ejecutó las pruebas de auditoría.
- **Veredicto sobre la auditoría:** cumple el LISTÓN **dentro del alcance declarado**.
  **Veredicto sobre la app:** **NO APROBADA** dentro de ese alcance (H-09 en el servicio;
  H-01, H-05 y H-10 en flujos principales del escritorio; todos confirmados con el Broker
  real); el resto queda **NO VERIFICADO**.

## 8. Pruebas con el Broker real (segunda sesión, 19-nov noche)

A petición del dueño, con el Broker encendido. **Modalidad:**
- Broker real `https://192.168.1.52:8765`, con la CA del Broker.
- Token pedido a AI OS Core con un `GET /api/v1/credentials/broker` y pasado por variable de
  entorno; nunca por línea de órdenes ni a disco.
- Core **aislado** en todos los runs (`AI_OS_CONNECT_DIR` vacío): no se escribieron
  recibos, saltos ni evaluaciones en el Core real.
- Lecturas de la carpeta de conexión real: `core_connection()` para pedir el token
  (`broker_real.py`) y el paso H-01 de la ventana. Ninguna escritura.
- Modelo `qwen3.8:27b`, el medido como más fiable en agosto. Solo modelos locales; no se
  eligió ninguno de pago.
- Sondeo inicial (`pruebas/broker_real.py`): `healthy`, token aceptado, 28 modelos (18 con
  `completion`) y cola vacía.

### 8.1 Servicio (`pruebas/e2e_broker_real.py` → `evidencias/broker_real/`)

| ID | Objetivo | Esperado | Observado | Estado |
| --- | --- | --- | --- | --- |
| R00 | catálogo del despliegue | permitidos | `qwen3.8:27b` (por defecto), `nemotron-3.5-lightning:30b` | PASA |
| R01 | consulta real | `completed` con el modelo pedido | `completed`; `qwen3.8:27b` en las 3 llamadas | PASA |
| R02 | documento real (bucle) | `completed` y fichero | `resumen.txt` = «Programa que cuenta botellas de agua en un almacén.» | PASA |
| R03 | deshacer | fichero eliminado | `restored: [resumen.txt]` | PASA |
| R04 | modelo no permitido | 4xx | 400 «Modelo desconocido… Disponibles: …» | PASA |
| R05 | cancelar con una llamada en curso | `cancelled` en segundos | 202 → `cancelled` en 0,5 s | PASA |
| R06 | corregir un test con `unittest` real | `completed` | código corregido y sus tests en verde, pero el run acaba `failed` (`process_timeout`) | FALLA → H-09 |
| R07 | planificación encendida (modo de serie) | `completed` | `plan.decided` (eligió el bucle), `notas.txt` | PASA |
| R08 | plan forzado (`hierarchical`) | grafo y cierre `agent.*` | `graph.started/completed`, `task.*`, `agent.completed`, `indice.txt` | PASA |

R07 y R08 cubren el modo de serie que la primera sesión había dejado NO VERIFICADO.

### 8.2 Ventana (`pruebas/ventana_broker_real.py` → `evidencias/ventana_real/`)

| ID | Paso | Observado | Estado |
| --- | --- | --- | --- |
| V01 | H-01 con el **Core real** (token vacío) | «✖ Falta el token de AI_Broker» | FALLA → H-01 confirmado en real |
| V02 | Probar conexión con token | «✔ Conectado… 18 modelos aptos», desplegable con los 18 | PASA |
| V03 | modelo real sin `completion` (`tev1:4b`) | diálogo «no sirve para trabajar con Athena… declara: decision» | PASA (I-06) |
| V04 | consulta real | «✔ Respuesta dada»; respuesta «El programa cuenta botellas de agua en un almacén.» | PASA; modelo real `gpt-oss-120b` → H-10 |
| V05–V06 | documento con «Preguntar» → Permitir | diálogo con el contenido; «✔ Terminado y comprobado»; `resumen.txt` correcto | PASA; modelo `gpt-oss-120b` → H-10 |
| V07 | Deshacer | «Restaurados: resumen.txt», fichero eliminado | PASA |

### 8.3 Reanudar (H-05) con el Broker real

| Tirada | Observado | Estado |
| --- | --- | --- |
| `evidencias/h05_real` (en paralelo con 8.1; estado de la cola no registrado) | el trabajo reanudado lo atendieron «auto» y `gpt-oss-120b`, que no dio decisiones válidas; `failed` con «El proveedor del modelo no respondió» | H-05 **no concluyente** (no escribió); origen de H-10 y H-11 |
| `evidencias/h05_real2` (en serie; la captura final dice «Cola: Libre») | `informe.txt` = «El proyecto cuenta botellas de agua en un almacén.»; titular «✔ Respuesta dada — no se ha cambiado ningún archivo» + «Archivos cambiados: informe.txt» | FALLA → **H-05 confirmado en real** (guion con código 1) |

### 8.4 Qué sigue sin verificarse con el Broker real

- System-1 (apagado por defecto).
- Aprobación remota por SSE con un cliente real (ChatyGPT).
- Delegación (`delegate_task`).
- CLI, Telegram, MCP.
- Coste real de `gpt-oss-120b`: no se sabe si es local o de pago.
- Reanudar un plan interrumpido.

## 9. Correcciones (0.3.8) y pruebas que faltaban (20-nov)

Autorizadas por el dueño («realiza las correcciones y pruebas que faltan»). Registro completo
en `docs/AUDIT_2026-11-19_RESOLUTION.md`. Resumen:

- **Corregidos:** H-01 a H-11 (H-07 era solo visual), R-04, R-05, y cuatro defectos nuevos
  hallados al probar:
  - **H-13:** una tarea de un plan no se guardaba como «en marcha», así que reanudar la
    repetía;
  - **CLI-1:** una consulta de solo lectura se daba por fallida y no imprimía la respuesta;
  - **CLI-2:** `--resume` con un id desconocido daba una traza de Python;
  - y la causa del cuelgue de H-09 en Windows: `NUL` cuenta como terminal.

  Cada corrección tiene su prueba y se comprobó que falla sin ella. Puertas: 1319 pruebas,
  ruff, formato y mypy limpios. Wheel 0.3.8 regenerado.
- **Corregidos en una segunda ronda:** R-01 (misma clave con otro cuerpo → 422
  `idempotency_key_reused`; cambio de contrato para Agora y ChatyGPT), H-12 (cuentan los
  modelos `online` locales, no los de la nube), TG-1 (Telegram anunciaba un run inconcluso
  como «stopped») y A11Y-1 (el verde de éxito no llegaba a 4,5:1).
- **Sin corregir:** R-02 y R-03, sin evidencia de daño.
- **Pruebas pendientes, ahora ejecutadas:**
  - con el Broker real: SSE como ChatyGPT (P1), R06 con H-09 corregido (P6), System-1 en
    sombra (P2), plan interrumpido (P4), H-10 en la ventana. Todas PASAN;
  - P3, delegación: OBSERVADO, el modelo no delegó;
  - con dobles: pestaña «Servicio para otras apps» y CLI de punta a punta. PASAN;
  - repetidos tras corregir: e2e del servicio (29 casos, 0 en FALLA), H-05, H-09, H-13 y
    el recorrido de la ventana.
  - Telegram de punta a punta (bot real con un doble de la Bot API): T1-T3 PASAN.
  - MCP en un bucle real con un cliente en proceso (Athena no trae transporte): M1-M3
    PASAN; M3 confirma H-09 también para un servidor MCP colgado.
  - Accesibilidad medida en la ventana: teclado (Ctrl+Intro, Escape, Tab) y contraste
    WCAG AA. PASAN tras A11Y-1.
  - MCP con un servidor **real de terceros** (`tradingview-mcp` 2.0.0 por stdio): 84
    herramientas adaptadas, permiso R3 y llamada real hasta el modelo. PASA.
  - Lector de pantalla, medido con UI Automation: 75 elementos «Pane» sin nombre.
    **A11Y-2 confirmado**: no se arregla sin cambiar de toolkit, y queda propuesto con la
    medición como justificación. El README lo dice y remite al CLI.
- **Pendiente por decisión del dueño:** Telegram con la Bot API real; aún no hay bot
  preparado. Los pasos para repetirlo están en la resolución.

**Veredicto actualizado sobre la app, dentro del alcance probado: APROBADA en 0.3.8.** Todos
los defectos confirmados están corregidos y verificados, también con el Broker real.

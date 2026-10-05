# System-1 en Athena

Implementación basada en `02_Athena_SYSTEM1.md` y en el [Client_API actualizado](../../docs/Client_API.md), sección 15. El código de Athena usa exclusivamente AI_Broker: el operador decide el orden de proveedores. El contrato actualizado describe Nimble local → Laya MCP → alternativa de la aplicación.

## Arquitectura y decisiones

- `system1.py` contiene la configuración, las peticiones y la política conservadora común. `adapters/system1_broker.py` reutiliza el transporte autenticado existente, comprueba `system1_judgments` y hace un POST síncrono a `/api/v1/system1/judge`. No crea tareas, sondea estados ni llama directamente a Ollama o Laya.
- El servicio comprueba capacidades al arrancar cuando hay alguna función habilitada. El descubrimiento tiene su propio plazo breve; si falla, el servicio sigue arrancando y vuelve a comprobarlas antes del primer juicio. Si el broker informa `system1_judgments: false`, Athena lo cree durante 60 segundos y después vuelve a preguntar: el operador puede activar el servicio sin reiniciar Athena. Una respuesta `true` se conserva. Sin funciones habilitadas no hace llamadas System-1.
- `AgentLoop` usa la misma política determinista y los mismos hooks de verificación que antes. Tras un bloque con archivos modificados y un comando exitoso, puede cerrar con un juicio positivo de confianza ≥ 0,97. Requiere evidencia independiente de la respuesta final, ausencia de trabajo pendiente y un objetivo vigente. Un check fallido nunca puede ser reemplazado por un juicio positivo. Una revisión obligatoria impide este cierre anticipado. El tamaño de una salida no la convierte en obligatoria: leer un fichero grande o una suite de tests verbosa no es una operación sensible (ver «Evidencia truncada»).
- Al terminar normalmente o rescatar un run detenido, un juicio negativo aceptado exige completar los requisitos pendientes. Rechazo, baja confianza, timeout o fallo conservan la verificación y recuperación anteriores. El juicio recibe objetivo, criterios, estado, archivos modificados, salida, comprobaciones y pendientes; no recibe el repositorio completo.
- `ContextBuilder` filtra solamente notas opcionales recuperadas de memoria. Conserva instrucciones, petición, historial de herramientas, errores, estado, criterios y definiciones de herramientas. Los recuerdos confirmados por el usuario, restricciones y notas marcadas obligatorias se conservan completos, incluidas sus líneas de continuación. El máximo de candidatos limita llamadas; lo no puntuado permanece. Cualquier juicio fallido conserva todas las notas originales. El resultado se reutiliza mientras objetivo y notas no cambien.
- `GraphExecutor` y `DelegateTaskTool` colocan el gate antes de los verificadores existentes. La delegación directa conserva primero el control de permisos. Se requiere evidencia determinista, salida del ejecutor y criterios de aceptación. Revisiones expresas, operaciones sensibles/destructivas, inconsistencias de contratos, resultados incompletos, checks fallidos, pérdida de procedencia y una salida juzgada que llegó truncada mantienen al reviewer. La verificación final del grafo permanece.
- La sensibilidad se deriva de los riesgos y niveles R0–R4 de PermissionEngine. Se propaga desde los delegados, incluidas continuaciones. System-1 nunca concede permisos ni evita una denegación.

## Compatibilidad con el Client_API revisado

Cada función usa un `use_case` propio de Athena y toma el umbral del perfil del operador para ese mismo tipo de decisión con `threshold_profile`:

| Función Athena | `use_case` | `threshold_profile` | Umbral del broker | Umbral local |
|---|---|---|---:|---:|
| Objetivo completado | `athena_goal_completion` | `goal_completion` | 0,97 | 0,97 |
| Relevancia del contexto | `athena_context_ranking` | `ranking` | 0,85 + margen 0,15 | 0,85 de confianza |
| Omitir segunda revisión | `athena_reviewer_gate` | `agora_review_gate` | 0,97 | 0,97 |

Así las métricas del broker (`use_case:<nombre>`, §15.7) separan a Athena de Agora y del resto de aplicaciones, que antes compartían `agora_review_gate`, `goal_completion` y `ranking`. No hace falta tocar el broker. Comprobado en vivo el 3 de octubre: `athena_reviewer_gate` sin perfil devuelve `UNKNOWN_USE_CASE`; con `threshold_profile: "agora_review_gate"` aplica 0,97 y rechaza con `LOW_CONFIDENCE` una salida parcial con una inyección «answer true» (nota cruda 0,903); con `"default"` (0,85) esa misma salida se habría aceptado.

Athena envía sus propias instrucciones en cada petición; el perfil `agora_review_gate` del broker no trae instrucciones. Si el operador registra un perfil propio para un caso de Athena, basta con vaciar su variable `..._THRESHOLD_PROFILE` para dejar de enviarlo. Un perfil explícito desconocido devuelve `UNKNOWN_THRESHOLD_PROFILE` y mantiene el flujo anterior. El gate rechaza en la configuración el perfil `default`: omitir una revisión no puede depender del umbral por defecto.

El DTO admite también `threshold_profile: "default"` para un caso propio, conforme al contrato, pero ninguna de las tres funciones lo usa. No envían campos no admitidos, modelos, timeouts de petición ni clasificación de riesgo al endpoint.

La detección de objetivo tiene tres resultados lógicos: `complete` (booleano positivo aceptado por encima del umbral), `incomplete` (negativo aceptado por encima del umbral) y `uncertain` (rechazo, error o confianza insuficiente). El modo sombra registra el juicio y conserva el flujo anterior.

Se distingue un `false` aceptado de un rechazo. Se acepta un resultado válido del segundo proveedor aunque `fallback_used` sea `true`. `accepted: false` siempre activa la alternativa, incluidos `UNKNOWN_USE_CASE`, `UNKNOWN_THRESHOLD_PROFILE`, `INPUT_TOO_LARGE` y códigos futuros. Athena no reintenta un rechazo ni busca una segunda opinión.

El score de contexto es un índice ordinal: 0 = irrelevante, 1 = posiblemente útil, 2 = relevante. Se traduce a la escala de relevancia local 0 / 0,65 / 1 para aplicar los umbrales configurados. No se interpreta como probabilidad. Tampoco la confianza top1 es una probabilidad calibrada. Puede exigirse calibración para omitir revisiones; con la instalación actual, que informa `false`, esa opción conserva todos los reviewers.

Todas las peticiones usan `cloud_allowed: false`. El cliente valida aceptación, eco del caso, booleanos, índices de rúbrica y confianza finita entre 0 y 1. Campos aditivos de capacidades y respuesta no rompen la compatibilidad.

La ampliación del 3 de octubre añade `system1_evaluation`, `target` para evaluar un proveedor/modelo concreto y notas en bruto dentro de `attempts`, incluso cuando se rechaza el juicio (§15.8). La capacidad de evaluación es independiente de `system1_judgments`: su ausencia o valor `false` no impide los juicios operativos; su valor `true` tampoco habilita un servicio de juicios apagado. Las funciones operativas no envían `target`, para conservar la política de proveedores del operador.

Cada intento puede declarar `score_source: "native"` (Nimble o Laya) o `"self_reported"` (un modelo generativo usado como profesor). El contrato permite al modelo generativo juzgar únicamente con `target` y rechaza siempre su resultado principal con `SELF_REPORTED_SCORE`; una puntuación autodeclarada de 1,0 no autoriza una decisión. Si el operador configura un modelo generativo como juez automático, el broker devuelve `MODEL_CAPABILITY_MISMATCH` sin invocarlo. Athena conserva el flujo anterior ante esos rechazos.

Athena toma decisiones únicamente del primer nivel con `accepted: true`; nunca promueve una nota de `attempts` a decisión de negocio. Las pruebas comprueban que los intentos rechazados, incluidos los autodeclarados de confianza 1,0, no pueden cerrar el objetivo, omitir al reviewer ni retirar contexto. También verifican que el rechazo se conserva en los eventos y no causa reintentos.

La comprobación de conexión del adaptador generativo usa `GET /api/v1/auth/check` (§3). Distingue un token validado de `auth_required: false`, rechazos de credencial, respuestas incompatibles y un servicio de autenticación no disponible. Si un broker anterior no tiene esa ruta (`404`), conserva la comprobación mediante el endpoint protegido de tareas del panel. Nunca valida credenciales mediante `health` o `capabilities`.

El broker real responde a un fallo de autenticación con el código como cadena en `detail` (`{"detail": "ADMIN_AUTH_REQUIRED"}`). Athena reconoce el código en esa forma y en `code`, `error.code` o `detail.code`; antes un `503` con la forma real se habría tratado como fallo transitorio, con reintento y cancelación de la tarea.

La revisión posterior de §3 exige distinguir `403 ADMIN_AUTH_REQUIRED` de `503 ADMIN_AUTH_BACKEND_UNAVAILABLE` también durante un trabajo. Un rechazo de acceso al enviar o consultar tareas se informa inmediatamente como `model_authentication_required`; un fallo identificado del almacén de credenciales se informa como `model_authentication_backend_unavailable`. No provoca reintentos automáticos ni cambio de proveedor. Si ya existe una tarea, no se envía su cancelación: el identificador y `task_preserved: true` quedan en el error y en el evento del run. La ventana presenta una interrupción de conexión y explica si hay que renovar el token o restablecer el almacén; no atribuye esos fallos al contenido del trabajo. Los demás errores HTTP 503 conservan la recuperación anterior.

System-1 conserva su alternativa ante ambos errores HTTP, pero sus eventos mantienen `ADMIN_AUTH_REQUIRED` o `ADMIN_AUTH_BACKEND_UNAVAILABLE` para diagnosticar la causa. La renovación del token sigue siendo manual: esta revisión no añade lectura automática del llavero ni reanudación automática de tareas remotas conservadas.

## Configuración

El servicio y los procesos gestionados heredan estas variables del entorno. Hay que definirlas antes de arrancar Athena. Los tres flags están desactivados y el modo sombra está activado por defecto.

| Variable | Valor predeterminado |
|---|---|
| `ATHENA_SYSTEM1_GOAL_COMPLETION` | `false` |
| `ATHENA_SYSTEM1_CONTEXT_FILTERING` | `false` |
| `ATHENA_SYSTEM1_REVIEWER_GATE` | `false` |
| `ATHENA_SYSTEM1_SHADOW_MODE` | `true` |
| `ATHENA_SYSTEM1_GOAL_THRESHOLD` | `0.97` |
| `ATHENA_SYSTEM1_REVIEWER_THRESHOLD` | `0.97` |
| `ATHENA_SYSTEM1_CONTEXT_CONFIDENCE` | `0.85` |
| `ATHENA_SYSTEM1_CONTEXT_INCLUDE` | `0.80` |
| `ATHENA_SYSTEM1_CONTEXT_EXCLUDE` | `0.55` |
| `ATHENA_SYSTEM1_TIMEOUT_SECONDS` | `75` |
| `ATHENA_SYSTEM1_CAPABILITIES_TIMEOUT_SECONDS` | `3` |
| `ATHENA_SYSTEM1_MAX_CANDIDATES` | `8` |
| `ATHENA_SYSTEM1_CONTEXT_BUDGET_CHARS` | `60000` |
| `ATHENA_SYSTEM1_REQUIRE_CALIBRATED_REVIEW` | `false` |
| `ATHENA_SYSTEM1_GOAL_USE_CASE` | `athena_goal_completion` |
| `ATHENA_SYSTEM1_CONTEXT_USE_CASE` | `athena_context_ranking` |
| `ATHENA_SYSTEM1_REVIEWER_USE_CASE` | `athena_reviewer_gate` |
| `ATHENA_SYSTEM1_GOAL_THRESHOLD_PROFILE` | `goal_completion` |
| `ATHENA_SYSTEM1_CONTEXT_THRESHOLD_PROFILE` | `ranking` |
| `ATHENA_SYSTEM1_REVIEWER_THRESHOLD_PROFILE` | `agora_review_gate` (no admite `default`) |

Un `..._THRESHOLD_PROFILE` vacío no envía perfil: el `use_case` debe tenerlo configurado en el broker.

Los umbrales locales pueden endurecer los del Broker; no rebajan los umbrales del operador. El timeout HTTP de 75 segundos cubre los dos intentos secuenciales de 30 segundos del contrato actual. Si el operador cambia esos plazos, hay que ajustar la variable correspondiente.

El presupuesto de contexto es conservador: permite retirar candidatos de relevancia intermedia cuando falta espacio, pero nunca recorta información protegida o claramente relevante para cumplir un límite. Las referencias implícitas como «haz lo mismo con Athena» conservan los candidatos intermedios incluso con poco presupuesto.

**No activar `CONTEXT_FILTERING` con el juez actual.** Medido el 3 de octubre contra Nimble con la rúbrica de Athena: cuatro candidatos (una nota de Knowledge_Orchestrator, una de Athena, una de trading y una receta de cocina) salieron todos con `LOW_CONFIDENCE` (0,52–0,71), y la receta con «relevante» como nivel más votado. Una variante binaria tampoco discrimina (la receta, `true` 0,925). El filtro es seguro —ante el rechazo conserva todas las notas—, pero no ahorra nada y añade una llamada por construcción de contexto. El benchmark muestra lo que ahorraría un juez que sí distinga, no lo que hace Nimble.

Para observar las tres funciones, poner los tres flags a `true` y conservar `SHADOW_MODE=true`. Para aplicar las decisiones, poner `SHADOW_MODE=false`. Se reutilizan `ATHENA_BROKER_BASE_URL` y `ATHENA_BROKER_TOKEN`; las credenciales no se guardan en la telemetría.

`POST /v1/runs` admite además dos claves de **primer nivel** del cuerpo, junto a `objective` y `workspace` (no dentro de un objeto `options`):

- `acceptance_criteria`: lista de cadenas.
- `mandatory_review`: `true`, `false` o ausente. `true` declara que el usuario pidió revisión. `false` declara que no la pidió: Athena deja de buscar «review», «revisa», «auditoría»… en el objetivo, lo que necesita un cliente como Agora, que compone el objetivo con el texto de perfiles y skills. Ausente, Athena busca esas palabras como antes. Cualquier otro valor (por ejemplo `"false"`) se rechaza. Las demás causas de revisión obligatoria —operaciones sensibles, checks fallidos, inconsistencias, evidencia truncada— se aplican siempre.

Los valores se guardan con el run y llegan al bucle y al gate jerárquico, también al reanudar. El gate recibe el objetivo global y los criterios del usuario, del reviewer y de los ejecutores. Las tareas jerárquicas conservan sus propios criterios.

## Evidencia truncada

El gate juzga como salida del ejecutor el último resultado de herramienta, recortado a 2000 caracteres. Si ese resultado se recortó o se guardó aparte por tamaño, el gate mantiene al reviewer. La marca es solo de ese último resultado y se recalcula en cada llamada.

Antes, cualquier salida de más de 2000 caracteres —o externalizada— en cualquier momento del run lo marcaba entero como revisión obligatoria. Eso apagaba el checkpoint de objetivo en casi cualquier run real (leer un fichero mediano o ejecutar pytest basta) y hacía que un delegado que hubiera leído un fichero grande nunca pudiera omitir al reviewer. Con una suite verbosa el ahorro del benchmark pasaba de 5 → 4 iteraciones a 5 → 5. Ahora se conserva (fila «verbosa» de la tabla).

## Telemetría

Los eventos `system1.judged`, `system1.context`, `system1.review`, `system1.completion` y `system1.comparison` se guardan en el registro existente. Incluyen decisión, confianza, umbral, proveedor, modelo, plazo, razón y propuesta/aplicación cuando corresponde. No incluyen el input del juicio ni el prompt completo.

Las métricas del run contienen un objeto `system1`, persistido en una tabla adicional compatible con bases existentes: juicios, fallbacks por juicio no utilizable, candidatos/exclusiones, tokens estimados de notas, reviewers requeridos/omitidos y cierres anticipados. En modo sombra, se compara la propuesta del gate con el informe estructurado del reviewer. Un informe no estructurado se registra como no comparable; no se inventa una aprobación.

## Comparación reproducible

Datos de [system1_benchmark.json](system1_benchmark.json), producidos por [test_system1_benchmark.py](../tests/test_system1_benchmark.py). Se ejecutan el bucle, el ensamblador, las herramientas y el grafo reales con juicios y delegados guionizados. El caso de regresión escribe los archivos y ejecuta pytest. Esta prueba demuestra la política y su ahorro posible; no mide la precisión ni la latencia de Nimble/Laya.

| Escenario | Iteraciones antes → después | Tokens estimados antes → después | Reviewers antes → después | Fallbacks antes → después | Errores antes → después |
|---|---:|---:|---:|---:|---:|
| Bug corregido y regresión añadida | 5 → 4 | 1685 → 1260 | 0 → 0 | 0 → 0 | 0 → 0 |
| Lo mismo con salida de pytest verbosa (> 2000 caracteres) | 5 → 4 | 1709 → 1274 | 0 → 0 | 0 → 0 | 0 → 0 |
| Referencia implícita + notas de trading | 1 → 1 | 816 → 286 | 0 → 0 | 0 → 0 | 0 → 0 |
| Salida con evidencia y gate positivo | — | — | 1 → 0 | 0 → 0 | 0 → 0 |
| Broker no disponible | 1 → 1 | 817 → 817 | 0 → 0 | 0 → 1 | 0 → 0 |

Los tokens se estiman con `ceil(caracteres/4)` sobre todos los mensajes enviados al modelo principal; no son tokens facturados y excluyen el coste de los juicios. Los valores pueden variar ligeramente con la ruta del workspace. El escenario del gate usa un delegado guionizado y no ejecuta un LLM de subagente, por eso no informa iteraciones ni tokens de ese LLM.

Reproducir desde la raíz de Athena, con el entorno de desarrollo instalado:

```powershell
rtk proxy .venv\Scripts\python.exe tests/test_system1_benchmark.py --output docs/system1_benchmark.json
rtk proxy python -m pytest tests/test_system1.py tests/test_system1_broker.py tests/test_system1_integration.py tests/test_system1_benchmark.py -q --basetemp=.pytest-system1
```

Las pruebas cubren los tres casos obligatorios, el ejemplo de regresión ausente/presente, incertidumbre, modo sombra y flags desactivados; errores HTTP, transporte y contrato; score ordinal con cero; cancelación, cambio de objetivo durante el juicio, protección de memoria, límite de candidatos, revisiones obligatorias, denegación de permisos, ensamblaje del servicio y persistencia de métricas sin inputs.

Validación ejecutada:

- 85 pruebas de la implementación inicial aprobadas. La revisión posterior del contrato amplía la cobertura de capacidades de evaluación y de intentos rechazados en las tres funciones.
- Suite general de la integración inicial: 1188 aprobadas y 4 omitidas por ausencia de Tcl/Tk en Python 3.14.
- Después del último ajuste de criterios globales y evidencia abreviada: 131 pruebas de grafo, servicio e integración aprobadas; las 22 de gate y benchmark volvieron a pasar tras el último ajuste conservador.
- Ruff 0.16.4: lint y formato de `src` y `tests` aprobados. `git diff --check` aprobado.
- Mypy no se pudo ejecutar en ese entorno; se ejecutó en la revisión 0.3.0 (abajo) y entonces dio 43 errores, todos de este código.
- Revisión del Client_API del 3 de octubre: 126 pruebas de System-1 y del adaptador Broker, más 14 del escritorio, aprobadas. Incluyen la independencia de `system1_evaluation`, los rechazos `SELF_REPORTED_SCORE` y `MODEL_CAPABILITY_MISMATCH`, las notas de intentos rechazados en las tres funciones y la comprobación de conexión con `/auth/check`. Ruff y formato de los 200 archivos de `src` y `tests`, junto con `git diff --check`, aprobados.
- Revisión posterior de autenticación: 133 pruebas del adaptador Broker, System-1, escritorio, errores y selección de proveedores aprobadas. Cubren fallos al enviar y consultar una tarea, conservación del identificador sin cancelación ni reenvío, ausencia de fallback a otro modelo, códigos en eventos System-1 y el aviso que presenta la ventana. Otras 34 pruebas de bucle, recuperación y escritorio aprobadas; 4 pruebas de Tk omitidas por la misma ausencia de Tcl/Tk. Lint, formato y comprobación del diff aprobados.
- **Revisión 0.3.0 (3 de octubre de 2026).** Suite completa: 1258 aprobadas, sin omisiones. Mypy 1.18.2 (`uvx --python 3.11 --with pytest==8.4.2 --from mypy==1.18.2 mypy`): sin errores en 200 ficheros. Ruff 0.14.5: lint y formato aprobados. `git diff --check` aprobado. Los tests nuevos se comprobaron quitando cada arreglo: fallan sin él (salida verbosa del benchmark, lectura externalizada, declaración `mandatory_review: false`).
- **En vivo contra el AI_Broker 2.11 (192.168.1.52) el mismo día.** Un run real del servicio (`qwen3.8:27b`, tres flags activos, sombra desactivada) corrigió una división por cero y añadió su prueba de regresión; el checkpoint lo cerró con Nimble a 0,98 tras 3 llamadas al modelo. Sin la prueba de regresión, el juicio de objetivo no se acepta (`LOW_CONFIDENCE`). El gate omite la segunda revisión con una salida completa (0,998) y la mantiene con una parcial. Un token inválido conserva el flujo anterior con `ADMIN_AUTH_REQUIRED`. Las métricas del broker registran los tres `use_case` de Athena por separado. Con el objetivo «Revisa calc.py y corrige…» y `mandatory_review: false`, el checkpoint cerró el run tras 3 llamadas (100 s); el mismo encargo sin declaración no tuvo cierre anticipado (9 llamadas, 380 s). En ambos casos el trabajo era correcto.

## Archivos y alcance

Los módulos nuevos son `src/athena/system1.py` y `src/athena/adapters/system1_broker.py`. Las conexiones están en `agent_loop/`, `context.py`, `delegation.py`, `graph_executor.py`, `subagents.py`, `tool_executor.py`, `adapters/service/runs/`, `adapters/service/orchestration/orquestador.py`, `adapters/service/server/transporte.py` y `athena_service.py`. Eventos, registro y métricas amplían sus módulos existentes. Las cuatro suites nuevas están en `tests/test_system1*.py`.

El filtro se limita a memoria recuperada; no elimina historial de herramientas ni fragmentos de archivos. No se ha modificado el servicio Broker ni activado los flags de un despliegue; la evaluación en vivo de la revisión 0.3.0 está en la sección anterior. La activación depende de las capacidades y perfiles descritos en el Client_API actualizado.

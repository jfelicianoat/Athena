# System-1 en Athena

Implementación basada en `02_Athena_SYSTEM1.md` y en el [Client_API actualizado](../../docs/Client_API.md), sección 15. El código de Athena usa exclusivamente AI_Broker: el operador decide el orden de proveedores. El contrato actualizado describe Nimble local → Laya MCP → alternativa de la aplicación.

## Arquitectura y decisiones

- `system1.py` contiene la configuración, las peticiones y la política conservadora común. `adapters/system1_broker.py` reutiliza el transporte autenticado existente, comprueba `system1_judgments` y hace un POST síncrono a `/api/v1/system1/judge`. No crea tareas, sondea estados ni llama directamente a Ollama o Laya.
- El servicio comprueba capacidades al arrancar cuando hay alguna función habilitada. El descubrimiento tiene su propio plazo breve; si falla, el servicio sigue arrancando y vuelve a comprobarlas antes del primer juicio. Sin funciones habilitadas no hace llamadas System-1.
- `AgentLoop` usa la misma política determinista y los mismos hooks de verificación que antes. Tras un bloque con archivos modificados y un comando exitoso, puede cerrar con un juicio positivo de confianza ≥ 0,97. Requiere evidencia independiente de la respuesta final, ausencia de trabajo pendiente y un objetivo vigente. Un check fallido nunca puede ser reemplazado por un juicio positivo. Una revisión obligatoria impide este cierre anticipado.
- Al terminar normalmente o rescatar un run detenido, un juicio negativo aceptado exige completar los requisitos pendientes. Rechazo, baja confianza, timeout o fallo conservan la verificación y recuperación anteriores. El juicio recibe objetivo, criterios, estado, archivos modificados, salida, comprobaciones y pendientes; no recibe el repositorio completo.
- `ContextBuilder` filtra solamente notas opcionales recuperadas de memoria. Conserva instrucciones, petición, historial de herramientas, errores, estado, criterios y definiciones de herramientas. Los recuerdos confirmados por el usuario, restricciones y notas marcadas obligatorias se conservan completos, incluidas sus líneas de continuación. El máximo de candidatos limita llamadas; lo no puntuado permanece. Cualquier juicio fallido conserva todas las notas originales. El resultado se reutiliza mientras objetivo y notas no cambien.
- `GraphExecutor` y `DelegateTaskTool` colocan el gate antes de los verificadores existentes. La delegación directa conserva primero el control de permisos. Se requiere evidencia determinista, salida del ejecutor y criterios de aceptación. Revisiones expresas, operaciones sensibles/destructivas, inconsistencias de contratos, resultados incompletos, checks fallidos y pérdida de procedencia mantienen al reviewer. La verificación final del grafo permanece.
- La sensibilidad se deriva de los riesgos y niveles R0–R4 de PermissionEngine. Se propaga desde los delegados, incluidas continuaciones. System-1 nunca concede permisos ni evita una denegación.

## Compatibilidad con el Client_API revisado

Los valores predeterminados usan los perfiles ya documentados por el operador:

| Función Athena | `use_case` | Umbral local |
|---|---|---:|
| Objetivo completado | `goal_completion` | 0,97 |
| Relevancia del contexto | `ranking` | 0,85 de confianza |
| Omitir segunda revisión | `agora_review_gate` | 0,97 |

`agora_review_gate` es el nombre del perfil de revisión disponible; Athena envía sus propias instrucciones en cada petición. Para usar `athena_reviewer_gate` u otro nombre propio, el operador debe configurar previamente ese perfil. Un nombre desconocido devuelve `UNKNOWN_USE_CASE` y mantiene la revisión. Athena no pide el perfil `default` para abaratar el umbral del gate.

El DTO admite `threshold_profile` para consumidores que necesiten un caso propio con `"default"`, conforme al contrato. Las tres funciones integradas usan perfiles configurados. No envían campos no admitidos, modelos, timeouts de petición ni clasificación de riesgo al endpoint.

La detección de objetivo tiene tres resultados lógicos: `complete` (booleano positivo aceptado por encima del umbral), `incomplete` (negativo aceptado por encima del umbral) y `uncertain` (rechazo, error o confianza insuficiente). El modo sombra registra el juicio y conserva el flujo anterior.

Se distingue un `false` aceptado de un rechazo. Se acepta un resultado válido del segundo proveedor aunque `fallback_used` sea `true`. `accepted: false` siempre activa la alternativa, incluidos `UNKNOWN_USE_CASE`, `UNKNOWN_THRESHOLD_PROFILE`, `INPUT_TOO_LARGE` y códigos futuros. Athena no reintenta un rechazo ni busca una segunda opinión.

El score de contexto es un índice ordinal: 0 = irrelevante, 1 = posiblemente útil, 2 = relevante. Se traduce a la escala de relevancia local 0 / 0,65 / 1 para aplicar los umbrales configurados. No se interpreta como probabilidad. Tampoco la confianza top1 es una probabilidad calibrada. Puede exigirse calibración para omitir revisiones; con la instalación actual, que informa `false`, esa opción conserva todos los reviewers.

Todas las peticiones usan `cloud_allowed: false`. El cliente valida aceptación, eco del caso, booleanos, índices de rúbrica y confianza finita entre 0 y 1. Campos aditivos de capacidades y respuesta no rompen la compatibilidad.

La última revisión añade `target` para evaluar un proveedor/modelo concreto y notas en bruto dentro de `attempts`, incluso cuando se rechaza el juicio (§15.8). Las funciones operativas no envían `target`, para conservar la política de proveedores del operador. Athena toma decisiones únicamente del primer nivel con `accepted: true`; nunca promueve una nota de `attempts` a decisión de negocio. Una prueba específica comprueba que incluso un intento con nota alta no puede cerrar el objetivo si el resultado principal fue rechazado.

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
| `ATHENA_SYSTEM1_GOAL_USE_CASE` | `goal_completion` |
| `ATHENA_SYSTEM1_CONTEXT_USE_CASE` | `ranking` |
| `ATHENA_SYSTEM1_REVIEWER_USE_CASE` | `agora_review_gate` |

Los umbrales locales pueden endurecer los del Broker; no rebajan los umbrales del operador. El timeout HTTP de 75 segundos cubre los dos intentos secuenciales de 30 segundos del contrato actual. Si el operador cambia esos plazos, hay que ajustar la variable correspondiente.

El presupuesto de contexto es conservador: permite retirar candidatos de relevancia intermedia cuando falta espacio, pero nunca recorta información protegida o claramente relevante para cumplir un límite. Las referencias implícitas como «haz lo mismo con Athena» conservan los candidatos intermedios incluso con poco presupuesto.

Para observar las tres funciones, poner los tres flags a `true` y conservar `SHADOW_MODE=true`. Para aplicar las decisiones, poner `SHADOW_MODE=false`. Se reutilizan `ATHENA_BROKER_BASE_URL` y `ATHENA_BROKER_TOKEN`; las credenciales no se guardan en la telemetría.

El cliente HTTP de Athena admite además `options.acceptance_criteria` (lista de cadenas) y `options.mandatory_review` (booleano). Los valores se guardan con el run y llegan al bucle y al gate jerárquico, también al reanudar. El gate recibe el objetivo global y los criterios del usuario, del reviewer y de los ejecutores. Las tareas jerárquicas conservan sus propios criterios.

## Telemetría

Los eventos `system1.judged`, `system1.context`, `system1.review`, `system1.completion` y `system1.comparison` se guardan en el registro existente. Incluyen decisión, confianza, umbral, proveedor, modelo, plazo, razón y propuesta/aplicación cuando corresponde. No incluyen el input del juicio ni el prompt completo.

Las métricas del run contienen un objeto `system1`, persistido en una tabla adicional compatible con bases existentes: juicios, fallbacks por juicio no utilizable, candidatos/exclusiones, tokens estimados de notas, reviewers requeridos/omitidos y cierres anticipados. En modo sombra, se compara la propuesta del gate con el informe estructurado del reviewer. Un informe no estructurado se registra como no comparable; no se inventa una aprobación.

## Comparación reproducible

Datos de [system1_benchmark.json](system1_benchmark.json), producidos por [test_system1_benchmark.py](../tests/test_system1_benchmark.py). Se ejecutan el bucle, el ensamblador, las herramientas y el grafo reales con juicios y delegados guionizados. El caso de regresión escribe los archivos y ejecuta pytest. Esta prueba demuestra la política y su ahorro posible; no mide la precisión ni la latencia de Nimble/Laya.

| Escenario | Iteraciones antes → después | Tokens estimados antes → después | Reviewers antes → después | Fallbacks antes → después | Errores antes → después |
|---|---:|---:|---:|---:|---:|
| Bug corregido y regresión añadida | 5 → 4 | 1683 → 1259 | 0 → 0 | 0 → 0 | 0 → 0 |
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

- 85 pruebas nuevas aprobadas, incluida la compatibilidad con `attempts` de la última revisión del Client_API.
- Suite general: 1188 aprobadas y 4 omitidas por ausencia de Tcl/Tk en Python 3.14.
- Después del último ajuste de criterios globales y evidencia abreviada: 131 pruebas de grafo, servicio e integración aprobadas; las 22 de gate y benchmark volvieron a pasar tras el último ajuste conservador.
- Ruff 0.16.4: lint y formato de `src` y `tests` aprobados. `git diff --check` aprobado.
- Mypy no ejecutado: no está instalado en el entorno ni disponible en la caché; la descarga quedó bloqueada por las restricciones de red. La comprobación de tipos permanece pendiente.

## Archivos y alcance

Los módulos nuevos son `src/athena/system1.py` y `src/athena/adapters/system1_broker.py`. Las conexiones están en `agent_loop/`, `context.py`, `delegation.py`, `graph_executor.py`, `subagents.py`, `tool_executor.py`, `adapters/service/runs/`, `adapters/service/orchestration/orquestador.py`, `adapters/service/server/transporte.py` y `athena_service.py`. Eventos, registro y métricas amplían sus módulos existentes. Las cuatro suites nuevas están en `tests/test_system1*.py`.

El filtro se limita a memoria recuperada; no elimina historial de herramientas ni fragmentos de archivos. No se ha modificado el servicio Broker, activado los flags de un despliegue ni realizado una evaluación con un proveedor real. La activación depende de las capacidades y perfiles descritos en el Client_API actualizado.

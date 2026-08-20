\# Informe de ejecución para completar Athena y convertirla en un agente jerárquico plenamente integrado



\## 1. Propósito



Este informe está dirigido al programador responsable de Athena.



El objetivo no es definir una arquitectura futura ni dejar subsistemas “para más adelante”. El objetivo es \*\*integrar y completar ahora todas las capacidades ya construidas\*\*, cerrar las piezas incompletas y conseguir cuanto antes una versión de Athena que funcione realmente como agente jerárquico, recuperable, verificable, multi-proveedor y utilizable desde ChatyGPT y Telegram.



El trabajo puede ejecutarse por fases para reducir riesgo, pero \*\*todas las fases forman parte del alcance actual\*\*.



La situación actual es buena: Athena ya dispone de un runtime monoagente sólido, 401 pruebas, cuatro puertas de calidad verdes y una aceptación V0.1 PASS 15/15. También existen subsistemas avanzados ya implementados que todavía no forman parte del camino real de ejecución.



El principal objetivo ahora es transformar:



```text

muchos subsistemas correctamente implementados

```



en:



```text

un único sistema agéntico integrado de extremo a extremo

```



\---



\# 2. Principio arquitectónico



Athena debe mantener el principio que ha guiado su diseño:



> \*\*La inteligencia procede del modelo; la fiabilidad procede del runtime.\*\*



El estudio arquitectónico previo de Claude Code refuerza que las capacidades diferenciales de un buen agente de programación están en el runtime: AgentLoop, herramientas, permisos, estado estructurado, contexto, cancelación, verificación, recuperación, subagentes, eventos y control de resultados.



Athena debe conservar además su característica propia:



> \*\*Divide y vencerás.\*\*



Por tanto deben coexistir dos niveles:



```text

NIVEL MACRO

Planner

↓

TaskGraph

↓

GraphExecutor



NIVEL MICRO

AgentLoop

↓

Tools

↓

Verification

↓

Recovery

```



El `GraphExecutor` no debe sustituir el `AgentLoop`.



Debe utilizarlo.



\---



\# 3. Estado actual



Según la auditoría actual, Athena dispone de aproximadamente:



```text

23 ADRs



42 módulos core



\~13.900 líneas src



\~9.600 líneas tests



401 tests



4 puertas de calidad verdes



V0.1:

PASS 15/15

```



Se ha realizado además una prueba real contra:



```text

granite4.1:3b

```



sin necesidad de modificar el AgentLoop.



Esto confirma que la abstracción de proveedor funciona correctamente.



\---



\# 4. Componentes actualmente integrados



El runtime real monta actualmente:



```text

AgentLoop



ToolRegistry



PermissionEngine



ToolExecutor



Verification



Recovery



Persistence



Hooks



Skills



HTTP/SSE



ChatyGPT adapter



Telegram channel

```



Estos elementos están realmente cableados en el flujo principal.



\---



\# 5. Subsistemas construidos pero desconectados



El problema principal actual está aquí.



\## 5.1. Subagentes



Existe:



```text

subagents.py

```



con:



```text

Explorer

Coder

Verifier

```



pero actualmente:



```text

AgentLoop

&#x20;  X

subagents.py

```



No forman parte de una ejecución normal.



\---



\# 5.2. TaskManager



Existe:



```text

tasks.py

```



con gestión de tareas, presupuestos y procesos.



Pero:



```text

AgentLoop

&#x20;  X

TaskManager

```



El runtime no lo utiliza.



\---



\# 5.3. TaskGraph



Existe:



```text

planning.py

```



pero actualmente:



```text

Planner

→ TaskGraph

→ termina ahí

```



No existe un executor que consuma realmente el grafo.



\---



\# 5.4. ProjectMemory



Existe únicamente el contrato:



```text

ProjectMemory Protocol

```



sin implementación persistente.



Athena, por tanto, no aprende conocimiento estable entre sesiones.



\---



\# 5.5. Provider fallback



`RecoveryPolicy` puede producir:



```text

provider\_fallback

```



pero actualmente no existe un consumidor real de esa directiva.



Debe corregirse.



Una recovery action sin implementación efectiva debe considerarse un error arquitectónico.



\---



\# 6. Arquitectura objetivo inmediata



El runtime debe quedar finalmente así:



```text

&#x20;                      USER GOAL

&#x20;                          │

&#x20;                          ▼

&#x20;                      Planner

&#x20;                          │

&#x20;             ¿requiere descomposición?

&#x20;                  │               │

&#x20;                 NO              SÍ

&#x20;                  │               │

&#x20;                  │          TaskGraph

&#x20;                  │               │

&#x20;                  │         GraphExecutor

&#x20;                  │               │

&#x20;                  │        ready frontier

&#x20;                  │               │

&#x20;                  │         TaskManager

&#x20;                  │               │

&#x20;                  │       SubagentRunner

&#x20;                  │               │

&#x20;                  └──────────┬────┘

&#x20;                             ▼

&#x20;                         AgentLoop

&#x20;                             │

&#x20;                      ContextBuilder

&#x20;                             │

&#x20;                        ToolRuntime

&#x20;                             │

&#x20;                   PermissionEngine

&#x20;                             │

&#x20;                        Workspace

&#x20;                             │

&#x20;                      Verification

&#x20;                             │

&#x20;                       PASS / FAIL

&#x20;                             │

&#x20;                        Recovery

&#x20;                             │

&#x20;                             ▼

&#x20;                      Task Evidence

&#x20;                             │

&#x20;                             ▼

&#x20;                      GraphExecutor

&#x20;                             │

&#x20;                             ▼

&#x20;                     Goal Verification

&#x20;                             │

&#x20;                             ▼

&#x20;                          DONE

```



\---



\# 7. Regla fundamental



Todo lo que ya existe debe reutilizarse.



No se debe crear:



```text

SecondAgentLoop

GraphAgentLoop

SubagentLoop2

```



ni otro runtime paralelo.



Los subagentes deben ejecutar mediante el \*\*AgentLoop existente\*\*.



\---



\# 8. FASE 1 — Normalizar cancelación



Esta fase debe hacerse primero porque posteriormente habrá una jerarquía:



```text

Run

↓

Graph

↓

Task

↓

Subagent

↓

AgentLoop

↓

Tool

↓

Process

```



Si la semántica de cancelación no está limpia antes de introducir esa jerarquía, el problema se multiplica.



\## Objetivo



Convertir cancelación en un resultado operacional explícito, no en una variante de error.



Actualmente `ProcessCancelledError` tiene tratamientos especiales en diferentes puntos.



Debe existir una semántica uniforme.



\---



\## Modelo recomendado



```text

ExecutionOutcome



COMPLETED



FAILED



CANCELLED



TIMED\_OUT

```



`CANCELLED` no debe interpretarse como:



```text

FAILED

```



\---



\## Propagación



Debe existir propagación completa:



```text

cancel Run

&#x20;↓

GraphExecutor

&#x20;↓

TaskManager

&#x20;↓

SubagentRunner

&#x20;↓

AgentLoop

&#x20;↓

ModelProvider

&#x20;↓

ToolExecutor

&#x20;↓

Process

```



\---



\## Scopes



Implementar:



```text

CancellationScope



TASK



SUBGRAPH



RUN

```



Ejemplo:



```text

&#x20;     A

&#x20;    / \\

&#x20;   B   C

&#x20;   │   │

&#x20;   D   E

```



Cancelar:



```text

B

```



debe producir:



```text

B cancelled



D blocked/cancelled



C continúa



E continúa

```



Cancelar el Run:



```text

A B C D E

→ cancellation cascade

```



\---



\## Tests obligatorios



\* cancelar model call;

\* cancelar Tool;

\* cancelar Bash;

\* cancelar Task;

\* cancelar Subgraph;

\* cancelar Run;

\* ningún proceso huérfano;

\* ninguna Task queda falsamente `running`;

\* reiniciar tras cancelación mantiene estado consistente.



\---



\# 9. FASE 2 — Implementar GraphExecutor



Éste es el desarrollo de mayor importancia.



Debe convertir `planning.py`, `tasks.py` y `subagents.py` en parte real del runtime.



\---



\# 10. Responsabilidad del GraphExecutor



`GraphExecutor` debe:



```text

1\. recibir un TaskGraph validado;



2\. consultar ready();



3\. obtener el frente ejecutable;



4\. comprobar dependencias;



5\. comprobar conflictos;



6\. asignar presupuesto;



7\. seleccionar role;



8\. seleccionar toolset;



9\. construir contexto mínimo;



10\. enviar Task a TaskManager;



11\. TaskManager ejecuta mediante SubagentRunner;



12\. SubagentRunner utiliza AgentLoop;



13\. recoger resultado;



14\. obtener VerificationEvidence;



15\. actualizar TaskGraph;



16\. desbloquear nuevas Tasks;



17\. reintentar/replanificar cuando corresponda;



18\. finalizar al verificarse el Goal.

```



\---



\# 11. No toda petición debe crear TaskGraph



Debe preservarse un camino rápido.



\## Objetivo sencillo



```text

Goal

&#x20;↓

AgentLoop

&#x20;↓

Tools

&#x20;↓

Verify

&#x20;↓

Done

```



\## Objetivo complejo



```text

Goal

&#x20;↓

Planner

&#x20;↓

TaskGraph

&#x20;↓

GraphExecutor

&#x20;↓

multiple Tasks

```



Debe existir un criterio de descomposición.



\---



\# 12. Cuándo dividir



Dividir cuando exista alguna de estas señales:



```text

varios outputs independientes



dependencias entre pasos



trabajo paralelizable



varios subsistemas



varios ficheros importantes



investigación + implementación



alta incertidumbre



roles diferentes



verificaciones independientes

```



No dividir cuando hacerlo solo produzca microtareas artificiales.



\---



\# 13. Atomicidad de una Task



Una tarea puede considerarse suficientemente pequeña cuando:



```text

1 objetivo claro



inputs identificables



output concreto



criterios de aceptación claros



verificación posible



sin decisiones estratégicas importantes pendientes

```



\---



\# 14. Workspace de los subagentes



Inicialmente no introducir worktrees automáticamente.



Utilizar:



```text

shared workspace

\+

serialización de writes

```



\---



\## Explorer



```text

workspace completo



READ ONLY

```



Tools típicas:



```text

Glob

Grep

Read

GitStatus

GitDiff

GitLog

```



\---



\## Coder



```text

workspace completo



WRITE sujeto a PermissionEngine

```



Tools:



```text

Read

Edit

Write

Bash

Git status/diff

```



\---



\## Verifier



Por defecto:



```text

READ ONLY

```



con capacidad de ejecutar:



```text

build

tests

lint

Git diff

```



No debe modificar código salvo workflow explícito de rework.



\---



\# 15. Concurrencia



Inicialmente:



```text

READ operations

→ parallel

```



Mientras:



```text

WRITE operations

→ serialized

```



En particular:



```text

Edit

Write

Git mutation

```



no deben ejecutarse simultáneamente sobre el mismo workspace.



Posteriormente pueden utilizarse worktrees, pero no son necesarios para completar Athena ahora.



\---



\# 16. Evidencia de subagentes



No aceptar:



```text

"he terminado"

```



como resultado.



Implementar salida estructurada.



Ejemplo:



```text

SubagentResult



task\_id



role



status



summary



artifacts



files\_examined



files\_changed



commands\_run



tool\_evidence



verification\_evidence



facts



assumptions



risks



unresolved



result\_references

```



\---



\# 17. Composición de evidencia



El padre no debe recibir todo el historial del hijo.



Debe recibir solamente:



```text

summary



verified facts



artifacts



changes



verification evidence



risks



unresolved items

```



Esto mantiene el contexto limpio.



\---



\# 18. Verificación jerárquica



Debe existir:



```text

Task Verification

```



y posteriormente:



```text

Goal Verification

```



Una Task puede pasar:



```text

Task PASS

```



sin que esto implique automáticamente:



```text

Goal PASS

```



Al terminar todas las Tasks:



```text

GraphExecutor

&#x20;↓

GoalVerification

```



debe comprobar el resultado completo.



\---



\# 19. FASE 3 — Tool de delegación



Debe existir una Tool mediante la cual el agente pueda solicitar una delegación.



Nombre recomendado:



```text

delegate\_task

```



y no:



```text

spawn\_agent

```



porque la unidad conceptual de Athena debe seguir siendo la Task.



\---



\# 20. Contrato conceptual



```text

delegate\_task



goal



task\_id opcional



role



context\_refs



expected\_output



acceptance\_criteria



toolsets



budget



timeout

```



El modelo no decide infraestructura.



Athena decide cómo ejecutar la delegación.



\---



\# 21. PermissionEngine y delegación



Delegar también debe pasar por PermissionEngine.



El riesgo no depende de que exista un subagente, sino de las capacidades que recibe.



\---



\## Ejemplo



```text

Explorer read-only

→ ALLOW

```



```text

Coder workspace local

→ policy

```



```text

subagent con escritura externa

→ ASK

```



```text

subagent con deploy

→ ASK/DENY

```



\---



\# 22. Regla de seguridad crítica



Debe ser matemáticamente cierta:



```text

child\_permissions

⊆

parent\_permissions

```



Y:



```text

child\_tools

⊆

tools permitidas por Task + Parent Policy

```



Un subagente jamás puede escalar permisos.



\---



\# 23. FASE 4 — ProviderRouter y fallback



Debe eliminarse la directiva muerta actual.



Si `RecoveryPolicy` produce:



```text

provider\_fallback

```



debe existir un componente que la consuma.



\---



\# 24. No duplicar AI\_Broker



Athena no debe convertirse en otro broker de modelos.



El router de Athena opera a nivel de \*\*provider\*\*, no necesariamente de modelo.



Ejemplo:



```text

Primary provider:

AI\_Broker



Fallback:

OpenAI-compatible endpoint

```



No:



```text

Qwen

→ DeepSeek

→ Claude

```



si AI\_Broker ya realiza ese routing.



\---



\# 25. ProviderRouter mínimo



Implementar:



```text

ProviderRegistry



ProviderRouter

```



con:



```text

primary



fallback\[]

```



\---



\## Flujo



```text

ModelProvider primary

&#x20;↓

ModelTransient/Permanent failure

&#x20;↓

RecoveryPolicy

&#x20;↓

provider\_fallback

&#x20;↓

ProviderRouter

&#x20;↓

next provider

```



\---



\# 26. Restricción



AgentLoop sigue hablando únicamente con:



```text

ModelProvider abstraction

```



No debe conocer el router concreto.



\---



\# 27. FASE 5 — ProjectMemory real



Debe implementarse ahora para que Athena aprenda entre sesiones.



No debe existir memoria automática descontrolada.



\---



\# 28. Tipos de memoria



Como mínimo:



```text

architecture\_decision



project\_convention



verified\_command



known\_constraint



domain\_fact



user\_confirmed\_fact



environment\_fact

```



\---



\# 29. Modelo sugerido



```text

ProjectMemoryItem



id



project\_id



kind



content



source



source\_reference



created\_at



updated\_at



confidence



verification\_state



scope



supersedes



status

```



\---



\# 30. Operaciones



Implementar explícitamente:



```text

propose\_memory



approve\_memory



update\_memory



forget\_memory



search\_memory

```



\---



\# 31. Escritura automática



No debe escribirse automáticamente una conclusión LLM como verdad.



Como mínimo diferenciar:



```text

proposed



verified



user\_confirmed

```



\---



\# 32. SQLite



Implementar `SQLiteProjectMemory`.



Debe integrarse con:



```text

ContextBuilder

```



pero mediante retrieval selectivo.



Nunca cargar toda la memoria.



\---



\# 33. Frescura



Cada MemoryItem debe permitir saber:



```text

cuándo se creó



de dónde procede



si ha sido sustituido



si sigue vigente

```



La memoria es una pista.



El estado real actual del repositorio sigue siendo la fuente de verdad.



\---



\# 34. FASE 6 — Métricas y observabilidad



Debe hacerse ahora porque Athena ya está en una etapa en la que conviene medir su comportamiento.



Además permitirá obtener datos para el TFM.



No se necesita Prometheus inicialmente.



El EventBus existente puede alimentar una tabla SQLite.



\---



\# 35. RunMetrics



Implementar como mínimo:



```text

run\_id



started\_at



completed\_at



duration\_ms



status



model\_calls



tool\_calls



input\_tokens



output\_tokens



estimated\_cost



tasks\_total



tasks\_completed



tasks\_failed



repair\_cycles



permission\_requests



permission\_denials



verification\_runs



verification\_failures



subagents\_spawned



provider\_failures



cancellations

```



\---



\# 36. Métricas agregadas



Debe poder calcularse:



```text

success\_rate



first\_pass\_success\_rate



mean\_run\_duration



mean\_tool\_calls



mean\_model\_calls



mean\_repair\_cycles



verification\_failure\_rate



provider\_failure\_rate



intervention\_rate



subagent\_usage

```



\---



\# 37. Datos especialmente útiles para el TFM



Poder comparar:



```text

monoagent

vs

hierarchical Athena

```



y:



```text

small LLM

vs

larger LLM

```



mediante métricas como:



```text

éxito



tiempo



tokens



reparaciones



tool calls



intervenciones humanas

```



Esto puede convertirse en evidencia experimental del valor de la arquitectura.



\---



\# 38. FASE 7 — Mejorar Verification



No dejar las limitaciones actuales sin resolver.



Deben abordarse dentro del presente roadmap.



\---



\# 39. INCONCLUSIVE real



Actualmente debe ampliarse.



Debe poder producirse cuando:



```text

no existe herramienta de verificación



falta dependencia



entorno incompleto



resultado ambiguo



verificación parcial



servicio externo indisponible

```



\---



\# 40. FailureDiagnosis



Añadir una fase:



```text

VerificationFailure

&#x20;↓

FailureDiagnosis

&#x20;↓

RepairStrategy

&#x20;↓

AgentLoop

```



El diagnóstico debe intentar clasificar:



```text

code\_error



test\_error



environment\_error



dependency\_error



preexisting\_failure



tool\_failure



insufficient\_evidence

```



\---



\# 41. Repair Loop informado



En lugar de:



```text

falló

→ intenta otra vez

```



debe ser:



```text

falló



diagnóstico



evidencia



hipótesis



acción de reparación



nueva verificación

```



\---



\# 42. Anti-cheating



Mantener los mecanismos actuales, pero mejorar detección de:



```text

tests eliminados



asserts debilitados



tests skip



configuración desactivada



lint deshabilitado



acceptance criteria cambiados

```



No es necesario construir un verificador formal perfecto, pero sí mejorar los casos comunes.



\---



\# 43. FASE 8 — Completar ChatyGPT



La integración existe, pero debe cerrarse.



\---



\# 44. Last-Event-ID



El cliente Rust debe utilizar:



```text

Last-Event-ID

```



para reaprovechar la capacidad de reanudación SSE del servidor.



\---



\# 45. Reconnection



Flujo:



```text

SSE connection drops



ChatyGPT stores last\_event\_id



reconnect



Last-Event-ID sent



Athena resumes stream



UI reconstructs current state

```



\---



\# 46. Limpiar deuda



Eliminar:



```text

\#!\[allow(dead\_code, unused\_imports)]

```



y resolver realmente warnings/imports.



\---



\# 47. Configuración Athena



Tomar una decisión explícita.



Recomiendo:



```text

ChatyGPT configuration

↓

Athena connection settings

```



para:



```text

URL



auth



connection mode



display preferences

```



Pero las configuraciones internas de Athena deben permanecer en Athena.



\---



\# 48. UI de ejecución jerárquica



Ahora sí debe hacerse la vista cenital.



Debe representar información real del GraphExecutor.



Ejemplo:



```text

&#x20;                ATHENA

&#x20;                  │

&#x20;      ┌───────────┼───────────┐

&#x20;      ▼           ▼           ▼

&#x20;  Explorer      Coder      Verifier

&#x20;     ✓            ▶           ○

```



y/o:



```text

TaskGraph



T01  ✓

&#x20;│

&#x20;├── T02  ✓

&#x20;├── T03  ▶

&#x20;│     │

&#x20;│     └── T05 blocked

&#x20;│

&#x20;└── T04  ✓

```



\---



\# 49. Información útil en UI



Mostrar:



```text

Goal



plan



Task activa



roles activos



tools usadas



permiso pendiente



verification



retries



artifacts



errores



progreso



coste/tokens si están disponibles

```



No mostrar chain-of-thought.



\---



\# 50. FASE 9 — Telegram completo



Telegram ya existe, pero debe integrarse también con ejecución jerárquica.



Debe poder:



```text

crear Goal



consultar run



consultar TaskGraph



cancelar Task/Run



aprobar permisos



recibir fallos



recibir finalización

```



\---



\# 51. Eventos agregados



No enviar un mensaje por cada tool.



Enviar:



```text

run iniciado



plan preparado



fase importante



permiso solicitado



Task relevante falló



replanning



verification



run completado

```



\---



\# 52. Continuidad multicanal



Un Run pertenece a Athena.



No a ChatyGPT.



No a Telegram.



Por tanto:



```text

ChatyGPT

&#x20;  │

&#x20;  ├─────────────┐

&#x20;  ▼             │

Athena Run       │

&#x20;  ▲             │

&#x20;  └─────────────┤

&#x20;             Telegram

```



El mismo run debe ser operable desde ambos canales si la identidad está vinculada.



\---



\# 53. FASE 10 — Worktrees y paralelismo real de escritura



Esto también debe formar parte del plan actual, pero debe implementarse \*\*después\*\* de que GraphExecutor funcione correctamente sobre workspace compartido.



No porque sea “para algún día”, sino porque introducirlo antes haría mucho más difícil depurar GraphExecutor.



\---



\# 54. Objetivo



Permitir:



```text

Coder A

\+

Coder B

```



trabajando en paralelo sin colisionar.



\---



\# 55. Estrategia



Utilizar:



```text

Git worktree

```



por Task/subgrafo de escritura.



Ejemplo:



```text

Main repository



├── worktree/task-T03

└── worktree/task-T04

```



\---



\# 56. Integración



```text

GraphExecutor

&#x20;↓

WorkspaceIsolationPolicy

&#x20;↓

shared/read-only

o

isolated worktree

```



\---



\# 57. Merge de resultados



No realizar merge automático a ciegas.



Debe existir:



```text

IntegrationTask

```



con:



```text

diff comparison



conflict detection



tests



verification

```



\---



\# 58. FASE 11 — Checkpoints y rollback



Implementar ahora como parte de completar la ejecución avanzada.



Antes de cambios de alto riesgo:



```text

checkpoint

```



Puede implementarse inicialmente mediante estado Git/local snapshot.



\---



\# 59. Rollback



Debe poder revertir:



```text

Task



Subgraph



Run

```



cuando las modificaciones sean atribuibles a Athena.



No utilizar:



```text

git reset --hard

```



sobre trabajo preexistente sin protección y autorización.



\---



\# 60. FASE 12 — Cierre de Athena completa



Athena debe considerarse funcionalmente completa cuando se cumpla el siguiente flujo:



```text

User Goal

&#x20;↓

Goal analysis

&#x20;↓

simple?

&#x20;├─ yes → AgentLoop

&#x20;└─ no

&#x20;     ↓

&#x20;  Planner

&#x20;     ↓

&#x20;  TaskGraph

&#x20;     ↓

&#x20;GraphExecutor

&#x20;     ↓

&#x20;ready frontier

&#x20;     ↓

&#x20;TaskManager

&#x20;     ↓

&#x20;Subagents

&#x20;     ↓

&#x20;AgentLoop

&#x20;     ↓

&#x20;Tools

&#x20;     ↓

&#x20;Verification

&#x20;     ↓

&#x20;Evidence

&#x20;     ↓

&#x20;TaskGraph update

&#x20;     ↓

&#x20;Replan if needed

&#x20;     ↓

&#x20;Goal verification

&#x20;     ↓

&#x20;Result

```



\---



\# 61. Definition of Done de Athena jerárquica



No declarar completado hasta que todas estas pruebas estén verdes.



\## Runtime



\* \[ ] AgentLoop anterior sigue funcionando.

\* \[ ] Simple Goal puede evitar TaskGraph.

\* \[ ] Goal complejo crea TaskGraph.

\* \[ ] TaskGraph se ejecuta realmente.

\* \[ ] `ready()` gobierna la ejecución.

\* \[ ] dependencias se respetan.

\* \[ ] fan-in funciona.

\* \[ ] fan-out funciona.

\* \[ ] ciclos siguen rechazándose.



\## Subagentes



\* \[ ] Explorer se usa realmente.

\* \[ ] Coder se usa realmente.

\* \[ ] Verifier se usa realmente.

\* \[ ] contextos están aislados.

\* \[ ] permisos hijos no superan permisos padre.

\* \[ ] budgets se respetan.

\* \[ ] timeout funciona.

\* \[ ] resultados son estructurados.



\## Concurrencia



\* \[ ] lecturas paralelas.

\* \[ ] writes conflictivos serializados.

\* \[ ] worktrees disponibles para writes paralelos.

\* \[ ] conflictos detectados.

\* \[ ] integración verificada.



\## Cancelación



\* \[ ] Task.

\* \[ ] Subgraph.

\* \[ ] Run.

\* \[ ] model call.

\* \[ ] tool.

\* \[ ] Bash.

\* \[ ] subagent.

\* \[ ] background process.

\* \[ ] cero huérfanos.



\## Verification



\* \[ ] Task verification.

\* \[ ] Goal verification.

\* \[ ] baseline attribution.

\* \[ ] FailureDiagnosis.

\* \[ ] informed repair.

\* \[ ] INCONCLUSIVE real.

\* \[ ] anti-cheating.



\## Recovery



\* \[ ] restart con Graph activo.

\* \[ ] restart con child activo.

\* \[ ] running pasa correctamente a recovery.

\* \[ ] tareas completadas no se repiten indebidamente.

\* \[ ] eventos pueden recuperarse.



\## Providers



\* \[ ] primary provider.

\* \[ ] fallback provider.

\* \[ ] direct provider intercambiable.

\* \[ ] AI\_Broker adapter.

\* \[ ] cambio provider sin cambiar AgentLoop.



\## Memory



\* \[ ] ProjectMemory SQLite.

\* \[ ] memories trazables.

\* \[ ] retrieval selectivo.

\* \[ ] no auto-memoria no verificada.

\* \[ ] supersedes.

\* \[ ] forget.



\## Metrics



\* \[ ] runs.

\* \[ ] tasks.

\* \[ ] tokens.

\* \[ ] tools.

\* \[ ] reparaciones.

\* \[ ] errores.

\* \[ ] verificaciones.

\* \[ ] permisos.

\* \[ ] subagentes.

\* \[ ] proveedores.



\## ChatyGPT



\* \[ ] SSE resume.

\* \[ ] Last-Event-ID.

\* \[ ] UI del TaskGraph.

\* \[ ] agentes visibles.

\* \[ ] permisos.

\* \[ ] cancelación.

\* \[ ] recovery.



\## Telegram



\* \[ ] Goal.

\* \[ ] status.

\* \[ ] TaskGraph.

\* \[ ] cancel.

\* \[ ] approve/reject.

\* \[ ] final result.

\* \[ ] misma identidad/run que ChatyGPT.



\---



\# 62. Orden de implementación recomendado



Ejecutar estrictamente en este orden:



```text

P1  Cancelación semántica

&#x20;↓

P2  GraphExecutor

&#x20;↓

P3  delegate\_task

&#x20;↓

P4  ProviderRouter/fallback

&#x20;↓

P5  ProjectMemory SQLite

&#x20;↓

P6  Metrics

&#x20;↓

P7  Verification diagnosis

&#x20;↓

P8  ChatyGPT debt + hierarchical UI

&#x20;↓

P9  Telegram hierarchical control

&#x20;↓

P10 Worktree isolation

&#x20;↓

P11 Checkpoints/rollback

&#x20;↓

P12 Full acceptance suite

```



\---



\# 63. Regla de desarrollo por fases



Las fases no significan “esto queda para el futuro”.



Significan:



> \*\*Una fase debe quedar verde antes de introducir la siguiente.\*\*



Cada fase debe comenzar:



```text

1\. leer ADRs;



2\. leer AGENTS.md;



3\. ejecutar toda la suite actual;



4\. confirmar baseline verde;



5\. implementar exclusivamente el alcance;



6\. añadir tests;



7\. ejecutar tests nuevos;



8\. ejecutar regresión completa;



9\. lint;



10\. typecheck;



11\. revisar diff;



12\. documentar arquitectura;



13\. solo entonces avanzar.

```



\---



\# 64. Prohibiciones para el desarrollo



No introducir:



```text

nuevo AgentLoop



nuevo ToolRuntime paralelo



nuevo sistema de permisos



segundo TaskManager



segunda persistencia de Runs



duplicación de estado ChatyGPT/Athena



routing de modelos duplicando AI\_Broker



memoria automática no trazable



swarm genérico



autorización decidida por LLM



writes paralelos sobre workspace compartido



subagentes con permisos superiores al padre



DONE sin Verification

```



\---



\# 65. Versionado recomendado



El estado actual debe permanecer:



```text

Athena V0.1

```



como baseline aceptado.



El siguiente release:



```text

Athena V0.2

Hierarchical Execution

```



debe incorporar:



```text

GraphExecutor



TaskManager integrado



Subagents integrados



delegate\_task



cancelación jerárquica

```



Posteriormente, sin dejar el desarrollo actual:



```text

Athena V0.3

Persistent Intelligence



ProjectMemory

Provider fallback

Metrics

FailureDiagnosis

```



Después:



```text

Athena V0.4

Parallel Execution



worktrees

checkpoints

parallel writers

integration

rollback

```



Y finalmente:



```text

Athena V1.0

Integrated Agent Runtime

```



con:



```text

ChatyGPT



Telegram



multi-provider



hierarchical execution



memory



metrics



verification



parallel execution



recovery



human approval

```



\---



\# 66. Resultado esperado



El desarrollo no debe terminar simplemente con todos los módulos implementados.



Debe existir una prueba real como ésta:



```text

Objetivo:



"Analiza este repositorio, identifica el error que provoca el fallo

de autenticación, corrígelo y demuestra que la solución funciona."

```



Athena debe:



```text

1\. analizar complejidad;



2\. crear TaskGraph;



3\. delegar investigación a Explorer;



4\. recibir evidence;



5\. crear/activar Task de implementación;



6\. Coder modifica código;



7\. Verifier ejecuta tests;



8\. falla un test;



9\. FailureDiagnosis clasifica el problema;



10\. GraphExecutor devuelve trabajo al Coder;



11\. Coder corrige;



12\. Verifier vuelve a ejecutar;



13\. PASS;



14\. GoalVerifier revisa integración;



15\. genera diff;



16\. almacena métricas;



17\. actualiza memoria explícita si procede;



18\. informa al usuario.

```



Mientras:



```text

ChatyGPT

```



muestra el proceso.



Y:



```text

Telegram

```



puede consultar:



```text

/status



/tasks

```



o cancelar/aprobar.



\---



\# 67. Objetivo final del proyecto



Athena no debe terminar siendo:



```text

un clon de Claude Code

```



ni:



```text

un agente exclusivamente programador

```



El enfoque aprendido del análisis de Claude Code debe utilizarse como patrón arquitectónico general. El informe de referencia precisamente concluye que herramientas, permisos, estado, verificación, eventos, cancelación y recuperación deben pertenecer al runtime, no al dominio específico de programación.



Por ello, la arquitectura final debe permitir:



```text

Athena Core

│

├── Developer

├── Financial Analyst

├── Researcher

├── Data Analyst

├── System Analyst

└── futuras especializaciones

```



sin reescribir:



```text

AgentLoop



GraphExecutor



ToolRuntime



PermissionEngine



EventBus



Memory framework



TaskManager



Verification framework



Recovery



ModelProvider

```



Lo que cambiará entre especializaciones será principalmente:



```text

Tools



Skills



Context contributors



Verification policies



Roles



Permission policies



Domain memory



Task decomposition strategies

```



\---



\# 68. Directriz final al programador



A partir de este punto:



> \*\*No construir más piezas aisladas.\*\*



Cada nuevo subsistema debe terminar conectado al camino real de ejecución antes de considerar el hito completado.



La prioridad absoluta es convertir:



```text

planning.py



tasks.py



subagents.py

```



de bibliotecas disponibles en:



```text

runtime activo

```



mediante:



```text

GraphExecutor

```



Después deben cerrarse, dentro del mismo ciclo de desarrollo actual:



```text

provider fallback



ProjectMemory



metrics



verification diagnosis



ChatyGPT



Telegram



parallel workspaces



checkpoints

```



El objetivo no es obtener cuanto antes más módulos.



El objetivo es obtener cuanto antes \*\*Athena funcionando de verdad como un único sistema coherente\*\*.




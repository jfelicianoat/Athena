# Integración DeepSeek — informe de cierre

- Fecha: 2026-08-22
- Punto de partida: `docs/DEEPSEEK_INTEGRATION_BASELINE.md` (auditoría de fase 0, commit `7eccb33`)
- Estado de la suite al cerrar: **952 pruebas**, `ruff check`, `ruff format --check`,
  `mypy --strict` sobre 148 ficheros
- ADR nuevos: 025 – 032. ADR enmendados: 003, 010, 015, 019, 026

## Qué se hizo

Diecisiete fases, cada una con su verificación contra el broker real cuando la fase
afirmaba algo sobre el comportamiento de un run.

| Fase | Qué añadió | ADR |
| --- | --- | --- |
| 0 | Auditoría: 18 conceptos clasificados con evidencia | — |
| 1 | GraphExecutor + concurrencia real en `_execute_calls` | 023 |
| 2 | Costura de subagentes (`SubagentProvider`, `Delegator`) | — |
| 3 | Capacidades exigidas; visibilidad ≠ autoridad | — |
| 4 | `delegate_task`: el modelo puede pedir un especialista | — |
| 5 | Contrato de salida que obliga; una verdad y dos proyecciones | 026 |
| 6 | Registro duradero con procedencia | 025 |
| 7 | Revisión de objetivo con concurrencia optimista | 029 |
| 8 | Perfiles: Athena fuera del código | 028 |
| 9 | Delegados continuables dentro de su presupuesto | 030 |
| 10 | Respaldo de proveedor que exige antes de gastar | — |
| 11 | Memoria de proyecto que se escribe y caduca | 031 |
| 12 | Métricas comparadas por estrategia | — |
| 13 | «No verificado» ≠ «verificado mal» | 027 |
| 14 | ChatyGPT: la interfaz deja de llamar fallido a lo no comprobado | — |
| 15 | Telegram arrancable, dentro del servicio que posee los runs | 019 |
| 16 | Deshacer se pide, y sólo alcanza lo que el run escribió | 032 |
| 17 | Consolidación y los diez escenarios de aceptación | — |

## El patrón que se repitió, y que es el hallazgo principal

La auditoría de fase 0 lo nombró: **subsistemas construidos, probados, exportados y
conectados a nada**. No fue una observación puntual. Fue lo que apareció, una y otra vez, en
casi todas las fases:

- **`InconclusiveReason`** existía con un docstring que explicaba exactamente la distinción
  que la fase 13 vino a implementar. Nada lo llamaba.
- **`rollback.py`** —un módulo entero sobre cuándo copiar y qué se puede deshacer— no lo
  importaba ningún otro fichero. Su propio docstring decía que `CheckpointStore` llevaba sin
  usarse desde H2.
- **La memoria de proyecto** se leía en cada run y no se escribía nunca. `remember_command`
  no tenía llamante; `approve`, `forget` e `is_stale` eran inalcanzables desde fuera.
- **Telegram** traía su cableado escrito como fragmento en un docstring y ningún punto de
  entrada.
- **Todos los `output_schema`** decían `{"type": "object"}`: un contrato que no se puede
  incumplir, y por eso siempre pasaba.
- **`SubagentCapabilities.continuation`** estaba declarada `false` con un comentario honesto
  —«no existe, y declararlo sería un deseo»— hasta que la fase 9 la hizo cierta.

La lección operativa, ya en la memoria del proyecto: **antes de dar por vivo un subsistema,
grepear el camino del servicio, no sólo sus pruebas.** Una suite verde no distingue entre
«funciona» y «nadie lo llama».

## Lo que sólo encontró un modelo de verdad

Cinco defectos que las pruebas no podían ver, porque un proveedor guionizado responde al
instante y siempre bien:

1. **`delegate_task` no podía funcionar nunca.** `ToolExecutor` aplicaba un techo de 30 s a
   toda tool; una delegación es un bucle entero. Se cortaba siempre y el fallo se atribuía
   al delegado, no al reloj de quien llamaba. (`ToolSpec.timeout_seconds`, fase 9.)
2. **Una delegación exitosa devolvía «(sin resultados)» al modelo.** La proyección por
   defecto elige la primera lista del resultado —`files_changed`, vacía en un explorer— como
   lo enumerable. (`DelegateTaskTool.project()`, fase 9.)
3. **`read_range` declaraba devolver cadenas y devuelve objetos.** 835 pruebas verdes; todas
   preguntaban qué hace la tool y ninguna si eso es lo que dijo que haría.
   (`tests/test_tool_output_schemas.py`, fase 5.)
4. **`goal.revised` no era duradero.** La revisión se aplicaba, se publicaba y no quedaba en
   el registro: la historia guardada contaba un trabajo que no encajaba con su objetivo
   inicial y no decía por qué. (Fase 7.)
5. **El checkpoint no llegaba a las escrituras.** Enganchado al principio de una tarea, con
   los ficheros que el plan nombraba, no copiaba nada — un plan real no los nombra. Y
   enganchado sólo al bucle padre tampoco, porque en un run jerárquico todas las escrituras
   son del hijo. (Fase 16, dos intentos.)

## El criterio que ordenó las decisiones

Casi todas las fases acabaron decidiendo lo mismo por caminos distintos: **es peor informar
mal que fallar.** Un runtime que se cae se arregla; uno que cuenta mal lo que hizo se
arregla cuando alguien se da cuenta, que puede ser nunca.

De ahí salen, y no de un principio de diseño abstracto:

- un run que no se pudo verificar no se cuenta como un run que falló;
- un perfil puede declarar evidencia más débil, pero no puede callárselo;
- un recuerdo viejo se etiqueta en vez de tirarse o darse por bueno;
- un respaldo que no cumple se rechaza en vez de degradar en silencio;
- un rollback deja intacto lo que el run no escribió, y lo dice;
- una lista que se enseña recortada lleva el total aparte del recorte;
- lo que se le recorta al modelo no se le recorta al registro.

## Lo que queda

**ChatyGPT.** La fase 14 llevó al cliente sólo la distinción de la fase 13, que era la que
engañaba a una persona. Le falta consumir lo que Athena ya publica: `display` de
`tool.completed` (ADR-026), un control de revisión de objetivo con su 409 (ADR-029), los
seguimientos de delegado (ADR-030), un panel de memoria (ADR-031), el selector de perfil
(ADR-028) y `/v1/runs/{id}/history` (ADR-025). Nada de eso lo bloquea Athena: los campos
están, y un cliente ignora lo que no conoce (ADR-024).

**Worktrees.** `workspaces.py` sigue declarando la estrategia y rechazándola claramente. El
disparador para implementarla sigue sin cumplirse: haría falta evidencia de que dos tareas
que escriben necesitan correr a la vez, y hoy las escrituras se serializan.

**Reanudar un run jerárquico interrumpido a mitad de tarea.** `resume` se sigue negando y
nombra las tareas cuyo resultado desconoce. Ninguna interfaz ofrece todavía forma de
registrar esa decisión.

## Los diez escenarios

`tests/test_acceptance_deepseek.py`. **No vienen del prompt maestro** —esa lista no está en
el repositorio— sino que se derivan de lo que las fases construyeron: uno por propiedad que
alguien podría creerse mal si dejara de cumplirse. Corren con proveedores guionizados para
que pasen en cada suite; los que además se comprobaron contra el broker lo dicen en su
docstring, porque «probado con un modelo de mentira» y «probado con uno de verdad» no son la
misma afirmación.

## Puerta

- Suite Athena: **952 pruebas verdes**, lint, formato y tipos limpios.
- ChatyGPT: 277 Rust + 243 vitest, clippy y tsc limpios.
- Verificación contra el broker real (`qwen3.8:27b`): fases 5, 6, 7, 8, 9, 11, 12, 13, 15 y
  16.

**PASS.**

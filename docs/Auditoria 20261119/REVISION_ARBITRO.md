# Revisión adversarial de la auditoría — 19-nov-2026

**Modalidad:** árbitro independiente: un agente separado que no compartía el contexto del
auditor. Recibió las instrucciones, el informe, las pruebas, las evidencias y el código,
con la orden de no modificar nada y de aislar `AI_OS_CONNECT_DIR` y `LOCALAPPDATA`.

Por su cuenta, el árbitro:
- ejecutó las pruebas de auditoría (fallaban las 7 de entonces);
- contrastó con el código H-01, H-02, H-03, H-04, H-05, R-05 y D-5;
- leyó `e2e_servicio.json`, `arnes_ventana.json` y `arnes_reanudar.json`.

No volvió a ejecutar la suite completa ni los arneses de la ventana, y no miró las capturas.

**Rondas:** 3 de 3 (la 3.ª, tras la sesión con el Broker real; ver al final). Tras la 2.ª no
quedaban objeciones abiertas.

## Ronda 1 — hallazgos que el árbitro confirmó

| Hallazgo | Por qué lo da por bueno |
| --- | --- |
| H-01 | Confirmado: `runtime.py:474` comprueba el token antes de `broker_token_source()`, al contrario que `validate()`. La fuente de lo esperado es la ayuda de `app.py:871`. |
| H-02 | Confirmado: `transporte.py:304`, `compare_digest` sobre texto. |
| H-03 | Confirmado: `_post` no captura `ValueError` y `.get` se aplica sobre una lista; el caso del recibo se reproduce. |
| H-05 | Confirmado, y la reproducción es válida. El doble no la invalida: que `informe.txt` exista demuestra que el run usó el perfil guardado; solo falla la presentación. |
| U05 «estado degradado» | Bien atribuido al doble, no a Athena (`ai_broker.py:181` espera `"healthy"`). |
| R-05, D-5 | Correctos por inspección. |

## Ronda 1 — objeciones y resolución

Las 9 objeciones se **aceptaron**; ninguna se descartó.

**1. Faltaba este documento de cierre.**
- Gravedad: alta. Criterio incumplido: 7.
- Resolución: creado este documento y añadido el §7 del informe.
- Comprobación: existe y cada objeción tiene su resolución.

**2. `test_h04` afirmaba lo contrario de la conclusión (la conducta es deliberada y el fallo es de la documentación).**
- Gravedad: media. Criterios incumplidos: 2 y 5.
- Resolución:
  - `test_h04` reescrito: da por buena la conducta (`governor_from_environment()` no es `None`) y comprueba que el README nombre el fichero de conexión y que el docstring del módulo no repita la frase falsa.
  - Añadidas a H-04 las dos contradicciones que faltaban: el docstring del módulo y la sección de tokens del README.
- Comprobación: falla con «el README no dice que el fichero de conexión activa Core»; pasaría corrigiendo solo la documentación.

**3. El caso «solo fichero + Core caído → 503» era una inferencia presentada como ejecutada.**
- Gravedad: media. Criterio incumplido: 6.
- Resolución: añadido el caso **C-implicito-caido** al e2e y ejecutado: 503 `core_unavailable`, sin decir de dónde salió la URL. La parte del escritorio queda marcada como inspección.
- Comprobación: `evidencias/e2e_servicio.json`.

**4. La regla «una consulta no escribe» se daba por verificada sin haberse probado.**
- Gravedad: media. Criterios incumplidos: 2 y 6.
- Resolución: añadido el caso **E19** (perfil `questions` y `writes: allow`, con el modelo pidiendo `write_file`). PASA: no se crea el fichero y el run acaba `failed` («tool Athena did not offer»). La parte de la ventana queda marcada como inspección.
- Comprobación: E19 en el JSON.

**5. `test_h05` era frágil, porque buscaba una cadena en el código.**
- Gravedad: baja-media. Criterio incumplido: 5.
- Resolución:
  - `test_h05` retirado. La regresión pasa a ser `h05_reanudar.py`, que sale con código 1 si el titular niega los ficheros cambiados. Repetido (`evidencias/h05c`): sale con 1, «H-05 ABIERTO».
  - Añadidos al alcance de H-05 el botón «Cambiar objetivo…» y el caso inverso, ambos por inspección.
- Comprobación: el código de salida del guion.

**6. La evidencia registraba E10 como FALLA con una regla inventada.**
- Gravedad: baja. Criterios incumplidos: 2 y 6.
- Resolución: E10 se registra ahora como OBSERVADO, sin regla esperada, y el JSON se volvió a generar. El recuento del resumen es ahora 23 PASA / 4 FALLA / 2 OBSERVADO.
- Comprobación: el recuento del JSON coincide con §1 y §3.2.

**7. El resumen no avisaba de que el servicio se probó con `ATHENA_PLANNING=0`, que no es el modo de serie.**
- Gravedad: baja-media. Criterio incumplido: 6.
- Resolución: añadido al §1 el aviso de que la planificación, el modo de serie, queda NO VERIFICADA.
- Comprobación: lectura del §1.

**8. Lo esperado en H-08 no tenía fuente citada y la prueba era débil.**
- Gravedad: baja. Criterios incumplidos: 2 y 5.
- Resolución: H-08 cita la decisión del 20-oct y las versiones de las otras apps, y se presenta como propuesta que el dueño debe confirmar. `test_h08` compara ahora el valor exacto.
- Comprobación: la prueba falla con cualquier otra URL.

**9. En H-01, «Iniciar funciona sin token» solo se había ejecutado en el servicio.**
- Gravedad: baja. Criterio incumplido: 6.
- Resolución: marcado como «servicio: ejecutado; escritorio: por inspección».
- Comprobación: lectura de H-01.

## Ronda 2 — comprobación de las correcciones

- `e2e_servicio.py` repetido: 29 casos (23 PASA, 4 FALLA, 2 OBSERVADO). E19 y C-implicito-caido son nuevos.
- `h05_reanudar.py` repetido: código de salida 1, «informe.txt escrito: True; titular niega cambios: True».
- `pytest "docs/Auditoria 20261119/pruebas"`: 6 fallan, cada una por el motivo esperado. Se comprobó en particular que `test_h04` falla por el README y no por la conducta.
- `ruff check` y `ruff format --check` limpios sobre la carpeta de la auditoría: el repo pasa ruff también sobre `docs/`.

## Pendiente

La cobertura no realizada que lista el §3.3 del informe. Ninguna objeción quedó abierta.

## Ronda 3 — tras la sesión con el Broker real (§8 del informe)

El mismo papel de árbitro independiente, sobre lo añadido. Repitió por su cuenta:
- `h09_plazo_de_un_comando.py`: `failed`, `process_timeout`, `cancelled`, una sola tarea al
  Broker, salida 1;
- el cuelgue de `python` sin argumentos: en el hijo, `isatty()` es `True`.

Confirmó la causa raíz de H-09, el mecanismo de H-10 (`fallback_allowed`), H-11, y las
tablas §8.1 y §8.2 frente a las evidencias.

Las 8 objeciones se aceptaron; ninguna se descartó.

**1. H-09 ignoraba dos pruebas que fijan la conducta actual y la rama `LIMITED_RETRY`, que nunca se ejecuta.**
- Gravedad: media.
- Resolución:
  - se citan `test_cancellation_semantics.py:57/:103` y la rama de `recovery.py`;
  - la corrección se acota a la frontera de `BashTool`, sin tocar `classify_outcome` en
    general (también clasifica `await_cancellable` y el grafo).

**2. El «1 de cada 6» se presentaba como frecuencia actual.**
- Gravedad: baja-media.
- Resolución: marcado como histórico (medido con el plazo de 30 s); gravedad de H-09 rebajada
  de alta a media-alta.

**3. La reproducción de H-09 solo vale en Windows y el esperado de R06 era demasiado exigente.**
- Gravedad: baja.
- Resolución: declarado el alcance (solo Windows); R06 pasa a esperar «no acaba `failed` por
  el plazo de una herramienta y se verifica».

**4. H-10 es conducta deliberada.**
- Gravedad: media.
- Resolución: reclasificado como incoherencia de diseño frente al ADR-034, gravedad media; el
  fracaso del run y el coste pasan a riesgos.

**5. «Fuera del catálogo de 28» no tenía evidencia guardada.**
- Gravedad: media.
- Resolución: ahora dice «no está entre los 18 aptos de V02»; lo de los 28 queda como
  inferido.

**6. «Cola libre / ocupada» sin evidencia.**
- Gravedad: baja-media.
- Resolución: marcado NO VERIFICADO; solo se cita la captura de H05-real2 («Libre» al final).

**7. Partes antiguas del informe contradecían §8.**
- Gravedad: media.
- Resolución: actualizados la cabecera, §1, §3.3 y §7. Corregida la frase sobre las lecturas
  de la conexión real de Core (también `core_connection()` para el token).

**8. `h05_reanudar.py` daba «abierto» sin poder concluir.**
- Gravedad: baja.
- Resolución: ahora sale con 2 y dice «NO CONCLUYENTE» si no se escribió el fichero.

No hay más rondas: se alcanzó el máximo de tres.

# Resolución de la auditoría del 28-sep-2026

Auditoría: `Informes/Athena-2026-09-28/INFORME_ATHENA.md`, sobre el commit `d255a94`.
Versión que la resuelve: **0.2.0**.

## ¿Era correcta la auditoría?

Sí, en lo sustancial. Se volvieron a ejecutar sus dos sondas (`probes.py` y
`probes_integration.py`) sobre el commit auditado y **todas las evidencias se reprodujeron
idénticas**; la única diferencia fue la carpeta actual, que era la esperada.

Matices:

- **Suite:** la auditoría vio `1012 passed, 2 failed`. En este equipo el mismo commit dio
  `1014 passed`. Las dos fallidas (nieto huérfano tras cancelar y aislamiento sin Git)
  dependían de su entorno, y ella misma lo etiquetó así (E). Aun así, el código de A14 tenía
  el defecto que describía y se corrigió.
- **Tk (`init.tcl`):** aquí Tk funciona. Lo que sí se reproduce es que Tcl no admite crear
  varios intérpretes Tk seguidos en un mismo proceso en Windows («invalid command name
  tcl_findLibrary»); las pruebas de ventana comparten ahora un único intérprete.
- **A25:** la subcarpeta sin Git solo falla si está dentro de otro repositorio. Aquí no lo
  estaba, pero `git worktree list` conserva cuatro worktrees «prunable» de ejecuciones
  antiguas de la suite, que son justo ese síntoma.

## Hallazgos

| ID | Qué se hizo | Pruebas |
| --- | --- | --- |
| A01 | La verificación pide permiso por el mismo motor que `bash` (`PermissionCheckAuthorizer`). Con ejecución `off` no se ejecuta nada del proyecto, tampoco la línea base; con `ask` se enseñan los comandos exactos una vez por run. Nueva razón de inconcluso `execution_not_authorized`. El `git diff` de integridad va con `--no-ext-diff --no-textconv`. | `test_auditoria_20260928::test_a01_*`, `test_desktop_auditoria::test_a01_*` |
| A02 | `CommandPolicy`: confinamiento de toda ruta en argumentos, opciones que escriben (`--fix`, `--output`, `--write`, `ruff format`) pasan a R3, opciones que ejecutan (`git -c`, `--ext-diff`, `find -exec/-delete`, `rg --pre`) a R4, `uv run X` se clasifica como X, `npm run <script>` no verificador es R3, `git branch/remote` distinguen listar de modificar, `git tag` es R4, y una extensión nunca relaja una regla incorporada. Documentado que no es un sandbox del sistema. | `test_a02_*` (matriz de 24 comandos, rutas externas, `ToolExecutor` real) |
| A03 | Credencial por destino (proveedor + URL), solo en memoria; cambiar de proveedor recupera su URL, modelo y token; botón «Probar conexión» que valida el token contra un endpoint protegido del broker. | `test_desktop_auditoria::test_a03_*` |
| A04 | Tipo de tarea elegido por la persona: pregunta (perfil `questions`, evidencia «respuesta sin cambios», etiquetada como tal), cambio (`require_change`: solo texto nunca es éxito) o documentos (entregables). La lista de palabras solo sirve para avisar. | `test_desktop_auditoria::test_a04_*` |
| A05 | El grafo pasa a la verificación los ficheros que las tareas escribieron; la política de artefactos compara rutas canónicas y distingue inexistente, vacío y sin procedencia. | `test_a05_*` |
| A06 | Proveedor con el modelo fijado en toda llamada (planificador, hijos, delegados); plazo global del run como cancelación `timed_out` que alcanza a planificador e hijos; un run jerárquico rechaza la revisión de objetivo en vez de aceptarla sin efecto. | `test_a06_*` |
| A07 | Manifiesto versionado del run (`run_manifests`): raíz, opciones completas, objetivo. `resume` exige la misma raíz, recupera opciones y el presupuesto restante, y crea tablero de objetivo. | `test_a07_*` |
| A08 | Identidad de proyecto estable derivada de la ruta canónica (sin distinguir mayúsculas en Windows). | `test_a08_*` |
| A09 | Cerrojo lector/escritor por raíz, común al proceso, reentrante por tarea; lo usan el grafo, cada herramienta y la verificación. Límite declarado: no coordina entre procesos. | `test_a09_*` |
| A10 | Copia previa también de ficheros nuevos; lo escrito se confirma en `POST_EDIT` (tras el éxito), con hash. | `test_a10_*` (por `ToolExecutor` real) |
| A11 | Cada restauración comprueba que el fichero conserva el contenido que dejó Athena; si no, conflicto y se preserva. **Defecto adicional encontrado:** deshacer un run que editó dos veces un fichero solo aplicaba la copia más reciente; ahora se desenrolla la cadena. | `test_a11_*` |
| A12 | Copias en `files/` separadas de `manifest.json`, hash verificado antes de restaurar, escritura atómica; se leen los checkpoints del formato anterior. | `test_a12_*` |
| A13 | `RollbackLedger.load` reconstruye el libro desde disco por `run_id`; el endpoint deshace runs que ya no están vivos usando el manifiesto; lo ya deshecho no se vuelve a ofrecer. | `test_a13_*` |
| A14 | Job Object de Windows para cada hijo (mata a los nietos aunque su padre haya muerto), resultado de `taskkill` comprobado, espera final con plazo (`ProcessTreeError`). **Defecto adicional:** un comando que dejaba un proceso de fondo colgaba la herramienta hasta su plazo porque el nieto sujetaba las tuberías. | `test_a14_*` |
| A15 | El escritorio vigila el servicio y refleja una caída; cerrar espera al trabajo (con plazo) y para un servicio que arrancaba; el servicio se para con su árbol entero. **Defecto adicional:** parar el servicio esperaba hasta un latido SSE por cada cliente inactivo. | `test_desktop_auditoria::test_a15_*` |
| A16 | El escritorio corre sobre `RunRegistry`: perfiles, memoria, copias, historial, reanudar, deshacer y cambiar objetivo desde la ventana. | `test_desktop_auditoria::test_desktop_history_*` |
| A17 | Solo se acepta una ruta absoluta existente; la ventana no deja iniciar sin proyecto y lo dice. | `test_desktop_auditoria::test_a17_*` |
| A18 | Diálogo de aprobación con diff (editar/escribir), comando exacto (bash), comandos de verificación, alcance del permiso, foco en «Denegar», Esc deniega; se retira si se detiene el trabajo. | `test_desktop_auditoria::test_a18_*` |
| A19 | Una aserción trivial no compensa una real; la integridad resta lo que ya estaba cambiado al empezar; el plan usa el Python del `.venv` del proyecto y añade `ruff format --check`. | `test_a19_*` |
| A20 | Cabecera y cuerpo con plazo, credencial comprobada antes de leer el cuerpo, 400/408/413/431/503 con respuesta, `Transfer-Encoding` y `Content-Length` duplicado rechazados, límite de conexiones. | `test_a20_*` |
| A21 | Lectura acotada de respuestas de proveedores (32 MiB), salida de procesos con principio y final acotados, el registro suelta de memoria los runs terminados. | `test_a21_*` |
| A22 | Conexiones SQLite que se cierran, `user_version` en cada base (una más nueva se rechaza), propiedad de sesiones por proceso: una instancia no recupera las sesiones vivas de otra. | `test_a22_*` |
| A23 | Offset y ids vistos persistidos de forma atómica; el offset avanza con toda actualización numerada. Entrega «como mucho una vez». | `test_a23_*` |
| A24 | mypy: de 44 errores a 0 declarando el contrato de los mixins (sin `Any` ni ignores); herramientas de desarrollo fijadas; CI en `.github/workflows/gates.yml` (Windows y Linux, Python 3.11 y 3.14). | las cuatro puertas |
| A25 | `isolation.py` exige que la raíz de Git sea la carpeta autorizada; las bibliotecas de worktrees se marcan como experimentales y no conectadas. | `test_a25_*` |
| A26 | `CURRENT_STATE.md` reconciliado con matriz capacidad × interfaz y pruebas; `security-model.md` y README al día. La licencia queda para decisión del propietario. | — |
| A27 | Ventana reorganizada: pasos numerados, tipo de tarea, permisos explicados, estado en vivo, actividad en español con detalles técnicos plegados, resultado con qué se demostró y qué no, historial, servicio en su propia pestaña, panel con scroll y ventana mínima utilizable. | pruebas de uso reales (abajo) |

## Otros defectos encontrados al corregir

- Con `exec=ask`, la primera pregunta del run (la de la línea base) llegaba antes de que el
  cliente pudiera suscribirse y se denegaba siempre: `ask` no podía verificar nunca. Se da
  una ventana de enganche de 5 s al principio del run.
- Deshacer un run con dos ediciones del mismo fichero dejaba la primera (ver A11).
- Comandos con procesos de fondo colgaban `bash` hasta su plazo (ver A14).
- Parar el servicio tardaba un latido SSE por cliente inactivo (ver A15).
- Un run jerárquico cortado por plazo mientras planificaba escapaba sin estado final.

## Prueba de uso real

Con la ventana de verdad, ratón y teclado del sistema, contra AI_Broker
(`192.168.1.52:8765`, `qwen3.8:27b`), sobre un proyecto con un fallo y su test:

1. «Probar conexión» → conectado, token aceptado.
2. Modificar el proyecto, cambios y ejecución en «Preguntar»: cuatro aprobaciones (los
   comandos de verificación, la edición con su diff y dos ejecuciones de `pytest`). Resultado
   «Terminado y comprobado», `calc.py` corregido y `pytest` en verde; 2 min 48 s.
3. «Deshacer cambios» → `calc.py` vuelve a su versión anterior.
4. Pregunta con ejecución desactivada: respuesta correcta, ningún proceso del proyecto
   ejecutado, etiquetada como respuesta.
5. Crear documentos con entregable `resumen.md` y cambios en «Permitir»: documento escrito
   y verificado como entregable (el modelo se repitió y Athena rescató lo ya escrito).
6. «Detener» a mitad de una pregunta → «Detenido»; cerrar la ventana con un trabajo en
   marcha → pide confirmación, y el trabajo queda cancelado en el historial, no huérfano.
7. Aviso cuando el encargo pide cambios y la tarea es una pregunta; ventana a 920×640.

## Integración con Agora

Con el cliente real de Agora (`agora.integrations.athena.AthenaClient` y su comprobación de
entregables) contra `athena_service` sobre el broker: servicio disponible y autenticado,
replay idempotente con el mismo run, run `completed` con verificación `passed` en 102 s, y
Agora acepta `resumen.md`. Las pruebas de integración de Agora (`test_athena_integration`)
siguen pasando (6/6). La API de Agora (`:8741`) acepta la credencial; no se crearon tarjetas
en el tablero real.

## Puertas

`pytest`: 1108 passed · `ruff check`: limpio · `ruff format --check`: 244 ficheros ·
`mypy --strict`: sin errores en 194 ficheros. Las sondas originales de la auditoría fallan
ahora donde medían el defecto: el marcador de ejecución ya no se crea (A01) y reanudar en
otro proyecto se rechaza (A07).

## Límites que siguen abiertos

- La coordinación del workspace es por proceso.
- Telegram: entrega como mucho una vez.
- Licencia: `pyproject.toml` dice `Proprietary` y `LICENSE` es MIT.
- No se ha probado con lector de pantalla ni con escalado del 200 %.

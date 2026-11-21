\# TAREA



Realiza una auditoría integral de la aplicación proporcionada: lógica de negocio, funcionamiento técnico, flujos completos y usabilidad desde el punto de vista de quien la utiliza.



El objetivo es descubrir fallos reales, contradicciones, recorridos incompletos y dificultades de uso, y proponer mejoras justificadas tanto de lógica como de experiencia de usuario.



No te limites a leer archivos o comprobar que los tests existentes pasan. Reconstruye qué intenta conseguir el usuario, cómo lo permite la aplicación y qué ocurre realmente en condiciones normales y problemáticas.



Utiliza el código, documentación, configuración, pruebas y aplicación ejecutable que estén disponibles. Descubre su tecnología, arquitectura y funcionalidades sin presuponerlas.



Esta fase autoriza la inspección y las pruebas en un entorno aislado, y la creación de informes y pruebas de auditoría separadas. La implementación de cambios en el producto queda fuera de esta fase, salvo autorización expresa adicional. Respeta cualquier autorización ya existente.



\# MÉTODO



\## 1. Establece el contexto y la referencia inicial



Antes de emitir conclusiones:



\- Identifica la versión o commit analizado, plataforma, componentes y dependencias.

\- Lee las instrucciones del proyecto.

\- Reconstruye el propósito de la app, sus usuarios y las tareas que deben poder completar.

\- Identifica las reglas documentadas y las deducidas del código. No confundas comportamiento implementado con comportamiento correcto.

\- Si documentación, interfaz y código se contradicen, registra la contradicción y su impacto.

\- Determina qué puedes ejecutar realmente y qué requiere dependencias, credenciales o servicios no disponibles.

\- Ejecuta las comprobaciones existentes pertinentes y registra los fallos de partida.



Resuelve decisiones rutinarias con criterio propio. Pregunta únicamente cuando una ambigüedad impida evaluar correctamente una regla o avanzar de forma segura; continúa con el resto del análisis.



Utiliza datos ficticios y un entorno de pruebas. Evita operaciones reales con efectos externos, costes o modificaciones de datos de producción.



\## 2. Construye un inventario y una matriz de cobertura



Inventaría:



\- Funcionalidades y módulos.

\- Pantallas, rutas, puntos de entrada y acciones disponibles.

\- Entidades, estados y transiciones.

\- Roles y permisos, si existen.

\- Persistencia, procesos en segundo plano e integraciones.



Relaciona cada funcionalidad con:



Objetivo del usuario → recorrido → reglas implicadas → componentes → pruebas → resultado.



Incluye funciones secundarias relevantes: configuración, búsqueda, filtros, importación, exportación, recuperación, cancelación y borrado, cuando existan.



Utiliza este inventario para evitar zonas olvidadas. Si el alcance supera lo comprobable, prioriza por impacto y riesgo, y deja identificada toda la cobertura pendiente. No presentes una muestra como revisión completa.



\## 3. Audita la lógica de negocio y la gestión de datos



Comprueba, según corresponda:



\- Coherencia de cálculos, condiciones, prioridades y decisiones.

\- Validaciones de entrada en la interfaz y en las capas que procesan los datos.

\- Reglas invariantes: qué condiciones deben mantenerse siempre.

\- Transiciones válidas e inválidas entre estados.

\- Valores vacíos, nulos, duplicados, extremos y formatos incorrectos.

\- Fechas, zonas horarias, unidades y redondeos.

\- Dependencias entre entidades y consecuencias de editar, mover o borrar.

\- Consistencia entre interfaz, lógica interna y datos almacenados.

\- Persistencia tras cerrar y volver a abrir.

\- Guardados parciales, operaciones repetidas y fallos a mitad de un proceso.

\- Concurrencia, condiciones de carrera y respuestas que llegan fuera de orden.

\- Cancelación, reintentos, recuperación e idempotencia.

\- Manejo de errores y conservación de los datos del usuario.

\- Contratos entre módulos e integraciones.

\- Permisos y aislamiento de datos cuando sean relevantes.

\- Código desconectado de la interfaz, botones sin efecto y funciones aparentemente disponibles pero incompletas.



Sigue los datos de extremo a extremo: entrada, validación, transformación, almacenamiento y presentación.



Si la app usa IA, comprueba también cómo valida sus salidas, trata respuestas inválidas, gestiona latencia y fallos, y permite al usuario entender o revisar las decisiones relevantes.



\## 4. Recorre los flujos como un usuario



Construye escenarios basados en objetivos reales, no solo en pantallas.



Para cada flujo, examina:



1\. Cómo descubre el usuario dónde empezar.

2\. Qué información necesita y si la aplicación se la proporciona.

3\. Qué pasos realiza y qué decisiones debe tomar.

4\. Qué respuesta recibe después de cada acción.

5\. Cómo sabe que ha terminado correctamente.

6\. Qué ocurre si se equivoca, abandona, retrocede o retoma después.

7\. Si el resultado mostrado coincide con el estado realmente guardado.



Incluye, cuando aplique:



\- Primera ejecución y configuración inicial.

\- Uso habitual y tareas principales.

\- Edición y corrección de información existente.

\- Búsqueda y recuperación de trabajo previo.

\- Cancelación, deshacer y acciones destructivas.

\- Errores de validación y de servicios externos.

\- Pérdida de conexión y reintentos.

\- Cierre, reinicio y restauración del estado.

\- Uso repetido, doble clic y envío duplicado.

\- Interacción entre funcionalidades que por separado parecen funcionar.



Detecta callejones sin salida, pasos innecesarios, dependencias ocultas, estados imposibles, pérdida de contexto y recorridos que requieren conocer detalles internos.



No aceptes que un flujo funciona solo porque cada botón responde: verifica que el usuario consigue su objetivo y que el resultado persiste correctamente.



\## 5. Evalúa la usabilidad



Si puedes ejecutar y manejar la interfaz, hazlo. Contrasta la experiencia visible con el código y los datos. Si solo puedes inspeccionar el código, declara esa limitación.



Evalúa:



\- Claridad de navegación y organización.

\- Correspondencia entre términos de la app y conceptos del usuario.

\- Visibilidad del estado y respuesta tras las acciones.

\- Consistencia de controles y comportamientos.

\- Carga de memoria, esfuerzo y número de pasos.

\- Claridad de formularios y valores predeterminados.

\- Prevención de errores y facilidad para corregirlos.

\- Mensajes que expliquen qué ocurrió y cómo continuar.

\- Estados vacíos, carga, éxito, error y datos parciales.

\- Jerarquía visual, legibilidad y densidad de información.

\- Uso del teclado, foco, etiquetas y contraste, cuando sean comprobables.

\- Adaptación a tamaños de ventana y dispositivos pertinentes.

\- Sensación de respuesta durante operaciones largas.

\- Protección y recuperación del trabajo del usuario.



Distingue observaciones verificadas de juicios heurísticos. No presentes una revisión propia como una prueba con usuarios reales ni declares conformidad de accesibilidad sin comprobación suficiente.



Relaciona cada problema con una tarea concreta y una consecuencia: confusión, errores, esfuerzo, abandono o pérdida de trabajo.



\## 6. Diseña y ejecuta pruebas pertinentes



Combina las técnicas que aporten evidencia:



\- Pruebas unitarias de reglas.

\- Pruebas de integración.

\- Pruebas de extremo a extremo.

\- Pruebas exploratorias mediante la interfaz.

\- Casos límite y pruebas negativas.

\- Pruebas de transiciones de estado.

\- Simulación de fallos de dependencias.

\- Pruebas de persistencia, repetición y recuperación.



Prioriza pérdida o corrupción de datos, resultados incorrectos, operaciones bloqueadas, permisos y flujos principales.



Para cada caso registra:



ID, objetivo, precondiciones, datos, pasos o comando, resultado esperado, resultado observado, estado y evidencia.



Justifica de dónde procede el resultado esperado. Cuando la regla sea desconocida, no la inventes para declarar un fallo.



Estados:

\- PASA.

\- FALLA.

\- BLOQUEADA.

\- NO EJECUTADA.

\- NO APLICA, con justificación.



No sustituyas todas las pruebas por mocks. Explica qué verifican las simulaciones y qué queda pendiente con componentes reales.



No confundas ausencia de fallos en los tests con ausencia de defectos. Tampoco uses el porcentaje de cobertura como prueba suficiente de corrección.



\## 7. Propón mejoras concretas



Separa claramente:



A. Defectos confirmados.

B. Riesgos o hipótesis pendientes de comprobar.

C. Mejoras de lógica.

D. Mejoras de usabilidad.

E. Ampliaciones opcionales.



Para cada mejora explica:



\- Problema u oportunidad y evidencia.

\- Usuario y tarea afectados.

\- Comportamiento actual y propuesto.

\- Beneficio esperado, sin inventar cifras.

\- Componentes que habría que modificar.

\- Alternativa más sencilla, si existe.

\- Coste relativo, dependencias y riesgos.

\- Criterios de aceptación y prueba de validación.



Prioriza también simplificar, eliminar pasos, mejorar valores predeterminados y hacer comprensibles las decisiones de la app.



No propongas reescribir toda la aplicación, cambiar de tecnología o añadir funcionalidades por preferencia personal. Justifica cualquier cambio estructural mediante problemas demostrables.



\## 8. Aplica revisión adversarial



Trabaja con dos responsabilidades:



\- Auditor constructor: elabora el mapa, las pruebas, los hallazgos y las propuestas.

\- Árbitro: busca errores, contradicciones, evidencia insuficiente y omisiones en la auditoría.



Si hay un agente independiente disponible y autorizado, úsalo para el arbitraje. Entrégale requisitos, fuentes, informe y evidencias. En otro caso, realiza una revisión separada por rol y declárala como autoevaluación.



El árbitro debe cuestionar especialmente:



\- Supuestos presentados como requisitos.

\- Comportamientos intencionados etiquetados como bugs.

\- Pruebas que no demuestran lo que se afirma.

\- Flujos o estados omitidos.

\- Propuestas que introducen incoherencias o complejidad.

\- Conclusiones más amplias que la cobertura real.



Cada objeción debe incluir evidencia, gravedad, criterio incumplido, corrección propuesta y comprobación posterior.



Sigue el ciclo: planificar, construir la auditoría, comprobar, revisar, corregir el informe o las pruebas y volver a comprobar.



Realiza un máximo de tres rondas de evaluación. No fabriques defectos ni rondas adicionales. Si faltan herramientas o evidencias, entrega el estado alcanzado y sus límites.



\# LISTÓN



La referencia de calidad es una auditoría técnica y de producto reproducible, útil para decidir y ejecutar mejoras.



Son obligatorios estos criterios:



1\. Cobertura trazable.

&#x20;  Comprobación: cruzar inventario, flujos y pruebas.

&#x20;  Evidencia: matriz con todo lo revisado y lo pendiente.



2\. Hallazgos sustentados.

&#x20;  Comprobación: reproducción o demostración precisa mediante código y datos.

&#x20;  Evidencia: pasos, resultados, referencias y condiciones de aparición.

&#x20;  Un defecto demostrado por inspección puede confirmarse como tal, pero debe indicar que no se ha reproducido en ejecución.



3\. Recorridos completos.

&#x20;  Comprobación: verificar objetivos, resultado final y persistencia, incluyendo fallos y recuperación.

&#x20;  Evidencia: escenarios y resultados observados.



4\. Evaluación de usabilidad concreta.

&#x20;  Comprobación: relacionar cada observación con una tarea y su consecuencia.

&#x20;  Evidencia: pantalla o componente, acción y dificultad encontrada.



5\. Mejoras evaluables.

&#x20;  Comprobación: revisar que cada propuesta tenga alcance y aceptación definidos.

&#x20;  Evidencia: comportamiento antes/después y prueba que validaría la mejora.



6\. Honestidad sobre las comprobaciones.

&#x20;  Comprobación: contrastar cada conclusión con la evidencia disponible.

&#x20;  Evidencia: separación entre ejecutado, inspeccionado, inferido y NO VERIFICADO.



7\. Revisión crítica final.

&#x20;  Comprobación: evaluar el informe contra estos criterios.

&#x20;  Evidencia: objeciones, cambios realizados y pendientes.



Evalúa por separado:

\- Calidad y suficiencia de la auditoría.

\- Estado funcional de la aplicación.



Una auditoría puede estar bien realizada y concluir que la app tiene problemas graves. No declares la aplicación aprobada si falla un criterio obligatorio o faltan pruebas esenciales; utiliza NO APROBADA o NO VERIFICADA, según corresponda, indicando el alcance.



\# ENTREGABLE



Entrega en español:



1\. Resumen ejecutivo.

&#x20;  Estado de la app dentro del alcance probado, problemas más relevantes y prioridades.



2\. Mapa funcional y de flujos.

&#x20;  Relación entre objetivos, pantallas, reglas, estados y componentes.



3\. Matriz de pruebas y cobertura.

&#x20;  Casos ejecutados, resultados, bloqueos y áreas pendientes.



4\. Registro de hallazgos.

&#x20;  Para cada uno:

&#x20;  - ID y título.

&#x20;  - Categoría.

&#x20;  - Gravedad e impacto, con justificación.

&#x20;  - Estado: confirmado o hipótesis.

&#x20;  - Entorno y precondiciones.

&#x20;  - Pasos de reproducción, si procede.

&#x20;  - Resultado esperado y observado.

&#x20;  - Evidencia y archivos o componentes implicados.

&#x20;  - Causa raíz confirmada o probable, diferenciadas.

&#x20;  - Corrección propuesta.

&#x20;  - Prueba de regresión o verificación posterior.



5\. Propuestas de mejora.

&#x20;  Separa lógica y usabilidad; incluye ejemplos concretos de los recorridos que cambiarían.



6\. Plan priorizado.

&#x20;  Ordena primero los bloqueos y riesgos graves, después los problemas frecuentes y finalmente las ampliaciones. Distingue gravedad de prioridad e indica esfuerzo relativo y dependencias.



7\. Cierre de verificación.

&#x20;  Modalidad real de revisión, rondas realizadas, objeciones descartadas y motivo, correcciones comprobadas en el informe o pruebas, comprobaciones no realizadas y pendientes.



Conserva evidencias reproducibles y referencias precisas. Evita exponer datos sensibles en capturas o registros.



No afirmes haber corregido la aplicación por haber propuesto una solución. No inventes hallazgos para rellenar apartados ni prometas haber comprobado “toda la app” sin demostrar la cobertura.



Empieza inspeccionando los materiales disponibles y construyendo el mapa funcional; continúa con las pruebas que puedas realizar sin esperar confirmaciones rutinarias.


# Estado de desarrollo de Jarvis

## Configuración activa — 19 de noviembre de 2026

Esta sección refleja la implementación más reciente y prevalece sobre las notas históricas de modelos y reintentos que aparecen más abajo.

- Modelo único y fijo: `gemini-3.8-flash`. Se usa para analizar, decidir y verificar; no existe un segundo modelo.
- Una solicitud por decisión. No hay reintentos automáticos ni fallback a otros modelos; el selector y el barrido de modelos están deshabilitados.
- Si falla la API, el modelo, el formato de respuesta o la validación de coordenadas, el servicio se detiene, descarta la cola y requiere reiniciar la aplicación.
- El orden de estrategia se valida en código: clic con cursor y nueva captura antes de permitir Win+R; CMD/PowerShell solo después de intentar Win+R.
- Las coordenadas fuera de la captura se rechazan con sus dimensiones; nunca se ajustan automáticamente.
- En consola solo se permiten lanzamientos simples de Paint, Bloc de notas, Calculadora o Explorador. Como último recurso, `ShellExecuteW` de `shell32.dll` puede abrir esas mismas aplicaciones; no se usa `user32.dll` para crear procesos. Si la API nativa falla o el objetivo no está en la lista permitida, la tarea se detiene.
- Se añadieron pruebas unitarias para la jerarquía, la política de modelo único y la parada fatal. La ejecución de CI de este commit queda pendiente de confirmación.

## Estado general

En curso. El ciclo existente del agente ahora puede ejecutarse desde el servicio
de trabajo serial y dispone de una interfaz Tkinter persistente, independiente de
la terminal. La captura se limita al monitor principal y la GUI mantiene una pestaña informativa con el modelo único; el selector y el
barrido de modelos están deshabilitados. La suite nueva de estrategia y fallos
fatales está pendiente de ejecución en CI y en el notebook.

## Arquitectura actual

1. `jarvis_gui.py` ejecuta Tkinter en el hilo principal, acepta varias
   instrucciones y consulta eventos mediante `after()`.
2. `agent_service.py` mantiene un solo hilo trabajador y una cola serial de tareas.
   El trabajador publica eventos tipados y no importa Tkinter ni manipula widgets.
3. Las autorizaciones usan un identificador de lote, la tupla inmutable exacta de
   acciones y una caducidad; respuestas de otro lote, acciones distintas o
   respuestas vencidas se ignoran.
4. `gemini_agent.py` conserva el único ciclo de decisión y ejecución. `TaskControl`
   le permite publicar actividad/estado, pausar, cancelar, solicitar capturas y
   presentar lotes al servicio, sin duplicar el motor.
5. Para capturar, el trabajador emite `CaptureRequestEvent` y espera. La GUI oculta
   su ventana solo si esta se superpone con el monitor principal; captura mediante
   GDI en el hilo de UI, restaura la ventana al terminar y responde al trabajador.
   El agente no puede analizar esa captura antes de completarse el intercambio.
6. `screen_vision.py` captura el rectángulo del monitor principal usando el mismo
   origen `(0, 0)` que el controlador nativo. En una configuración spacedesk, una
   ventana colocada íntegramente en el monitor secundario no entra en la imagen.
7. `GeminiModelManager` pagina `client.models.list()`, conserva metadatos de
   descubrimiento y sondeos de compatibilidad por separado, prueba texto e imagen
   sintética con el contrato JSON de Jarvis y expira ambos resultados mediante
   `GEMINI_MODEL_DIAGNOSTICS_TTL_HOURS` (168 horas predeterminadas). Usa metadatos
   descriptivos para filtrar modelos dedicados a TTS, transcripción, generación de
   música/imagen; los resultados llevan versión para no revivir diagnósticos viejos.
   La GUI envía estos trabajos al mismo trabajador serial; no corren en paralelo
   con tareas de escritorio.
8. El fallback prioriza el modelo configurado/seleccionado y solo añade
   alternativas que superaron texto e imagen. Los cambios de modelo se comunican
   como actividad de la tarea. Los sondeos de diagnóstico usan el timeout HTTP
   configurado por el cliente Gemini; cancelarlos es cooperativo entre llamadas.
9. `screen_vision.py` conserva el contrato JSON y la captura GDI;
   `cursor_control.py` conserva el control nativo. Gemini sigue usando el SDK
   `google-genai`.
10. El contrato terminal sigue usando `continue` con acciones y `success`/`error`
   sin acciones. `success` requiere una captura nueva y verificación independiente.

`gemini_api.py` sigue aislado: no se migró de SDK a REST. PyAutoGUI y Pillow siguen
en `requirements.txt` hasta completar la comparación acordada; la ruta activa ya no
los importa.

## Funcionalidades implementadas

- Captura GDI centralizada y mapeo validado entre píxeles de captura y escritorio
  virtual, incluidos origen no primario y rechazo de relaciones de aspecto distintas.
- Control nativo para movimiento, clic, doble clic, arrastre, escritura, teclas y
  desplazamiento.
- Contrato JSON validado de forma completa antes de considerar cualquier acción.
- Confirmación humana por lote para clics, doble clic, arrastre, escritura, teclas
  y desplazamiento. Si no hay terminal interactiva, el lote se descarta.
- Detector de más de dos clics consecutivos en el mismo píxel y nueva observación
  antes de permitir otro intento.
- Tope de acciones, rondas de decisión y duración total configurable; cancelación
  mediante `Ctrl+C` o al mover el cursor a la esquina superior izquierda.
- Reintentos acotados para saturación 429/503, espera progresiva y modelo alterno,
  sujetos al plazo global de la tarea.
- Timeout HTTP configurable (`GEMINI_TIMEOUT_SECONDS`, predeterminado 60 s),
  reintentos limitados por modelo (`GEMINI_MAX_ATTEMPTS_PER_MODEL`, 1-3) y
  espera progresiva configurable (`GEMINI_RETRY_DELAY_SECONDS`); los reintentos
  internos del SDK se limitan a una tentativa para evitar duplicarlos.
- Modelo principal `gemini-3.5-flash-lite`, compatible con entradas de texto e
  imagen según la [documentación oficial de modelos de Gemini](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).
  `gemini-3.6-flash` es el modelo de respaldo y también admite texto e imagen
  ([documentación oficial](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)).
- Un 404 de modelo se registra una sola vez y pasa al siguiente modelo configurado,
  sin repetir el modelo inaccesible. Los 429/500/503 y timeouts mantienen reintentos
  limitados por modelo. El modelo principal actual está centralizado en
  `GEMINI_MODEL` y el respaldo predeterminado no usa `gemini-2.5-flash-lite`.
- Los errores HTTP/API, timeout, cancelación local y errores de formato/validación
  no se tratan como éxito. Se registra modelo, intento y duración sin registrar
  el cuerpo de la solicitud ni credenciales.
- Registro con redacción de claves API conocidas; se omite el texto de la instrucción
  y el contenido de acciones de escritura.
- Interfaz con entrada independiente, estado, historial de actividad, panel de
  lotes, autorización/rechazo, previsualización, pausa, reanudación y parada de
  emergencia.
- Servicio de un solo trabajador con entrada de tareas serial, eventos tipados y
  autorización asociada a un lote exacto. La parada cancela la tarea activa,
  descarta autorizaciones pendientes y vacía las instrucciones en espera.
- Capturas coordinadas: la GUI se oculta antes de GDI y solo se restaura después de
  completar la captura cuando se superpone con el monitor principal. En un segundo
  monitor, se mantiene visible mientras GDI captura solo el principal.
- GUI persistente: una sola raíz Tk y un único `mainloop`; terminar, cancelar o
  fallar una tarea no destruye ni recrea la ventana.
- Diagnóstico Gemini explícito en una pestaña independiente: búsqueda paginada,
  pruebas de texto e imagen sintética, progreso, cancelación, clasificación,
  duración, motivo del fallo y selección persistente entre modelos compatibles.
  No se diagnostican modelos automáticamente en cada instrucción. Los resultados
  se escriben en `diagnostics/gemini_model_diagnostics.json`, sin clave ni
  contenido de capturas; el descubrimiento y la compatibilidad caducan por separado.
- El servicio y la GUI no usan `input()` ni `print()` para interacción. El modo
  CLI anterior se mantiene para compatibilidad; los mensajes diagnósticos de la
  GUI se escriben en el log y la consola.
- No se habilitaron clics genéricos automáticos ni se modificó la política actual
  de autorización.
- El usuario confirmó previamente que reemplazó la clave expuesta. La variable
  `GEMINI_KEY_ROTATED=true` solo representa esa confirmación y no permite
  comprobar criptográficamente que la clave activa sea nueva o válida.

## Funcionalidades verificadas

- Pruebas automatizadas de geometría, contrato de acciones, rechazo de lotes,
  autorización, clics repetidos, bloqueo previo a la rotación, verificación
  independiente de `success`, callbacks de runtime, servicio serial, GUI
  persistente y gestor de modelos: consultar la sección de última verificación
  para el conteo vigente.
- Las pruebas verifican estados terminales, lotes inválidos,
  `httpx.ReadTimeout` recuperable y agotado, ausencia de reintentos de errores
  de validación y timeout como resultado no exitoso.
- Las pruebas del servicio verifican tareas seriales, eventos, autorización exacta
  y vencida, rechazo, ID de lote incorrecto, pausa/reanudación, parada con
  descarte de tareas en cola, captura coordinada por eventos y publicación de
  errores del trabajador. Pruebas de handlers GUI confirman que aprobar/rechazar
  envía solo las acciones y el ID de la propuesta vigente, y que una aprobación
  vencida no se envía.
- `python -m py_compile` se ejecutó correctamente para el runtime, servicio, GUI y
  pruebas.
- Tkinter 8.6 está disponible en este Windows. La ventana se abrió y cerró; el
  árbol de accesibilidad confirmó el título. El proveedor de Computer Use no pudo
  entregar una imagen y no expuso los controles Tk como nodos accionables, así que
  no se verificaron visualmente el layout, botones ni ocultación/restauración.
- Las pruebas unitarias cubren que una tarea completada o fallida no destruye ni
  recrea la ventana, pero no sustituyen una inspección visual real de la GUI.
- En una prueba anterior se capturó el escritorio real: 1366x768, origen `(0, 0)`,
  DPI awareness `per-monitor-v2` y DPI del sistema 96. La captura GDI coincidió
  en dimensiones con PyAutoGUI/Pillow.

## Pendiente y problemas conocidos

- La petición de texto e imagen confirma que `gemini-3.5-flash-lite` funciona con
  las credenciales actuales. No se ha comprobado en vivo el respaldo
  `gemini-3.6-flash` ni el modelo verificador `gemini-2.5-flash`.
- Se ejecutó descubrimiento real: 49 modelos ofrecían `generateContent` en bruto;
  el filtro actual guardó 35 candidatos según metadatos. El barrido de
  compatibilidad encontró 404 para `gemini-2.5-flash` y `gemini-2.5-pro`; una
  prueba detectó un modelo de voz que no admite respuestas de texto y la siguiente
  petición obtuvo 429. Jarvis detuvo el sondeo para evitar más presión sobre una
  cuota posiblemente global. Los demás candidatos, incluido `gemini-3.5-flash-lite`,
  no se verificaron en ese barrido. El informe anterior fue invalidado al cambiar
  el esquema; el JSON conserva ahora el descubrimiento, pendiente de compatibilidad.
- La API no ofrece metadatos estructurados de modalidad; los nombres de producto
  sin descripción explícita de su finalidad aún pueden aparecer como candidatos.
  No se usan como fallback hasta superar las pruebas reales de texto e imagen.
- La cuota/API Key impidió completar la prueba en vivo de texto e imagen para los
  candidatos actuales. Una petición sintética anterior a `gemini-3.5-flash-lite`
  sí respondió, pero no demuestra disponibilidad de cuota actual ni fallback en
  esta sesión.
- La geometría del monitor principal y la exclusión de una ventana situada fuera
  de ese monitor tienen pruebas unitarias, pero la captura física con spacedesk y
  el escalado/DPI de ambos monitores siguen pendientes.
- El movimiento, clic, arrastre, escritura y teclas nativos están conectados, pero
  todavía falta la prueba física no destructiva acordada.
- La verificación final depende de una segunda respuesta multimodal de otro modelo
  Gemini sobre una captura nueva. Es probabilística y no sustituye una
  verificación local determinista para cada tarea.
- El flujo GUI de autorización y la parada durante una acción real no se han
  probado interactivamente. La autorización del CLI permanece por compatibilidad.
- El repositorio Git se inicializó con el punto de recuperación
  `089fc66` antes de las pruebas GUI; hay cambios posteriores sin commit.

## Dependencias

- `google-genai`: usada por la ruta activa.
- `httpx`: excepción de timeout HTTP tipada usada por la política controlada de
  reintentos; también es requisito del SDK.
- `tkinter`: interfaz gráfica; incluida en la instalación de Python disponible.
- `pyautogui` y `pillow`: conservadas temporalmente para la comparación pendiente;
  no se usan desde `gemini_agent.py`.
- No se añadieron dependencias externas.

## Última verificación

- `python -m py_compile` para los módulos y pruebas modificados: correcto.
- `python -m unittest discover -s tests -v`: 60 pruebas aprobadas.
- La petición multimodal sintética a Gemini tuvo éxito. No hubo captura del
  escritorio ni acciones sobre aplicaciones reales; la integración completa del
  agente no se considera verificada en vivo.

## Disponibilidad del modelo Gemini

- El registro de ejecución confirmó un 404 al solicitar `gemini-2.5-flash-lite`
  para una cuenta sin acceso a ese modelo.
- El valor actual en `.env` es `gemini-3.5-flash-lite`; el archivo no se versiona
  y sus secretos no se registran aquí.
- La documentación oficial consultada declara entradas de texto e imagen para
  `gemini-3.5-flash-lite` y `gemini-3.6-flash`.
- Solicitud real de texto e imagen realizada con `gemini-3.5-flash-lite` y un PNG
  sintético de 1x1 generado en memoria: respuesta `Red`. No se capturó ni envió
  el escritorio; no se ejecutaron acciones de Windows.

## Próximo objetivo

1. Repetir la inspección visual de la GUI en Windows con un medio que permita
   observar sus controles y comprobar la captura coordinada.
2. Ejecutar mediante la GUI una prueba de autorización y otra de pausa/parada con
   un runner que no controle el escritorio.
3. Solo después, ejecutar con autorización explícita la prueba integrada con Gemini
   “Abrí Paint”. Sigue pendiente verificar la captura, la respuesta del modelo, el
   ciclo de ejecución y la verificación visual final; no se habilitan clics
   genéricos automáticos.
4. Revisar logs para confirmar que no contienen credenciales ni contenido sensible;
   después comparar captura/control nativos con PyAutoGUI/Pillow.

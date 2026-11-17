# Estado de desarrollo de Jarvis

## Estado general

En curso. El ciclo existente del agente ahora puede ejecutarse desde el servicio
de trabajo serial y dispone de una interfaz Tkinter independiente de la terminal.
Las pruebas offline pasan. La GUI todavía requiere una prueba visual interactiva
completa en Windows; Gemini y el control físico no se han probado en vivo en esta
etapa.

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
   su ventana, deja 150 ms para que se actualice la composición, captura mediante
   GDI en el hilo de UI, restaura la ventana al terminar y responde al trabajador.
   El agente no puede analizar esa captura antes de completarse el intercambio.
6. `screen_vision.py` conserva el contrato JSON, la captura GDI y las coordenadas;
   `cursor_control.py` conserva el control nativo. Gemini sigue usando el SDK
   `google-genai`.
7. El contrato terminal sigue usando `continue` con acciones y `success`/`error`
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
  completar la captura. La implementación todavía debe comprobarse visualmente.
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
  independiente de `success`, callbacks de runtime y servicio serial:
  29 pruebas aprobadas en la última ejecución.
- Las pruebas verifican estados terminales, lotes inválidos,
  `httpx.ReadTimeout` recuperable y agotado, ausencia de reintentos de errores
  de validación y timeout como resultado no exitoso.
- Las pruebas del servicio verifican tareas seriales, eventos, autorización exacta
  y vencida, pausa/reanudación, parada con descarte de tareas en cola, captura
  coordinada por eventos y publicación de errores del trabajador.
- `python -m py_compile` se ejecutó correctamente para el runtime, servicio, GUI y
  pruebas.
- Tkinter 8.6 está disponible en este Windows. La ventana se abrió y cerró; el
  árbol de accesibilidad confirmó el título. El proveedor de Computer Use no pudo
  entregar una imagen y no expuso los controles Tk como nodos accionables, así que
  no se verificaron visualmente el layout, botones ni ocultación/restauración.
- En una prueba anterior se capturó el escritorio real: 1366x768, origen `(0, 0)`,
  DPI awareness `per-monitor-v2` y DPI del sistema 96. La captura GDI coincidió
  en dimensiones con PyAutoGUI/Pillow.

## Pendiente y problemas conocidos

- No se han confirmado en vivo la compatibilidad del identificador del modelo
  principal `gemini-3.5-flash-lite`, del alterno `gemini-2.5-flash-lite` ni del
  verificador `gemini-2.5-flash`; la red se mantuvo sin uso deliberadamente.
- El movimiento, clic, arrastre, escritura y teclas nativos están conectados, pero
  todavía falta la prueba física no destructiva acordada.
- La verificación final depende de una segunda respuesta multimodal de otro modelo
  Gemini sobre una captura nueva. Es probabilística y no sustituye una
  verificación local determinista para cada tarea.
- El flujo GUI de autorización y la parada durante una acción real no se han
  probado interactivamente. La autorización del CLI permanece por compatibilidad.
- El directorio no es un repositorio Git. Existe un punto de recuperación local
  previo a la implementación, pero no hay commits.

## Dependencias

- `google-genai`: usada por la ruta activa.
- `httpx`: excepción de timeout HTTP tipada usada por la política controlada de
  reintentos; también es requisito del SDK.
- `tkinter`: interfaz gráfica; incluida en la instalación de Python disponible.
- `pyautogui` y `pillow`: conservadas temporalmente para la comparación pendiente;
  no se usan desde `gemini_agent.py`.
- No se añadieron dependencias externas.

## Última verificación

- `python -m py_compile gemini_agent.py agent_service.py jarvis_gui.py tests\\test_agent_service.py tests\\test_agent_runtime.py`: correcto.
- `python -m unittest discover -s tests -v`: 29 pruebas aprobadas.
- Las pruebas son locales y simuladas: no hubo solicitudes a Gemini ni acciones
  sobre aplicaciones reales. La funcionalidad integrada no se considera
  verificada en vivo.

## Próximo objetivo

1. Repetir la inspección visual de la GUI en Windows con un medio que permita
   observar sus controles y comprobar la captura coordinada; no enviar una tarea
   real sin autorización.
2. Probar eventos y autorizaciones desde la interfaz, además de pausa y parada,
   con un runner de prueba que no controle el escritorio.
3. Después, con autorización explícita y una prueba de bajo riesgo, evaluar Gemini
   en vivo. La aceptación “Abrí Paint” con automatización segura queda pendiente:
   las acciones que requieren autorización conservan la política existente.
4. Revisar logs para confirmar que no contienen credenciales ni contenido sensible;
   después comparar captura/control nativos con PyAutoGUI/Pillow.

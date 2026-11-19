# BabyJarvis

Asistente visual de escritorio para Windows basado en Gemini. La aplicación analiza capturas del monitor principal y ejecuta las acciones propuestas en modo autónomo, sin aprobación manual por lote. El usuario conserva pausa y parada de emergencia.

## Requisitos

- Windows 10 u 11.
- Python 3.11 de 64 bits.
- Una API Key válida de Google Gemini.
- Conexión a Internet para las llamadas a Gemini.

## Instalación inicial

Abre PowerShell en la carpeta del repositorio y ejecuta:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Si PowerShell impide activar el entorno virtual, puedes ejecutar los comandos de instalación usando directamente `.venv\Scripts\python.exe`, sin cambiar la política de ejecución.

## Configuración de Gemini

1. Copia la plantilla:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Abre `.env` con un editor local.
3. Pega tu API Key únicamente en `GEMINI_API_KEY`. No la compartas por chat, capturas, logs ni commits.
4. Si ya reemplazaste la clave que se había expuesto anteriormente, cambia `GEMINI_KEY_ROTATED=false` a `GEMINI_KEY_ROTATED=true`. Esta variable es una confirmación manual; Jarvis no puede comprobar si la clave fue rotada.
5. Guarda el archivo. `.env` está excluido de Git.

No subas `.env` al repositorio. Si alguna vez se publica una clave, revócala desde Google AI Studio y genera otra.

## Primer inicio

Con el entorno virtual activo:

```powershell
python .\jarvis_gui.py
```

Se abrirá la ventana de Jarvis. La primera inicialización valida la configuración local y crea el cliente Gemini. Si aparece un error, revisa el mensaje de la interfaz y el archivo local `agent_debug.log`; antes de compartirlo, comprueba que no contenga datos privados.

## Modelo Gemini fijo

BabyJarvis utiliza exclusivamente `gemini-3.5-flash` para analizar la pantalla, proponer acciones y verificar resultados. El modelo está fijado en código: no hay selector, fallback a otros modelos ni reintentos automáticos. Las respuestas visuales solicitan lotes mínimos y disponen de un límite de 1024 tokens; si Gemini corta el JSON, la respuesta completa se descarta sin ejecutar acciones.

Si la API, el modelo o una respuesta estructurada falla, BabyJarvis detiene el servicio completo, descarta las tareas pendientes y requiere reiniciar la aplicación. No ejecuta acciones nuevas después de ese fallo.

La verificación visual también utiliza `gemini-3.5-flash`; no se necesita un segundo modelo ni una segunda API Key.

La estrategia actual prioriza el cursor y, en Windows, contempla Win+R y CMD/PowerShell como métodos alternativos. Los nombres de aplicaciones no están limitados a una lista fija: el agente utiliza el nombre solicitado por el usuario. No existe un fallback nativo ligado a aplicaciones concretas. La captura y el control de escritorio actuales siguen siendo específicos de Windows; la abstracción multiplataforma para Linux queda pendiente.

## Prompt visual editable

El prompt de percepción y control visual está en `prompts/desktop_agent.md`. BabyJarvis lee ese archivo desde disco en cada decisión visual, completa los marcadores con la etapa actual, la solicitud, el historial y las dimensiones reales de la captura, y envía el texto junto con la imagen a Gemini.

Puedes editar el Markdown para ajustar instrucciones y reglas sin modificar `screen_vision.py`. Conserva los marcadores dinámicos. Si el archivo falta o está incompleto, la decisión falla de forma segura y no se ejecutan acciones.

Gemini no abre archivos locales directamente: BabyJarvis lee el `.md` y transmite su contenido en la solicitud.

La validación determinista de coordenadas en Python permanece activa. El prompt refuerza los límites exactos de la imagen, pero nunca sustituye esa barrera.

## Primera prueba segura en el notebook

Hazla después de iniciar la aplicación y confirmar que la API Key local está configurada:

1. Deja abierta una aplicación no sensible, por ejemplo el Bloc de notas, con un documento vacío.
2. Envía una instrucción sencilla para abrir una aplicación o mostrar una ventana, sin pedir que escriba, envíe, elimine o guarde información.
3. Observa la captura y el lote propuesto.
4. Comprueba en la interfaz el lote propuesto y las capturas guardadas. El modo autónomo puede hacer clic, escribir y pulsar teclas sin pedir confirmación.
5. Comprueba que Jarvis vuelva a capturar la pantalla y que la finalización se confirme mediante una segunda consulta al mismo modelo fijo.

El botón **Parada de emergencia** cancela la tarea activa y descarta las instrucciones pendientes. El botón de cierre también solicita la cancelación. No son una garantía de reversión: una acción que ya se haya ejecutado no se deshace automáticamente. La ejecución sigue acotada por los límites globales de duración, rondas y acciones configurados.

## Pruebas automatizadas

```powershell
python -m pytest tests -q

Get-ChildItem -Recurse -Filter *.py |
  Where-Object {
    $_.FullName -notmatch '\\(recovery|\.venv|venv|__pycache__)\\'
  } |
  ForEach-Object { python -m py_compile $_.FullName }
```

## Privacidad y seguridad

- Las capturas del monitor principal se envían a Gemini para análisis cuando se ejecuta una tarea.
- El modo autónomo no solicita aprobación manual para clics, doble clic, arrastre, escritura, teclas ni desplazamiento.
- Cada captura tomada durante una tarea se guarda localmente en `debug_captures/` como PNG, junto con un JSON de metadatos (hora, tarea, dimensiones y geometría).
- Cada tarea mantiene además `debug_captures/<task_id>/events.jsonl`, con la secuencia temporal de estados, actividad, lotes propuestos, acciones redactadas, errores y resultado final. Los textos escritos se registran solo por cantidad de caracteres para no almacenar contraseñas ni contenido privado.
- La carpeta está excluida de Git; revisa y elimina las capturas cuando ya no las necesites, porque pueden contener información privada.
- No se deben guardar claves, capturas privadas, diagnósticos del equipo ni logs en Git.
- La verificación visual es probabilística; revisa el resultado en pantalla y no uses Jarvis para operaciones irreversibles o sensibles.

## Limitaciones pendientes

La CI confirma compilación y pruebas unitarias en un entorno Windows automatizado. No sustituye las pruebas en tu notebook: GUI real, DPI/monitores, captura GDI, control físico de mouse/teclado y disponibilidad/cuota de tu API Key deben comprobarse localmente.

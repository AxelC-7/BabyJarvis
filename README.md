# BabyJarvis

Asistente visual de escritorio para Windows basado en Gemini. La aplicación analiza capturas del monitor principal, propone acciones y solicita autorización antes de ejecutar acciones sensibles.

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

## Diagnóstico obligatorio de modelos

Antes de pedirle tareas de escritorio:

1. Abre la pestaña **Modelos Gemini**.
2. Pulsa **Buscar modelos disponibles** y espera a que termine.
3. Pulsa **Probar compatibilidad**. El diagnóstico envía solicitudes de texto y una imagen sintética de prueba; no envía tu escritorio ni ejecuta acciones.
4. Espera los resultados. Si Gemini devuelve HTTP 429, el diagnóstico detiene el barrido para evitar multiplicar las solicitudes.
5. Comprueba que haya al menos dos modelos con estado `compatible_text_image`. Uno puede ser el principal y otro debe quedar disponible como verificador independiente.
6. Selecciona un modelo compatible como principal con **Usar como principal**.

Los resultados se guardan localmente en `diagnostics/gemini_model_diagnostics.json`; esa carpeta no debe publicarse. Los resultados caducan según `GEMINI_MODEL_DIAGNOSTICS_TTL_HOURS` (168 horas por defecto).

Si solo hay un modelo compatible, Jarvis puede analizar y proponer acciones, pero no podrá declarar una tarea completada: la verificación visual exige un segundo modelo independiente y validado.

## Primera prueba segura en el notebook

Hazla solo después de completar el diagnóstico:

1. Deja abierta una aplicación no sensible, por ejemplo el Bloc de notas, con un documento vacío.
2. Envía una instrucción sencilla para abrir una aplicación o mostrar una ventana, sin pedir que escriba, envíe, elimine o guarde información.
3. Observa la captura y el lote propuesto.
4. Rechaza cualquier acción que no coincida exactamente con lo que esperas. Autoriza únicamente el lote que entiendas.
5. Comprueba que Jarvis vuelva a capturar la pantalla y que la finalización se confirme mediante verificación visual independiente.

La parada de emergencia cancela la tarea y descarta las instrucciones pendientes. No es una garantía de reversión: una acción que ya se haya ejecutado no se deshace automáticamente.

## Pruebas automatizadas

```powershell
python -m compileall -q .
python -m pytest tests -q
```

La compilación manual anterior incluye el directorio histórico `recovery/`; para la comprobación equivalente a CI, usa:

```powershell
Get-ChildItem -Recurse -Filter *.py |
  Where-Object {
    $_.FullName -notmatch '\\(recovery|\.venv|venv|__pycache__)\\'
  } |
  ForEach-Object { python -m py_compile $_.FullName }
```

## Privacidad y seguridad

- Las capturas del monitor principal se envían a Gemini para análisis cuando se ejecuta una tarea.
- El diagnóstico de compatibilidad utiliza una imagen sintética, no una captura del escritorio.
- Los lotes de clic, doble clic, arrastre, escritura, teclas y desplazamiento requieren autorización explícita.
- No se deben guardar claves, capturas privadas, diagnósticos del equipo ni logs en Git.
- La verificación visual es probabilística; revisa el resultado en pantalla y no uses Jarvis para operaciones irreversibles o sensibles.

## Limitaciones pendientes

La CI confirma compilación y pruebas unitarias en un entorno Windows automatizado. No sustituye las pruebas en tu notebook: GUI real, DPI/monitores, captura GDI, control físico de mouse/teclado, cuota de tu API Key y verificación visual con dos modelos deben comprobarse localmente.

# Diagnóstico de Windows

Aplicación local, de solo lectura y con interfaz Tkinter para consultar recursos,
revisar procesos y persistencia, registrar historial y exportar informes. No
detiene procesos, no altera servicios, tareas ni registro y no requiere elevar
permisos (algunos datos pueden aparecer como no disponibles).

## Instalación y ejecución

Desde la raíz del repositorio, en PowerShell:

```powershell
python -m pip install -r requirements.txt
python -m windows_diagnostics
```

El primer refresco comienza automáticamente. Use **Ejecutar diagnóstico completo**
para recopilar también procesos, firmas digitales, servicios, entradas de inicio
y tareas programadas. Las operaciones se ejecutan en segundo plano. Los informes
se exportan como TXT, JSON o HTML desde la pestaña **Pruebas e informes**.

## Sensores

Los valores se muestran únicamente si los proporciona un controlador o sensor
accesible. En Windows, `psutil` normalmente no puede leer temperaturas por sí
solo. Para temperaturas de CPU, GPU, almacenamiento y otros sensores, descargue
LibreHardwareMonitor desde sus [releases oficiales](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases)
y configure la ruta a `LibreHardwareMonitorLib.dll` antes de iniciar Jarvis:

```powershell
$env:LIBRE_HARDWARE_MONITOR_PATH = "C:\ruta\LibreHardwareMonitorLib.dll"
python -m windows_diagnostics
```

También se detecta la DLL si está junto a una instancia activa de
`LibreHardwareMonitor.exe`. La aplicación accede a la API de la biblioteca
directamente mediante PowerShell; **no hace falta habilitar un proveedor WMI de
LibreHardwareMonitor**. Se consultan los valores actuales y los mínimos/máximos
que expone la biblioteca; además, el historial local conserva los extremos
observados por Jarvis. Si falta la DLL, el controlador o un sensor, el dato queda
como no disponible. En el entorno donde se realizaron estas mejoras no se
encontró la DLL, por lo que las temperaturas reales requieren configurarla.

`nvidia-smi`, si está instalado junto al controlador NVIDIA, permite consultar
carga, temperatura, VRAM y potencia; LibreHardwareMonitor puede complementar
información de GPU en otros fabricantes. La batería se consulta mediante
`psutil` y WMI (`root\wmi`) cuando el firmware expone capacidades y ciclos. El
desgaste se calcula solo cuando están disponibles tanto la capacidad de diseño
como la capacidad de carga completa; de lo contrario queda como no disponible.

Las conexiones remotas por proceso no incluyen bytes transferidos ni análisis
de reputación. Un nombre desconocido o una firma ausente no se clasifica por sí
solo como malware. Las señales son pistas para revisión manual.

La auditoría enumera servicios, entradas de inicio reportadas por Windows y
tareas programadas. No determina la política de recuperación automática de cada
servicio.

## Pruebas y almacenamiento

Las pruebas permiten hasta 30 segundos, 70% de carga solicitada y 256 MB de
memoria o disco. El disco usa un archivo temporal y lo elimina al finalizar.
Las pruebas se pueden cancelar; se detienen si un sensor accesible supera el
umbral térmico configurado. No se fuerza carga en GPU. El historial y el registro
se guardan en `%LOCALAPPDATA%\Jarvis\WindowsDiagnostics`. En la pestaña Resumen
se puede ajustar el intervalo de 5 a 60 segundos y la retención de 5 a 120
minutos. La pestaña Memoria ordena los procesos por RSS y muestra MiB, porcentaje
de RAM física y tendencias sostenidas; una tendencia no demuestra una fuga y
RSS puede contar páginas compartidas más de una vez. Use **Cargar informe
anterior** para comparar CPU y RAM con un informe TXT o JSON previo sin
sobrescribirlo.

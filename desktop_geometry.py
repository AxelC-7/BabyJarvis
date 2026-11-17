"""Shared Windows virtual-desktop geometry and screenshot coordinate mapping."""

import ctypes
import sys
from dataclasses import dataclass
from functools import lru_cache


_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_DPI_AWARENESS = {
    0: "unaware",
    1: "system-aware",
    2: "per-monitor-aware",
}


@dataclass(frozen=True)
class ScreenGeometry:
    left: int
    top: int
    width: int
    height: int
    dpi_awareness: str = "unknown"
    system_dpi: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def _awareness_name(user32: object) -> str:
    get_context = getattr(user32, "GetThreadDpiAwarenessContext", None)
    get_awareness = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
    if get_context is None or get_awareness is None:
        return "unknown"
    get_context.argtypes = []
    get_context.restype = ctypes.c_void_p
    get_awareness.argtypes = [ctypes.c_void_p]
    get_awareness.restype = ctypes.c_int
    context = get_context()
    if not context:
        return "unknown"
    value = get_awareness(context)
    return _DPI_AWARENESS.get(value, "unknown")


@lru_cache(maxsize=1)
def initialize_dpi_awareness() -> tuple[str, int]:
    """Request per-monitor DPI coordinates before reading desktop dimensions."""
    if sys.platform != "win32":
        raise OSError("La geometría del escritorio está implementada para Windows.")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    mode = "unknown"
    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_int
        if set_context(ctypes.c_void_p(-4)):
            mode = "per-monitor-v2"
        else:
            mode = _awareness_name(user32)

    if mode == "unknown":
        set_aware = getattr(user32, "SetProcessDPIAware", None)
        if set_aware is not None:
            set_aware.argtypes = []
            set_aware.restype = ctypes.c_int
            if set_aware():
                mode = "system-aware"
            else:
                mode = _awareness_name(user32)

    get_dpi = getattr(user32, "GetDpiForSystem", None)
    system_dpi = 0
    if get_dpi is not None:
        get_dpi.argtypes = []
        get_dpi.restype = ctypes.c_uint
        system_dpi = int(get_dpi())

    return mode, system_dpi


def get_screen_geometry() -> ScreenGeometry:
    """Read the virtual desktop bounds in the process's current DPI context."""
    if sys.platform != "win32":
        raise OSError("La geometría del escritorio está implementada para Windows.")
    dpi_awareness, system_dpi = initialize_dpi_awareness()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    geometry = ScreenGeometry(
        left=user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
        top=user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
        width=user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
        height=user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
        dpi_awareness=dpi_awareness,
        system_dpi=system_dpi,
    )
    if geometry.width <= 0 or geometry.height <= 0:
        raise RuntimeError(
            "Windows devolvió dimensiones de escritorio no válidas: "
            f"{geometry.width}x{geometry.height}."
        )
    return geometry


def image_point_to_desktop(
    x: int,
    y: int,
    image_width: int,
    image_height: int,
    geometry: ScreenGeometry,
) -> tuple[int, int]:
    """Map a valid screenshot pixel to the corresponding virtual-desktop pixel."""
    values = (x, y, image_width, image_height)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("Las coordenadas y dimensiones deben ser enteros.")
    if image_width < 2 or image_height < 2:
        raise ValueError("La imagen debe medir al menos 2 píxeles por eje.")
    if not 0 <= x < image_width or not 0 <= y < image_height:
        raise ValueError("El punto está fuera de los límites de la imagen.")
    image_ratio = image_width * geometry.height
    screen_ratio = image_height * geometry.width
    if abs(image_ratio - screen_ratio) > max(image_ratio, screen_ratio) * 0.001:
        raise ValueError(
            "La imagen y el escritorio tienen distintas proporciones; "
            "no se mapearán coordenadas con estiramiento."
        )

    desktop_x = geometry.left + _scale_axis(x, image_width, geometry.width)
    desktop_y = geometry.top + _scale_axis(y, image_height, geometry.height)
    if not geometry.contains(desktop_x, desktop_y):
        raise RuntimeError("La conversión produjo un punto fuera del escritorio.")
    return desktop_x, desktop_y


def _scale_axis(value: int, image_length: int, desktop_length: int) -> int:
    return int(value * (desktop_length - 1) / (image_length - 1) + 0.5)

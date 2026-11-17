"""Generic Windows cursor and keyboard input using only the standard library."""

import ctypes
import sys
import time
from dataclasses import dataclass
from functools import lru_cache


_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_KEY_CODES = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "ARROWLEFT": 0x25,
    "ARROWUP": 0x26,
    "ARROWRIGHT": 0x27,
    "ARROWDOWN": 0x28,
    "DELETE": 0x2E,
    "WINDOWS": 0x5B,
}


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouse_data", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("extra_info", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("virtual_key", ctypes.c_ushort),
        ("scan_code", ctypes.c_ushort),
        ("flags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("extra_info", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mouse", _MouseInput), ("keyboard", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint), ("data", _InputUnion)]


@dataclass(frozen=True)
class VirtualScreen:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@lru_cache(maxsize=1)
def _user32():
    if sys.platform != "win32":
        raise OSError("El control del cursor está implementado para Windows.")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = ctypes.c_int
    user32.SendInput.argtypes = [
        ctypes.c_uint,
        ctypes.POINTER(_Input),
        ctypes.c_int,
    ]
    user32.SendInput.restype = ctypes.c_uint
    user32.SetProcessDPIAware.argtypes = []
    user32.SetProcessDPIAware.restype = ctypes.c_int
    user32.SetProcessDPIAware()
    return user32


def _send_mouse_input(flags: int, mouse_data: int = 0) -> None:
    event = _Input()
    event.type = _INPUT_MOUSE
    event.data.mouse = _MouseInput(0, 0, mouse_data, flags, 0, 0)
    if _user32().SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        error_code = ctypes.get_last_error()
        if error_code:
            raise ctypes.WinError(error_code)
        raise OSError("Windows no aceptó el evento de cursor.")


def _send_keyboard_input(virtual_key: int, scan_code: int, flags: int = 0) -> None:
    event = _Input()
    event.type = _INPUT_KEYBOARD
    event.data.keyboard = _KeyboardInput(virtual_key, scan_code, flags, 0, 0)
    if _user32().SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        error_code = ctypes.get_last_error()
        if error_code:
            raise ctypes.WinError(error_code)
        raise OSError("Windows no aceptó el evento de teclado.")


def _resolve_key(key: str) -> int:
    if not isinstance(key, str):
        raise TypeError("Cada tecla debe ser una cadena.")
    normalized = key.strip().upper()
    if normalized in _KEY_CODES:
        return _KEY_CODES[normalized]
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return ord(normalized)
    if normalized.startswith("F") and normalized[1:].isdigit():
        function_number = int(normalized[1:])
        if 1 <= function_number <= 12:
            return 0x70 + function_number - 1
    raise ValueError(f"Tecla no permitida: {key!r}.")


def validate_key(key: str) -> None:
    """Validate a key name without sending input."""
    _resolve_key(key)


def press_key(key: str) -> None:
    """Press and release one named virtual key."""
    virtual_key = _resolve_key(key)
    _send_keyboard_input(virtual_key, 0)
    try:
        time.sleep(0.015)
    finally:
        _send_keyboard_input(virtual_key, 0, _KEYEVENTF_KEYUP)


def press_key_combo(keys: list[str] | tuple[str, ...]) -> None:
    """Press a bounded key combination and release keys in reverse order."""
    if not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= 4:
        raise ValueError("Una combinación debe contener entre 1 y 4 teclas.")
    virtual_keys = [_resolve_key(key) for key in keys]
    pressed: list[int] = []
    try:
        for virtual_key in virtual_keys:
            _send_keyboard_input(virtual_key, 0)
            pressed.append(virtual_key)
        time.sleep(0.06)
    finally:
        for virtual_key in reversed(pressed):
            _send_keyboard_input(virtual_key, 0, _KEYEVENTF_KEYUP)


def type_text(text: str) -> None:
    """Type Unicode text using native keyboard events, without clipboard access."""
    if not isinstance(text, str):
        raise TypeError("El texto debe ser una cadena.")
    if len(text) > 2000:
        raise ValueError("El texto excede el límite de 2000 caracteres por acción.")
    encoded = text.encode("utf-16-le", errors="strict")
    for offset in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[offset : offset + 2], "little")
        _send_keyboard_input(0, code_unit, _KEYEVENTF_UNICODE)
        _send_keyboard_input(0, code_unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


def get_virtual_screen() -> VirtualScreen:
    """Return virtual-desktop pixel bounds, including monitors left of the primary."""
    user32 = _user32()
    screen = VirtualScreen(
        left=user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
        top=user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
        width=user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
        height=user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
    )
    if screen.width <= 0 or screen.height <= 0:
        raise RuntimeError("Windows devolvió dimensiones de pantalla no válidas.")
    return screen


def get_cursor_position() -> tuple[int, int]:
    """Return the cursor position in virtual-desktop pixel coordinates."""
    point = _Point()
    if not _user32().GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def move_cursor_to(x: int, y: int) -> tuple[int, int]:
    """Move the pointer to an absolute virtual-desktop pixel coordinate."""
    if isinstance(x, bool) or not isinstance(x, int):
        raise TypeError("x debe ser un entero de píxeles.")
    if isinstance(y, bool) or not isinstance(y, int):
        raise TypeError("y debe ser un entero de píxeles.")

    screen = get_virtual_screen()
    if not screen.contains(x, y):
        raise ValueError(
            f"Coordenadas ({x}, {y}) fuera de la pantalla "
            f"({screen.left}, {screen.top})–({screen.right - 1}, {screen.bottom - 1})."
        )
    if not _user32().SetCursorPos(x, y):
        raise ctypes.WinError(ctypes.get_last_error())
    return get_cursor_position()


def click_cursor(button: str = "left") -> tuple[int, int]:
    """Click once at the current pointer position using a supported mouse button."""
    events = {
        "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
        "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
        "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
    }
    if button not in events:
        raise ValueError("button debe ser 'left', 'right' o 'middle'.")

    down_event, up_event = events[button]
    _send_mouse_input(down_event)
    try:
        time.sleep(0.04)
    finally:
        _send_mouse_input(up_event)
    return get_cursor_position()


def double_click_cursor(button: str = "left") -> tuple[int, int]:
    """Perform a double click with the selected mouse button."""
    click_cursor(button)
    time.sleep(0.08)
    click_cursor(button)
    return get_cursor_position()


def scroll_cursor(delta: int) -> tuple[int, int]:
    """Scroll at the current pointer position; delta is measured in wheel units."""
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise TypeError("delta debe ser un entero.")
    if delta == 0 or abs(delta) > 1200:
        raise ValueError("delta debe estar entre -1200 y 1200 y no puede ser cero.")

    position = get_cursor_position()
    _send_mouse_input(_MOUSEEVENTF_WHEEL, delta & 0xFFFFFFFF)
    return position

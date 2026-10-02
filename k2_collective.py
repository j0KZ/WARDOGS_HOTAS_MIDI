# K2 fader -> vJoy axis (collective) for Joystick Gremlin R14+ (user script)
#
# Reglas de diseno (para mantenerse en zona defendible):
# - Traduccion 1:1: cada salida es la posicion actual del fader. Nada mas.
# - Sin input no hay salida: solo escribe en vJoy cuando el fader cambia.
# - No lee nada del juego. Sin secuencias, sin temporizadores de accion,
#   sin valores por defecto inventados.
#
# Requisitos: Windows, vJoy, Joystick Gremlin R14 o superior.
# K2 Deck (u otro programa que use el K2) debe estar CERRADO: en Windows
# un puerto MIDI normalmente lo abre un solo programa a la vez.

import ctypes
import logging
import sys
import threading
import time

from gremlin.types import InputType
from gremlin.user_script import (
    BoolVariable,
    IntegerVariable,
    StringVariable,
    VirtualInputVariable,
    periodic,
)

log = logging.getLogger("user")  # logger que Gremlin escribe en user.log

# ---------------------------------------------------------------- variables

device_name = StringVariable(
    "MIDI device name",
    "Parte del nombre del dispositivo MIDI (ej. XONE:K2)",
    False,
    "XONE:K2",
)

midi_channel = IntegerVariable(
    "MIDI channel",
    "Canal MIDI del K2 (1-16). Verificalo con un monitor MIDI.",
    False,
    15,
    1,
    16,
)

cc_number = IntegerVariable(
    "Fader CC number",
    "Numero de Control Change del fader (0-127). Verificalo con un monitor MIDI.",
    False,
    19,
    0,
    127,
)

output_axis = VirtualInputVariable(
    "Collective vJoy axis",
    "Eje de vJoy donde va el collective",
    False,
    [InputType.JoystickAxis],
)

invert = BoolVariable(
    "Invert",
    "Invertir el fader (arriba = minimo)",
    False,
    False,
)

center_deadzone = IntegerVariable(
    "Center deadzone",
    "Pasos MIDI a cada lado del centro (64) que dan 0 exacto",
    False,
    6,
    0,
    30,
)

# ------------------------------------------------------------ pure helpers

CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MMSYSERR_NOERROR = 0
RETRY_SECONDS = 2.0


def parse_cc(message, channel, cc):
    """Devuelve el valor 0-127 si el mensaje es el CC buscado, si no None.

    message: entero empaquetado de winmm (status | data1<<8 | data2<<16).
    channel: 1-16. cc: 0-127.
    """
    status = message & 0xFF
    data1 = (message >> 8) & 0x7F
    data2 = (message >> 16) & 0x7F
    if status != (0xB0 | ((channel - 1) & 0x0F)):
        return None
    if data1 != cc:
        return None
    return data2


def cc_to_axis(value, inverted, deadzone=0):
    """Mapea 0-127 a -1.0..1.0 (rango de ejes de Gremlin), con 64 = 0 exacto.

    128 valores no tienen centro, asi que cada mitad se escala por separado.
    deadzone: pasos a cada lado de 64 que dan 0. Fuera de la zona el eje
    parte de 0 sin salto y llega a -1/1 en los extremos.
    """
    value = max(0, min(127, int(value)))
    offset = value - 64
    if abs(offset) <= deadzone:
        return 0.0
    if offset > 0:
        axis = (offset - deadzone) / (63.0 - deadzone)
    else:
        axis = (offset + deadzone) / (64.0 - deadzone)
    return -axis if inverted else axis


# ------------------------------------------------------------- MIDI (winmm)
#
# El estado vive en el modulo sys para sobrevivir a recargas del script:
# si Gremlin recarga el script y el callback antiguo se libera mientras el
# puerto sigue abierto, Windows llamaria a memoria liberada y se caeria.

_STATE_KEY = "_k2_collective_state"
if not hasattr(sys, _STATE_KEY):
    setattr(
        sys,
        _STATE_KEY,
        {
            "handle": None,
            "callback": None,
            "lock": threading.Lock(),
            "pending": None,  # ultimo valor CC recibido, aun no enviado
            "last_sent": None,
            "next_retry": 0.0,
            "filter": (15, 19),  # (canal, cc) que lee el callback
        },
    )
_state = getattr(sys, _STATE_KEY)


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("dwSupport", ctypes.c_uint),
    ]


def _winmm():
    if sys.platform != "win32":
        raise OSError("winmm solo existe en Windows")
    return ctypes.WinDLL("winmm")


def _make_callback():
    # Regla de winmm: dentro del callback no se llaman funciones de winmm
    # ni nada que bloquee. Solo se guarda el valor.
    proto = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )

    def on_midi(handle, msg, instance, param1, param2):
        if msg != MIM_DATA:
            return
        channel, cc = _state["filter"]
        value = parse_cc(int(param1), channel, cc)
        if value is not None:
            with _state["lock"]:
                _state["pending"] = value

    return proto(on_midi)


def _find_device(winmm, name_part):
    count = winmm.midiInGetNumDevs()
    caps = MIDIINCAPSW()
    names = []
    for idx in range(count):
        res = winmm.midiInGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps))
        if res != MMSYSERR_NOERROR:
            continue
        names.append(caps.szPname)
        if name_part.lower() in caps.szPname.lower():
            return idx, names
    return None, names


def _close():
    handle = _state["handle"]
    if handle is None:
        return
    try:
        winmm = _winmm()
        winmm.midiInStop(handle)
        winmm.midiInReset(handle)
        winmm.midiInClose(handle)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"K2: error al cerrar MIDI: {exc}")
    _state["handle"] = None
    # El callback se conserva hasta el proximo open: nunca liberarlo
    # mientras winmm pueda llamarlo.


def _open():
    winmm = _winmm()
    idx, names = _find_device(winmm, device_name.value)
    if idx is None:
        log.warning(
            f"K2: no encontre '{device_name.value}'. Dispositivos MIDI: {names}"
        )
        return False

    _close()
    callback = _make_callback()
    handle = ctypes.c_void_p()
    res = winmm.midiInOpen(
        ctypes.byref(handle),
        idx,
        ctypes.cast(callback, ctypes.c_void_p),
        0,
        CALLBACK_FUNCTION,
    )
    if res != MMSYSERR_NOERROR:
        log.error(
            f"K2: midiInOpen fallo (codigo {res}). "
            "Probablemente otro programa (K2 Deck?) tiene el puerto abierto."
        )
        return False

    res = winmm.midiInStart(handle)
    if res != MMSYSERR_NOERROR:
        winmm.midiInClose(handle)
        log.error(f"K2: midiInStart fallo (codigo {res})")
        return False

    _state["callback"] = callback
    _state["handle"] = handle
    log.info(f"K2: escuchando '{names[idx]}' canal {midi_channel.value} CC {cc_number.value}")
    return True


# ------------------------------------------------------------- main loop


@periodic(0.005)  # 200 Hz: solo revisa si llego un valor nuevo
def k2_collective():
    _state["filter"] = (midi_channel.value, cc_number.value)

    if _state["handle"] is None:
        now = time.monotonic()
        if now < _state["next_retry"]:
            return
        _state["next_retry"] = now + RETRY_SECONDS
        try:
            if not _open():
                return
        except OSError as exc:
            log.error(f"K2: {exc}")
            return

    with _state["lock"]:
        value = _state["pending"]
        _state["pending"] = None

    # Sin input nuevo, no hay salida.
    if value is None:
        return
    axis = cc_to_axis(value, invert.value, center_deadzone.value)
    if axis == _state["last_sent"]:
        return

    try:
        output_axis.remap(axis)
        _state["last_sent"] = axis
    except Exception as exc:  # noqa: BLE001
        log.error(f"K2: no pude escribir en vJoy: {exc}")

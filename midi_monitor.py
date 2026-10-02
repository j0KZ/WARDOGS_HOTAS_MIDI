# Monitor MIDI de solo lectura: imprime canal/CC/valor de lo que manda el K2.
# Uso: python midi_monitor.py [segundos] [parte del nombre del dispositivo]

import ctypes
import sys
import time

CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("dwSupport", ctypes.c_uint),
    ]


events = []

proto = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t
)


def on_midi(handle, msg, instance, param1, param2):
    if msg == MIM_DATA:
        events.append((time.monotonic(), int(param1)))


callback = proto(on_midi)

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30
name_part = sys.argv[2].lower() if len(sys.argv) > 2 else "xone:k2"

winmm = ctypes.WinDLL("winmm")
caps = MIDIINCAPSW()
idx = None
for i in range(winmm.midiInGetNumDevs()):
    winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
    if name_part in caps.szPname.lower():
        idx = i
        break
if idx is None:
    sys.exit(f"No encontre '{name_part}'")

handle = ctypes.c_void_p()
res = winmm.midiInOpen(ctypes.byref(handle), idx, ctypes.cast(callback, ctypes.c_void_p), 0, CALLBACK_FUNCTION)
if res:
    sys.exit(f"midiInOpen fallo (codigo {res}). K2 Deck abierto?")
winmm.midiInStart(handle)

print(f"Escuchando '{caps.szPname}' {seconds:.0f} s...", flush=True)
time.sleep(seconds)

winmm.midiInStop(handle)
winmm.midiInReset(handle)
winmm.midiInClose(handle)

summary = {}
for _, m in events:
    status, d1, d2 = m & 0xFF, (m >> 8) & 0x7F, (m >> 16) & 0x7F
    kind = {0xB0: "CC", 0x90: "NoteOn", 0x80: "NoteOff"}.get(status & 0xF0, hex(status & 0xF0))
    key = (kind, (status & 0x0F) + 1, d1)
    s = summary.setdefault(key, {"n": 0, "min": 127, "max": 0, "values": []})
    s["n"] += 1
    s["min"] = min(s["min"], d2)
    s["max"] = max(s["max"], d2)
    s["values"].append(d2)

print(f"Mensajes recibidos: {len(events)}")
for (kind, ch, d1), s in sorted(summary.items(), key=lambda kv: -kv[1]["n"]):
    print(f"{kind:7} canal {ch:2} num {d1:3}: {s['n']:4} msgs, rango {s['min']}-{s['max']}, primeros {s['values'][:8]}")

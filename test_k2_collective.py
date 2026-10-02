"""Pruebas en Linux con Gremlin y winmm simulados.

Lo que NO prueban: el winmm real, el vJoy real ni Gremlin real (Windows).
"""

import ctypes
import importlib
import sys
import types

import pytest

if not hasattr(ctypes, "WINFUNCTYPE"):
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE


# ---------------------------------------------------------- Gremlin stubs
class _Var:
    def __init__(self, name, desc, optional, initial=None, *a):
        self.name, self.value = name, initial


class _VInput(_Var):
    def __init__(self, name, desc, optional, valid_types):
        super().__init__(name, desc, optional)
        self.writes = []

    def remap(self, value):
        self.writes.append(value)


_periodic = {}


def periodic(interval):
    def wrap(fn):
        _periodic[fn.__name__] = fn
        return fn

    return wrap


gremlin = types.ModuleType("gremlin")
gtypes = types.ModuleType("gremlin.types")
gtypes.InputType = types.SimpleNamespace(JoystickAxis="axis")
us = types.ModuleType("gremlin.user_script")
us.BoolVariable = us.IntegerVariable = us.StringVariable = _Var
us.VirtualInputVariable = _VInput
us.periodic = periodic
sys.modules.update({"gremlin": gremlin, "gremlin.types": gtypes, "gremlin.user_script": us})


# ------------------------------------------------------------ fake winmm
class FakeWinmm:
    def __init__(self, names, open_result=0):
        self.names, self.open_result = names, open_result
        self.opened = self.closed = 0

    def midiInGetNumDevs(self):
        return len(self.names)

    def midiInGetDevCapsW(self, idx, pcaps, size):
        pcaps._obj.szPname = self.names[idx]
        return 0

    def midiInOpen(self, phandle, idx, cb, inst, flags):
        if self.open_result:
            return self.open_result
        phandle._obj.value = 1234
        self.opened += 1
        return 0

    def midiInStart(self, h):
        return 0

    def midiInStop(self, h):
        return 0

    def midiInReset(self, h):
        return 0

    def midiInClose(self, h):
        self.closed += 1
        return 0


@pytest.fixture
def mod(monkeypatch):
    if hasattr(sys, "_k2_collective_state"):
        delattr(sys, "_k2_collective_state")
    m = importlib.import_module("k2_collective")
    m = importlib.reload(m)
    return m


def msg(status, d1, d2):
    return status | (d1 << 8) | (d2 << 16)


def tick(m):
    _periodic["k2_collective"]()


# ------------------------------------------------------------------ tests
def test_parse_cc_matches_only_channel_and_cc(mod):
    assert mod.parse_cc(msg(0xBE, 19, 100), 15, 19) == 100  # canal 15 = 0xE
    assert mod.parse_cc(msg(0xB0, 19, 100), 15, 19) is None  # otro canal
    assert mod.parse_cc(msg(0xBE, 20, 100), 15, 19) is None  # otro CC
    assert mod.parse_cc(msg(0x9E, 19, 100), 15, 19) is None  # nota, no CC


def test_cc_to_axis_range_and_invert(mod):
    assert mod.cc_to_axis(0, False) == -1.0
    assert mod.cc_to_axis(127, False) == 1.0
    assert mod.cc_to_axis(64, False) == 0.0  # centro exacto
    assert mod.cc_to_axis(64, True) == 0.0
    assert mod.cc_to_axis(32, False) == -0.5
    assert mod.cc_to_axis(127, True) == -1.0
    assert mod.cc_to_axis(500, False) == 1.0  # clamp
    assert mod.cc_to_axis(-5, False) == -1.0


def test_center_deadzone(mod):
    for v in range(58, 71):  # 64 +/- 6
        assert mod.cc_to_axis(v, False, 6) == 0.0
    assert mod.cc_to_axis(71, False, 6) > 0  # sale de la zona sin salto
    assert mod.cc_to_axis(71, False, 6) < 0.02
    assert mod.cc_to_axis(57, False, 6) < 0
    assert mod.cc_to_axis(57, False, 6) > -0.02
    assert mod.cc_to_axis(0, False, 6) == -1.0  # extremos intactos
    assert mod.cc_to_axis(127, False, 6) == 1.0


def test_deadzone_moves_write_zero_once(mod, monkeypatch):
    fake = FakeWinmm(["XONE:K2"])
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    tick(mod)
    cb = mod._state["callback"]
    for v in (60, 62, 64, 66, 68):  # todo dentro de la zona
        cb(None, mod.MIM_DATA, 0, msg(0xBE, 19, v), 0)
        tick(mod)
    assert mod.output_axis.writes == [0.0]


def test_no_output_without_input(mod, monkeypatch):
    fake = FakeWinmm(["XONE:K2"])
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    for _ in range(10):
        tick(mod)
    assert fake.opened == 1
    assert mod.output_axis.writes == []


def test_fader_move_writes_once_and_dedupes(mod, monkeypatch):
    fake = FakeWinmm(["Mic", "XONE:K2"])
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    tick(mod)
    cb = mod._state["callback"]

    cb(None, mod.MIM_DATA, 0, msg(0xBE, 19, 127), 0)
    tick(mod)
    tick(mod)
    assert mod.output_axis.writes == [1.0]

    cb(None, mod.MIM_DATA, 0, msg(0xBE, 19, 127), 0)  # mismo valor
    tick(mod)
    assert mod.output_axis.writes == [1.0]

    cb(None, mod.MIM_DATA, 0, msg(0xBE, 20, 0), 0)  # otro control: ignorado
    tick(mod)
    assert mod.output_axis.writes == [1.0]

    cb(None, mod.MIM_DATA, 0, msg(0xBE, 19, 0), 0)
    tick(mod)
    assert mod.output_axis.writes == [1.0, -1.0]


def test_device_missing_retries_slowly(mod, monkeypatch):
    fake = FakeWinmm(["Mic"])
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    for _ in range(50):
        tick(mod)
    assert mod._state["handle"] is None
    assert fake.opened == 0
    assert mod._state["next_retry"] > 0  # espera RETRY_SECONDS antes de reintentar


def test_port_busy_logs_and_no_crash(mod, monkeypatch, caplog):
    fake = FakeWinmm(["XONE:K2"], open_result=4)  # MMSYSERR_ALLOCATED
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    tick(mod)
    assert mod._state["handle"] is None
    assert "K2 Deck" in caplog.text


def test_state_survives_reload(mod, monkeypatch):
    fake = FakeWinmm(["XONE:K2"])
    monkeypatch.setattr(mod, "_winmm", lambda: fake)
    tick(mod)
    cb_before = mod._state["callback"]
    m2 = importlib.reload(mod)
    assert m2._state["callback"] is cb_before  # el callback no se libera

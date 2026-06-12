"""adb_serial() must auto-`adb connect` a dropped WiFi (TCP) serial.

Regression: the Mi9T's ADB-over-WiFi session drops on any adb-server or
network hiccup; adb_serial() then raised DeviceNotConnected forever (the
bot showed 'waiting for battery') until someone manually ran `adb connect`.
"""
import pytest

pytest.importorskip("tomllib")  # device.py needs 3.11+; CI/worker has it
import device


@pytest.fixture(autouse=True)
def _reset_device_state(monkeypatch):
    monkeypatch.setattr(device, "_cached", None)
    monkeypatch.setattr(device, "_cached_at", 0.0)
    monkeypatch.setattr(device, "_last_tcp_connect", 0.0)
    monkeypatch.setattr(device, "_from_env", lambda: "192.168.60.18:5555")


def test_tcp_serial_reconnects_when_dropped(monkeypatch):
    calls = []
    # First aliveness check fails (session dropped), the post-connect
    # re-check succeeds.
    alive_results = iter([False, True])
    monkeypatch.setattr(device, "_cached_serial_alive",
                        lambda s: next(alive_results))

    def fake_check_output(cmd, **kw):
        calls.append(cmd)
        assert cmd == ["adb", "connect", "192.168.60.18:5555"]
        return b"connected to 192.168.60.18:5555\n"

    monkeypatch.setattr(device.subprocess, "check_output", fake_check_output)
    assert device.adb_serial() == "192.168.60.18:5555"
    assert calls, "adb connect was never attempted"


def test_tcp_reconnect_failure_still_raises(monkeypatch):
    monkeypatch.setattr(device, "_cached_serial_alive", lambda s: False)
    monkeypatch.setattr(
        device.subprocess, "check_output",
        lambda cmd, **kw: b"failed to connect to 192.168.60.18:5555\n")
    with pytest.raises(device.DeviceNotConnected):
        device.adb_serial()


def test_reconnect_attempts_are_throttled(monkeypatch):
    monkeypatch.setattr(device, "_cached_serial_alive", lambda s: False)
    calls = []

    def fake_check_output(cmd, **kw):
        calls.append(cmd)
        return b"failed to connect\n"

    monkeypatch.setattr(device.subprocess, "check_output", fake_check_output)
    for _ in range(5):
        with pytest.raises(device.DeviceNotConnected):
            device.adb_serial()
    assert len(calls) == 1, f"expected 1 throttled attempt, got {len(calls)}"


def test_usb_serial_never_tries_adb_connect(monkeypatch):
    monkeypatch.setattr(device, "_from_env", lambda: "ABCDEF123")
    monkeypatch.setattr(device, "_cached_serial_alive", lambda s: False)
    calls = []

    def fake_check_output(cmd, **kw):
        calls.append(cmd)
        return b""

    monkeypatch.setattr(device.subprocess, "check_output", fake_check_output)
    with pytest.raises(device.DeviceNotConnected):
        device.adb_serial()
    assert not calls, "adb connect must not run for USB serials"

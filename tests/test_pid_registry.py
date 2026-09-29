from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

import pytest

from ecusdk.config import load_vehicle
from ecusdk.errors import ObdError
from ecusdk.obd import ObdRegistry, PidCodec
from ecusdk.simulation import ECU


def ecu(signals: dict[str, int | float] | None = None) -> ECU:
    return ECU("ecm", 0x7E0, 0x7E8, signals or {})


def test_builtin_pid_uses_current_signal_and_keeps_legacy_missing_default() -> None:
    engine = ecu({"rpm": 1000, "speed": 42})
    assert engine.obd_payload(b"\x01\x0c") == bytes.fromhex("410c0fa0")
    engine.set_signal("rpm", 1500)
    assert engine.obd_payload(b"\x01\x0c") == bytes.fromhex("410c1770")
    assert ecu().obd_payload(b"\x01\x0c") == bytes.fromhex("410c0000")
    assert ecu().obd_payload(b"\x01\x0d") == bytes.fromhex("410d00")


def test_bindings_are_per_ecu_and_can_be_rebound() -> None:
    first, second = ecu({"rpm": 100, "engine": 200}), ecu({"rpm": 300})
    first.obd.pid(0x0C, source="engine")
    assert first.obd_payload(b"\x01\x0c") == bytes.fromhex("410c0320")
    assert second.obd_payload(b"\x01\x0c") == bytes.fromhex("410c04b0")
    first.obd.pid(0x0C, source="rpm")
    assert first.obd_payload(b"\x01\x0c") == bytes.fromhex("410c0190")
    with pytest.raises(ObdError):
        first.obd.pid(0x0C, source="absent")


def test_custom_codec_requires_explicit_binding() -> None:
    engine = ecu({"temperature": 37})
    codec = PidCodec(0x50, 1, "C", 0, 255, 1, "temperature")
    engine.obd.register(codec)
    assert engine.obd_payload(b"\x01\x50") is None
    engine.obd.pid(0x50, source="temperature")
    assert engine.obd_payload(b"\x01\x50") == bytes.fromhex("415025")
    assert engine.obd.encode(0x51) is None


def test_pid_rejects_invalid_identifiers_and_empty_source() -> None:
    engine = ecu({"rpm": 100})
    for identifier in (-1, 256, True):
        with pytest.raises(ValueError):
            engine.obd.pid(identifier, source="rpm")
    with pytest.raises(ValueError):
        engine.obd.pid(0x0C, source="")


def test_register_rejects_non_codec_runtime_input() -> None:
    engine = ecu()
    invalid_codec = cast(PidCodec, object())
    with pytest.raises(TypeError):
        engine.obd.register(invalid_codec)


def test_registration_requires_deliberate_replacement_and_binding_survives_reset() -> (
    None
):
    engine = ecu({"rpm": 100, "engine": 200})
    engine.obd.pid(0x0C, source="engine")
    with pytest.raises(ObdError):
        engine.obd.register(PidCodec(0x0C, 2, "rpm", 0, 16383.75, 4, "rpm"))
    assert engine.obd_payload(b"\x01\x0c") == bytes.fromhex("410c0320")
    engine.obd.register(
        PidCodec(0x0C, 2, "half-rpm", 0, 32767.5, 2, "rpm"), replace=True
    )
    engine.set_signal("engine", 400)
    engine.reset()
    assert engine.obd_payload(b"\x01\x0c") == bytes.fromhex("410c0190")


def test_deleted_explicit_source_errors_on_encode_and_replace() -> None:
    signals: MutableMapping[str, int | float] = {"engine_rpm": 500}
    registry = ObdRegistry(signals)
    registry.pid(0x0C, source="engine_rpm")
    del signals["engine_rpm"]
    with pytest.raises(ObdError):
        registry.encode(0x0C)
    with pytest.raises(ObdError):
        registry.register(PidCodec(0x0C, 2, "rpm", 0, 16383.75, 4, "rpm"), replace=True)


def _config(obd: str) -> str:
    return "\n".join(
        (
            "[vehicle]",
            'name = "car"',
            "[ecus.ecm.can]",
            "request_id = 2016",
            "response_id = 2024",
            "[ecus.ecm.signals.rpm]",
            "initial = 900",
            "[ecus.ecm.signals.engine_rpm]",
            "initial = 1800",
            "[ecus.ecm.obd]",
            obd,
            "",
        )
    )


def test_toml_pid_mapping_reaches_mode01_request(tmp_path: Path) -> None:
    path = tmp_path / "car.toml"
    path.write_text(_config('"01:0C"="engine_rpm"'))
    vehicle = load_vehicle(path)
    assert vehicle.ecus[0].obd_payload(b"\x01\x0c") == bytes.fromhex("410c1c20")


@pytest.mark.parametrize(
    "mapping",
    [
        '"02:0C"="rpm"',
        '"01:99"="rpm"',
        '"1:0C"="rpm"',
        '"01:0C"="missing"',
        '"01:0C"=12',
        '"01:0C"="rpm"\n"01:0c"="rpm"',
    ],
)
def test_toml_rejects_invalid_pid_mappings(tmp_path: Path, mapping: str) -> None:
    path = tmp_path / "car.toml"
    path.write_text(_config(mapping))
    with pytest.raises((ValueError, ObdError)):
        load_vehicle(path)


def test_toml_rejects_non_table_obd_configuration(tmp_path: Path) -> None:
    path = tmp_path / "car.toml"
    path.write_text(
        '[vehicle]\nname="car"\n[ecus.ecm]\nobd=7\n'
        "[ecus.ecm.can]\nrequest_id=2016\nresponse_id=2024\n"
    )
    with pytest.raises(ValueError):
        load_vehicle(path)

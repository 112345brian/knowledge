"""The client sources' record-shaped contracts (music, measurements, claims): what `test_build_contracts` checked for
them while they sat in the core's `build_repositories_port` and `build_records`. The shapes are unchanged; the owner
is `client/`."""
import dataclasses
import inspect
import typing

import pytest

import wiring
from client import claims_port, measurements_port, measurements_records, music_port, music_records
from fake_client_repositories import (FakeClaims, FakeClientState, FakeFactMeasurementLinks, FakeMeasurements,
                                      FakeMusic)

SQL_WORDS = {"execute", "executemany", "executescript", "cursor", "sql", "query", "queries", "statement_sql", "con",
             "conn", "connection", "cur", "row", "rows", "raw"}

PORTS = {
    "measurements": (measurements_port.MeasurementRecordRepository, FakeMeasurements),
    "fact_measurement_links": (measurements_port.FactMeasurementRecordLinks, FakeFactMeasurementLinks),
    "claims": (claims_port.ClaimRepository, FakeClaims),
    "music": (music_port.MusicRecordRepository, FakeMusic),
}


def _methods(proto):
    return {n: v for n, v in vars(proto).items() if callable(v) and not n.startswith("_")}


@pytest.mark.parametrize("name", sorted(PORTS))
def test_a_client_record_port_is_sql_free_and_annotated(name):
    proto, _ = PORTS[name]
    for method, fn in _methods(proto).items():
        assert method.lower() not in SQL_WORDS and "sql" not in method.lower() and "execute" not in method.lower()
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        assert not set(params) & SQL_WORDS, f"{name}.{method}"
        hints = typing.get_type_hints(fn)
        assert "return" in hints and all(p in hints for p in params), f"{name}.{method} is not fully annotated"
        text = str(hints)
        assert "sqlite3" not in text and "Cursor" not in text and "Connection" not in text


@pytest.mark.parametrize("name", sorted(PORTS))
def test_the_client_fakes_satisfy_their_ports_by_name_and_signature(name):
    proto, fake = PORTS[name]
    assert wiring.conformance_problems(name, fake(FakeClientState()), {name: proto}) == []


@pytest.mark.parametrize("module", [music_records, measurements_records, claims_port])
def test_every_client_record_is_a_frozen_dataclass(module):
    records = [c for _, c in inspect.getmembers(module, inspect.isclass)
               if c.__module__ == module.__name__ and dataclasses.is_dataclass(c)]
    assert records
    for cls in records:
        assert cls.__dataclass_params__.frozen, cls.__name__   # type: ignore[attr-defined]


def test_the_cores_wiring_registry_names_no_client_port():
    """A client source passes its own ports to wiring (`ports=`); the core's registry may not list them."""
    for key, proto in wiring.PORT_PROTOCOLS.items():
        assert not proto.__module__.startswith("client"), f"{key}: {proto.__module__}"

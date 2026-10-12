"""Static check (``uv run mypy``) that the in-memory fakes satisfy the CLIENT sources' record-shaped ports
structurally (the core's are in build_port_types)."""
from client.claims_port import ClaimRepository
from client.measurements_port import FactMeasurementRecordLinks, MeasurementRecordRepository
from client.music_port import MusicRecordRepository
from fake_client_repositories import (FakeClaims, FakeClientState, FakeFactMeasurementLinks, FakeMeasurements,
                                      FakeMusic)

_s = FakeClientState()
measurements: MeasurementRecordRepository = FakeMeasurements(_s)
fact_measurement_links: FactMeasurementRecordLinks = FakeFactMeasurementLinks(_s)
claims: ClaimRepository = FakeClaims(_s)
music: MusicRecordRepository = FakeMusic(_s)

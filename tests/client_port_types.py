"""Static check (``uv run mypy``) that the in-memory fakes satisfy the CLIENT sources' record-shaped ports
structurally (the core's are in build_port_types)."""
from client.claims_port import ClaimRepository
from client.measurements_port import MeasurementRepository
from client.music_port import MusicRepository
from fake_client_repositories import (FakeClaims, FakeClientState, FakeMeasurements, FakeMusic)

_s = FakeClientState()
measurements: MeasurementRepository = FakeMeasurements(_s)
claims: ClaimRepository = FakeClaims(_s)
music: MusicRepository = FakeMusic(_s)

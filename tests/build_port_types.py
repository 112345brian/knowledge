"""Static check (``uv run mypy``) that the in-memory fakes satisfy the build ports structurally.

Runtime name/signature conformance is tests/test_build_contracts.py; these assignments add mypy's structural
type check of parameter and return types.
"""
from build_composition import SessionOpener
from build_repositories_port import (ClaimRepository, EntityRepository, FactRepository, MeasurementRepository,
                                     MusicRepository, PrivacyRepository, RevisionRepository, SourceRepository,
                                     SubjectRepository, UnitOfWork)
from build_steps_port import Diagnostics, VaultOpener, VaultReader
from fake_build_repositories import (CollectingDiagnostics, FakeClaims, FakeEntities, FakeFacts, FakeMeasurements,
                                     FakeMusic, FakePrivacy, FakeRevisions, FakeSessionOpener, FakeSources,
                                     FakeState, FakeSubjects, FakeUnitOfWork, FakeVault)

_s = FakeState()
sources: SourceRepository = FakeSources(_s)
subjects: SubjectRepository = FakeSubjects(_s)
facts: FactRepository = FakeFacts(_s)
revisions: RevisionRepository = FakeRevisions(_s)
privacy: PrivacyRepository = FakePrivacy(_s)
entities: EntityRepository = FakeEntities(_s)
measurements: MeasurementRepository = FakeMeasurements(_s)
claims: ClaimRepository = FakeClaims(_s)
music: MusicRepository = FakeMusic(_s)
unit_of_work: UnitOfWork = FakeUnitOfWork(_s)
diagnostics: Diagnostics = CollectingDiagnostics()
opener: SessionOpener = FakeSessionOpener()
vault: VaultReader = FakeVault()
vault_opener: VaultOpener = FakeVault()

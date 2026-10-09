import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Fixtures here pop and re-import repo modules (paths, privacy, revisions, ...) to get fresh copies.
    Put sys.modules back after every test so a test module's top-level `import privacy` and the code
    under test in the next test never hold two different copies of the same class."""
    saved = dict(sys.modules)
    yield
    for name in [n for n in sys.modules if n not in saved]:
        del sys.modules[name]
    for name, mod in saved.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod

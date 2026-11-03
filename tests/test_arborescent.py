"""Arborescent's own checks (rings, external allow-lists, effects, annotations) pass for this repository.

The policy is `[tool.arborescent]` in pyproject.toml: rings and each module's ring come from tach.toml, every module
declares its ring in code (`__arbor__`), and the findings that existed when it was adopted are recorded in
arborescent-baseline.json, so only a NEW finding fails here. Core mode runs Arborescent's own engine only; tach and
import-linter have their own tests in test_architecture.py.
"""
import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_arborescent_check_passes():
    exe = shutil.which("arborescent", path=os.path.join(REPO, ".venv", "bin") + os.pathsep + os.environ.get("PATH", ""))
    assert exe, "arborescent is a dev dependency: run `uv sync`"
    p = subprocess.run([exe, "check", "--engines", "core"], cwd=REPO, capture_output=True, text=True)
    assert p.returncode == 0, "arborescent check failed (uv run arborescent check):\n" + p.stdout[-4000:] + p.stderr[-1000:]


def test_every_module_declares_its_ring_in_code_and_it_is_the_one_tach_enforces():
    """`__arbor__` is the in-code declaration; tach.toml's layer is the enforced one. Arborescent reports a mismatch
    (`arbor-ring-mismatch`), and this pins that no module is left relying on the `classify` fallback."""
    p = subprocess.run([shutil.which("arborescent", path=os.path.join(REPO, ".venv", "bin")) or "arborescent", "discover"],
                       cwd=REPO, capture_output=True, text=True)
    assert p.returncode == 0 and "no unclassified" in p.stdout, p.stdout + p.stderr

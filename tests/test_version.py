"""The version is written in three places; they must say the same thing, and the first is the one a release bumps.

`pyproject.toml` is the source of truth. `uv.lock` records the project's own version, and the MCP server reports
`mcp_rules.SERVER_VERSION` to every client that connects (a pure rule module cannot read pyproject at run time).
"""
import os
import re
import tomllib

import mcp_rules

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_the_version_is_a_plain_semantic_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pyproject_version())


def test_the_mcp_server_reports_the_project_version():
    assert mcp_rules.SERVER_VERSION == _pyproject_version()


def test_the_lock_file_records_the_project_version():
    with open(os.path.join(REPO, "uv.lock"), "rb") as f:
        packages = tomllib.load(f)["package"]
    mine = [p for p in packages if p["name"] == "knowledge"]
    assert len(mine) == 1 and mine[0]["version"] == _pyproject_version(), "run `uv lock` after changing the version"

"""Keep the generated regions of README.md and docs/*.md in step with the code: `build` rewrites them, `check` reports drift.

The rules (`docs_rules`) decide what each region says and how it is fenced; this service reads the inputs and the
docs through the `DocsFiles` port and compares or writes. It never prints or exits. `build` computes every file
before writing any, so a damaged marker leaves the tree untouched.
"""
from dataclasses import dataclass
from typing import Dict, List, Sequence

import docs_rules as rules
from docs_files_port import DocsFiles


@dataclass(frozen=True)
class DocsDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    docs_files: DocsFiles


def _inputs(deps, commands: Sequence[rules.CommandDoc]) -> rules.Inputs:
    return rules.Inputs(
        tach_text=deps.docs_files.read("tach.toml"),
        pyproject_text=deps.docs_files.read("pyproject.toml"),
        module_sources=deps.docs_files.module_sources(),
        commands=tuple(commands),
    )


def check(deps, commands: Sequence[rules.CommandDoc]) -> List[str]:
    """What is out of date, as `path: region` lines (empty: every generated region is current). Raises
    `DocsError` only when the inputs cannot be rendered at all."""
    rendered = rules.render(_inputs(deps, commands))
    problems = []
    for path, names in sorted(rules.TARGETS.items()):
        try:
            text = deps.docs_files.read(path)
        except FileNotFoundError:
            problems.append(f"{path}: file is missing")
            continue
        for name in rules.region_names(text):
            if name not in names:
                problems.append(f"{path}: {name} (no generator by that name)")
        for name in names:
            try:
                current = rules.region_is_current(text, name, rendered[name])
            except rules.DocsError as e:
                problems.append(f"{path}: {name} ({e})")
                continue
            if not current:
                problems.append(f"{path}: {name}")
    return problems


def build(deps, commands: Sequence[rules.CommandDoc]) -> List[str]:
    """Rewrite every stale region; return the paths changed. Raises `DocsError`, writing nothing, if a target file
    is missing or a region's markers are absent or damaged."""
    rendered = rules.render(_inputs(deps, commands))
    updated: Dict[str, str] = {}
    for path, names in sorted(rules.TARGETS.items()):
        try:
            original = deps.docs_files.read(path)
        except FileNotFoundError as e:
            raise rules.DocsError(f"{path}: file is missing") from e
        text = original
        for name in names:
            text = rules.replace_region(text, name, rendered[name])
        if text != original:
            updated[path] = text
    for path, text in updated.items():
        deps.docs_files.write(path, text)
    return sorted(updated)

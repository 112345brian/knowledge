"""`knowledge.py privacy check | rules | tag | untag | add-keyword | remove-keyword`: the privacy rules. Thin printers
over `ctx.obj.rules_edit` and `ctx.obj.queries`; registered with `app.add_typer(cli_privacy.app, name="privacy")`."""
import enum
import privacy
import sys
from private_git_port import PrivateGitError

import typer

from cli_common import (ALLOW_DIRTY_OPT, DRY_RUN_OPT, JSON_OPT, _emit_json, _fail)

app = typer.Typer(help="Privacy rules: check what visibility a statement would get, and edit the rules "
                               "(subject tags and keywords). The rules file is edited only through these commands.",
                          pretty_exceptions_enable=False, rich_markup_mode=None)


class Requested(str, enum.Enum):
    normal = "normal"
    private = "private"


def _load_rules(c):
    try:
        return c.rules_edit.load_rules()
    except privacy.PrivacyRulesError as e:
        _fail(e)


def _db_context(c, rules):
    """`rules` with the subject tree and the known-subject list from the live db (see
    queries.subject_context). No db -> empty context, so the unknown-subject rule is not enforced
    (nothing to compare against)."""
    ctx = c.queries.subject_context()
    if ctx is None:
        return rules
    parents, known = ctx
    return rules.with_context(parents=parents, known_subjects=known)


def _resolution_json(res):
    return {"visibility": res.visibility, "raised_above_request": res.raised_above_request,
            "explanation": res.explain(),
            "reasons": [{"kind": r.kind, "detail": r.detail, "forces_private": r.forces_private} for r in res.reasons]}


@app.command("check", help="Resolve the visibility a statement would be stored with, and name the rule that raised it.")
def cmd_privacy_check(ctx: typer.Context, statement: str,
                      subject: str = typer.Option(..., "--subject", help="Subject the fact would be filed under."),
                      requested: Requested = typer.Option(Requested.normal, "--requested",
                                                          help="What the caller asks for; default normal shows what the rules alone do."),
                      as_json: bool = JSON_OPT):
    c = ctx.obj
    res = privacy.check(subject, statement, _db_context(c, _load_rules(c)), requested=requested.value)
    _emit_json(_resolution_json(res)) if as_json else print(res.explain())


@app.command("rules", help="Show the loaded privacy rules (subject tags and keywords).")
def cmd_privacy_rules(ctx: typer.Context, as_json: bool = JSON_OPT):
    c = ctx.obj
    path = c.rules_edit.rules_path()
    rules = _load_rules(c)
    exists = c.rules_edit.rules_file_exists(path)
    if as_json:
        _emit_json({"path": path, "exists": exists, "version": privacy.VERSION,
                    "subject_tags": dict(sorted(rules.subject_tags.items())), "keywords": sorted(rules.keywords)})
        return
    print(f"Privacy rules: {path}" + ("" if exists else "  (no rules file; empty rules)"))
    if rules.subject_tags:
        print("subject tags:")
        for name, tag in sorted(rules.subject_tags.items()):
            print(f"  {name}: {tag}")
    else:
        print("subject tags: (none)")
    if rules.keywords:
        print("keywords:")
        for kw in sorted(rules.keywords):
            print(f"  - {kw}")
    else:
        print("keywords: (none)")


def _edit_rules(c, edit, describe, allow_dirty, dry_run):
    """Shared body of the rules-editing commands: run rules_edit.edit_rules and print what it did."""
    try:
        res = c.rules_edit.edit_rules(edit, describe, allow_dirty, dry_run)
    except privacy.PrivacyRulesError as e:
        _fail(e)
    except PrivateGitError as e:
        _fail(e)
    if not res.changed:
        print(f"no change: {res.path} already has this rule state")
        return
    if res.not_in_git:
        print(f"note: {res.directory} is not inside a git repository; the change will not be committed.", file=sys.stderr)
    if res.dry_run:
        print(f"dry run: would {res.what} in {res.path}" + (f" and commit {res.message!r}" if res.repo else "") + "; nothing written")
        return
    print(f"Updated {res.path}: {res.what}.")
    if res.repo is None:
        return
    if res.commit_error:
        print(f"error: the rule IS written to {res.path} but is NOT committed: {res.commit_error}", file=sys.stderr)
        raise typer.Exit(3)
    print(f"Committed {res.committed} in {res.repo}: {res.message}")
    if res.detached:
        print(f"warning: {res.repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)


class Tag(str, enum.Enum):
    private = "private"
    normal = "normal"


@app.command("tag", help="Tag a subject private (inherited by its children); --tag normal only registers it as a known subject.")
def cmd_privacy_tag(ctx: typer.Context, subject: str, tag: Tag = typer.Option(Tag.private, "--tag"),
                    allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    c = ctx.obj
    _edit_rules(c, lambda r: privacy.tag_subject(r, subject, tag.value),
                lambda old, new: (f"tag subject {subject} {tag.value}", f"privacy: tag {subject} ({tag.value})"),
                allow_dirty, dry_run)


@app.command("untag", help="Remove a subject's tag.")
def cmd_privacy_untag(ctx: typer.Context, subject: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    c = ctx.obj
    _edit_rules(c, lambda r: privacy.untag_subject(r, subject),
                lambda old, new: (f"untag subject {subject}", f"privacy: untag {subject}"),
                allow_dirty, dry_run)


@app.command("add-keyword", help="Add a name/keyword; statements containing it as a whole word become private. Literal text, case-insensitive.")
def cmd_privacy_add_keyword(ctx: typer.Context, keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    c = ctx.obj
    def describe(old, new):
        kw = next(k for k in new.keywords if k not in old.keywords)
        return f"add keyword {kw!r}", f"privacy: add keyword {kw!r}"
    _edit_rules(c, lambda r: privacy.add_keyword(r, keyword), describe, allow_dirty, dry_run)


@app.command("remove-keyword", help="Remove a keyword. Facts already stored private stay private.")
def cmd_privacy_remove_keyword(ctx: typer.Context, keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    c = ctx.obj
    def describe(old, new):
        kw = next(k for k in old.keywords if k not in new.keywords)
        return f"remove keyword {kw!r}", f"privacy: remove keyword {kw!r}"
    _edit_rules(c, lambda r: privacy.remove_keyword(r, keyword), describe, allow_dirty, dry_run)

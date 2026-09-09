"""Phase 7 MCP wrapper - a thin server over the CLI verbs (docs/Architecture.md section 7).

Design (issue #7, settled in its `decision:` comment and `## Implementation plan`):

* **No business logic here.** Each tool builds the argv it would have typed, parses it with
  :func:`filingcabinet.cli.build_parser`, dispatches ``args.func(args)`` in-process with stdout
  captured, and returns the parsed ``--json`` payload. Every flag default, config/db resolution
  rule and payload shape therefore stays identical to the CLI *by construction*.
* **Through the CLI parser, not the engine.** The sibling project pemr calls its engine
  functions directly, but filingcabinet assembles each ``--json`` payload inside the matching
  ``cmd_*`` in :mod:`filingcabinet.cli` (plan_id minting, plan-file writing, the taxonomy
  fields); a direct-engine wrapper would fork that assembly and drift from the CLI.
* **In-process, never a subprocess** (import cost, Windows quoting, lost exceptions).
* **No new mutation path.** ``apply`` requires an existing plan file and ``undo`` a ``plan_id``,
  both forwarded verbatim to the same ``cmd_apply`` / ``cmd_undo`` the CLI calls. There is no
  combined propose-then-apply tool and no default plan discovery: an agent must name a plan a
  human can read first (docs/Architecture.md section 6 - tools never move documents unasked).
* **The ``mcp`` SDK is an optional dependency** (``pip install filingcabinet[mcp]``). Nothing
  outside :func:`build_server` / :func:`main` imports it, so the tool functions - and their
  tests - run with the SDK absent.

Run it: ``filingcabinet-mcp`` or ``python -m filingcabinet.mcp_server`` (stdio transport).
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any

from . import __version__, cli

# Every domain exception `cli.main` catches, so a tool reports what the CLI would print.
_DOMAIN_ERRORS = (
    cli.apply_mod.PlanError,
    cli.db.NotMigratedError,
    cli.snapshot_mod.SnapshotError,
    cli.taxonomy_mod.TaxonomyError,
)


class ToolError(RuntimeError):
    """A friendly, expected tool failure (missing db, unusable plan, bad taxonomy).

    The stdio server maps this to an MCP tool error; tests assert it directly. It is
    deliberately distinct from an unexpected crash so the wrapper never leaks a raw traceback
    for the ordinary "you asked for something that isn't there" cases.
    """


def _flags(**kwargs: Any) -> list[str]:
    """Turn keyword arguments into CLI flags: ``None`` disappears, ``True`` is a bare flag.

    Underscores become dashes, so ``max_distance=3`` is ``--max-distance 3``. Keeping flag
    spelling in one place is what lets each tool below stay a one-liner.
    """
    argv: list[str] = []
    for name, value in kwargs.items():
        if value is None or value is False:
            continue
        flag = "--" + name.replace("_", "-")
        if value is True:
            argv.append(flag)
        else:
            argv.append(flag)
            argv.append(str(value))
    return argv


def _run_json(argv: list[str]) -> dict:
    """The single seam: run one CLI invocation in-process and return its ``--json`` payload.

    ``--json`` and the global flags precede the verb, exactly as on the command line. stdout is
    captured (stderr deliberately is not - an engine warning stays a warning), and restored on
    every path.
    """
    parser = cli.build_parser()
    buffer = io.StringIO()
    try:
        args = parser.parse_args(["--json", *argv])
        with contextlib.redirect_stdout(buffer):
            code = args.func(args)
    except SystemExit as exc:  # argparse usage errors and the CLI's own `raise SystemExit`
        message = str(exc.code) if exc.code not in (None, 0) else "command failed"
        raise ToolError(message) from exc
    except _DOMAIN_ERRORS as exc:
        raise ToolError(str(exc)) from exc
    if code:
        raise ToolError(f"{argv[0] if argv else 'command'} failed with exit code {code}")
    output = buffer.getvalue().strip()
    if not output:
        raise ToolError(f"{argv[0] if argv else 'command'} produced no output")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ToolError(f"unparseable output from `{' '.join(argv)}`: {output[:200]}") from exc


def _globals(db: str | None, config: str | None) -> list[str]:
    """The global flags every tool accepts, resolved by the CLI exactly as for a shell user."""
    return _flags(db=db, config=config)


# --------------------------------------------------------------------------- #
# Tools - one per CLI verb, parameters mirroring that verb's flags
# --------------------------------------------------------------------------- #


def ingest(
    root: str | None = None, db: str | None = None, config: str | None = None
) -> dict:
    """[write] Scan the document root and index new or changed files. Mirrors `fc ingest`."""
    return _run_json([*_globals(db, config), "ingest", *_flags(root=root)])


def status(db: str | None = None, config: str | None = None) -> dict:
    """[read] Index summary: db path, migration state, document count. Mirrors `fc status`."""
    return _run_json([*_globals(db, config), "status"])


def find(
    query: str,
    limit: int | None = None,
    db: str | None = None,
    config: str | None = None,
) -> dict:
    """[read] Full-text search over OCR text. Mirrors `fc find`."""
    return _run_json([*_globals(db, config), "find", query, *_flags(limit=limit)])


def dupes(
    root: str | None = None,
    max_distance: int | None = None,
    db: str | None = None,
    config: str | None = None,
) -> dict:
    """[read] Exact, near-duplicate and subset tiers. Mirrors `fc dupes report`."""
    return _run_json(
        [
            *_globals(db, config),
            "dupes",
            "report",
            *_flags(root=root, max_distance=max_distance),
        ]
    )


def propose(
    root: str | None = None,
    taxonomy: str | None = None,
    plan_dir: str | None = None,
    out: str | None = None,
    limit: int | None = None,
    document: int | None = None,
    dry_run: bool = False,
    db: str | None = None,
    config: str | None = None,
) -> dict:
    """[write: a plan file, never a document] Plan renames/moves. Mirrors `fc propose`.

    ``plan_dir`` and ``out`` are mutually exclusive in the parser; passing both is a `ToolError`.
    The plan file always lands outside the document root - the CLI's own guard, unchanged.
    """
    return _run_json(
        [
            *_globals(db, config),
            "propose",
            *_flags(
                root=root,
                taxonomy=taxonomy,
                plan_dir=plan_dir,
                out=out,
                limit=limit,
                document=document,
                dry_run=dry_run,
            ),
        ]
    )


def apply(
    plan: str,
    root: str | None = None,
    dry_run: bool = False,
    db: str | None = None,
    config: str | None = None,
) -> dict:
    """[write] Execute an *already written* plan file. Mirrors `fc apply`.

    ``plan`` is required, exactly as on the CLI: this tool can only execute a plan that already
    exists on disk, so no move is ever invented by the wrapper (docs/Architecture.md section 6).
    """
    return _run_json(
        [*_globals(db, config), "apply", plan, *_flags(root=root, dry_run=dry_run)]
    )


def undo(
    plan_id: str,
    root: str | None = None,
    dry_run: bool = False,
    db: str | None = None,
    config: str | None = None,
) -> dict:
    """[write] Reverse every un-undone move of one plan from the move log. Mirrors `fc undo`."""
    return _run_json(
        [*_globals(db, config), "undo", plan_id, *_flags(root=root, dry_run=dry_run)]
    )


TOOL_NAMES = ("ingest", "status", "find", "dupes", "propose", "apply", "undo")


# --------------------------------------------------------------------------- #
# stdio server - the only code that needs the optional `mcp` SDK
# --------------------------------------------------------------------------- #


def build_server():  # pragma: no cover - exercised only with the mcp SDK installed
    """Construct the FastMCP server, registering every verb with its read/write annotation."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # friendly nudge, not a traceback
        raise SystemExit(
            "error: the MCP server needs the optional `mcp` SDK - install with "
            "`pip install filingcabinet[mcp]` (or `pip install mcp`)."
        ) from exc

    server = FastMCP("filingcabinet")

    # `serverInfo.version` - what a client UI shows the human. FastMCP takes no `version=`
    # argument and the low-level server it wraps falls back to the *SDK's* own package
    # version, so an unset version advertises the `mcp` release instead of filingcabinet's.
    # The attribute is read at initialize time, so setting it here is equivalent.
    server._mcp_server.version = __version__

    ro = {"readOnlyHint": True}
    rw = {"readOnlyHint": False}

    # Explicit `name=` per tool so the wire surface equals the CLI verb an agent reads about in
    # the docs; FastMCP would otherwise register under the wrapper function's name.

    @server.tool(name="status", annotations=ro)
    def status_tool(db: str | None = None, config: str | None = None) -> dict:
        return status(db=db, config=config)

    @server.tool(name="find", annotations=ro)
    def find_tool(
        query: str, limit: int | None = None, db: str | None = None, config: str | None = None
    ) -> dict:
        return find(query, limit=limit, db=db, config=config)

    @server.tool(name="dupes", annotations=ro)
    def dupes_tool(
        root: str | None = None,
        max_distance: int | None = None,
        db: str | None = None,
        config: str | None = None,
    ) -> dict:
        return dupes(root=root, max_distance=max_distance, db=db, config=config)

    @server.tool(name="ingest", annotations=rw)
    def ingest_tool(
        root: str | None = None, db: str | None = None, config: str | None = None
    ) -> dict:
        return ingest(root=root, db=db, config=config)

    @server.tool(name="propose", annotations=rw)
    def propose_tool(
        root: str | None = None,
        taxonomy: str | None = None,
        plan_dir: str | None = None,
        out: str | None = None,
        limit: int | None = None,
        document: int | None = None,
        dry_run: bool = False,
        db: str | None = None,
        config: str | None = None,
    ) -> dict:
        return propose(
            root=root,
            taxonomy=taxonomy,
            plan_dir=plan_dir,
            out=out,
            limit=limit,
            document=document,
            dry_run=dry_run,
            db=db,
            config=config,
        )

    @server.tool(name="apply", annotations=rw)
    def apply_tool(
        plan: str,
        root: str | None = None,
        dry_run: bool = False,
        db: str | None = None,
        config: str | None = None,
    ) -> dict:
        return apply(plan, root=root, dry_run=dry_run, db=db, config=config)

    @server.tool(name="undo", annotations=rw)
    def undo_tool(
        plan_id: str,
        root: str | None = None,
        dry_run: bool = False,
        db: str | None = None,
        config: str | None = None,
    ) -> dict:
        return undo(plan_id, root=root, dry_run=dry_run, db=db, config=config)

    return server


def main() -> None:  # pragma: no cover - stdio entry point
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()

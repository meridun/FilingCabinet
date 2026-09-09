"""Phase 7 unit tests: the MCP tool functions (docs/Architecture.md section 7).

These exercise the tool functions directly, not the wire protocol, so the whole file runs with
the optional ``mcp`` SDK absent - which is itself one of the acceptance criteria. The other
invariant under test is section 6: the ``apply`` tool moves nothing without a plan file that
already exists and was built for this root.

Fixtures are synthesized under ``tmp_path``; nothing lands in the repo (section 8).
"""

import json
import subprocess
import sys

import pytest

from filingcabinet import cli, mcp_server


def _corpus(tmp_path):
    """A migrated index plus a one-document root, and the paths the tools need."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "scan-001.pdf").write_bytes(b"%PDF-1.4 northwind invoice")
    db_path = tmp_path / "index.db"
    assert cli.main(["--db", str(db_path), "--json", "migrate", "--create"]) == 0
    return root, db_path


def _plans(tmp_path):
    plan_dir = tmp_path / "plans"  # deliberately outside the document root
    plan_dir.mkdir()
    return plan_dir


def test_status_matches_cli_json(tmp_path, capsys):
    _root, db_path = _corpus(tmp_path)
    capsys.readouterr()

    assert cli.main(["--db", str(db_path), "--json", "status"]) == 0
    from_cli = json.loads(capsys.readouterr().out)

    assert mcp_server.status(db=str(db_path)) == from_cli


def test_ingest_then_find_round_trip(tmp_path):
    root, db_path = _corpus(tmp_path)

    ingested = mcp_server.ingest(root=str(root), db=str(db_path))
    assert ingested["new"] == 1

    hits = mcp_server.find("northwind", db=str(db_path))
    assert hits["count"] == 0  # nothing OCRed yet: a report, not a failure
    assert hits["hits"] == []


def test_missing_database_raises_tool_error(tmp_path):
    with pytest.raises(mcp_server.ToolError) as excinfo:
        mcp_server.status(db=str(tmp_path / "nope.db"))
    assert "no database" in str(excinfo.value)


def test_propose_dry_run_writes_no_plan(tmp_path):
    root, db_path = _corpus(tmp_path)
    plan_dir = _plans(tmp_path)
    mcp_server.ingest(root=str(root), db=str(db_path))

    result = mcp_server.propose(
        root=str(root), plan_dir=str(plan_dir), dry_run=True, db=str(db_path)
    )

    assert result["plan"] is None
    assert result["dry_run"] is True
    assert list(plan_dir.iterdir()) == []


def test_propose_rejects_mutually_exclusive_targets(tmp_path):
    root, db_path = _corpus(tmp_path)
    plan_dir = _plans(tmp_path)

    with pytest.raises(mcp_server.ToolError):
        mcp_server.propose(
            root=str(root),
            plan_dir=str(plan_dir),
            out=str(plan_dir / "p.json"),
            db=str(db_path),
        )


def test_propose_then_apply_then_undo_round_trip(tmp_path):
    root, db_path = _corpus(tmp_path)
    plan_dir = _plans(tmp_path)
    mcp_server.ingest(root=str(root), db=str(db_path))

    # An agent verdict is what makes an entry movable; `classify` is a CLI verb by design
    # (not one of this issue's seven tools), so the test uses the CLI for it.
    assert cli.main([
        "--db", str(db_path), "--json", "classify", "--document", "1",
        "--party", "Northwind", "--doc-type", "invoice", "--doc-date", "2026-02-03",
        "--folder", "Suppliers/Northwind",
    ]) == 0

    planned = mcp_server.propose(root=str(root), plan_dir=str(plan_dir), db=str(db_path))
    assert planned["move"] == 1
    plan_path = planned["plan"]
    assert plan_path is not None

    applied = mcp_server.apply(plan_path, root=str(root), db=str(db_path))
    assert applied["moved"] == 1
    assert not (root / "scan-001.pdf").exists()

    undone = mcp_server.undo(planned["plan_id"], root=str(root), db=str(db_path))
    assert undone["reversed"] == 1
    assert (root / "scan-001.pdf").exists()


def test_apply_requires_a_plan(tmp_path):
    """No plan, no move - the section 6 invariant, enforced at the wrapper's own signature."""
    root, db_path = _corpus(tmp_path)
    mcp_server.ingest(root=str(root), db=str(db_path))
    before = sorted(p.name for p in root.iterdir())

    with pytest.raises(TypeError):
        mcp_server.apply(root=str(root), db=str(db_path))  # `plan` has no default

    with pytest.raises(mcp_server.ToolError):
        mcp_server.apply(str(tmp_path / "absent.json"), root=str(root), db=str(db_path))

    assert sorted(p.name for p in root.iterdir()) == before


def test_apply_rejects_a_plan_built_for_another_root(tmp_path):
    root, db_path = _corpus(tmp_path)
    plan_dir = _plans(tmp_path)
    mcp_server.ingest(root=str(root), db=str(db_path))
    planned = mcp_server.propose(root=str(root), plan_dir=str(plan_dir), db=str(db_path))

    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    with pytest.raises(mcp_server.ToolError) as excinfo:
        mcp_server.apply(planned["plan"], root=str(other_root), db=str(db_path))
    assert "retarget" in str(excinfo.value)


def test_dupes_report_on_empty_index(tmp_path):
    root, db_path = _corpus(tmp_path)

    report = mcp_server.dupes(root=str(root), db=str(db_path))

    assert report["exact_groups"] == 0
    assert report["near"] == 0
    assert report["subset"] == 0


def test_module_imports_without_mcp_sdk():
    """The optional-dependency AC: importing the wrapper must not pull in the `mcp` package."""
    code = (
        "import sys; import filingcabinet.mcp_server as m; "
        "assert not [n for n in sys.modules if n == 'mcp' or n.startswith('mcp.')], "
        "'mcp imported at module scope'; "
        "assert m.TOOL_NAMES"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_flags_helper_shapes():
    assert mcp_server._flags(root=None) == []
    assert mcp_server._flags(dry_run=True) == ["--dry-run"]
    assert mcp_server._flags(dry_run=False) == []
    assert mcp_server._flags(max_distance=3) == ["--max-distance", "3"]

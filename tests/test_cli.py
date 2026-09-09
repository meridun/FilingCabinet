import argparse
import json
from pathlib import Path

import pytest

from filingcabinet import cli, db, dedup, ocr, organize


def test_migrate_refuses_to_create_without_flag(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "migrate"])


def test_migrate_create_then_status_json(tmp_path, capsys):
    dbp = str(tmp_path / "fc.db")
    assert cli.main(["--db", dbp, "--json", "migrate", "--create"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created"] is True and "001_init.sql" in out["applied"]
    assert cli.main(["--db", dbp, "--json", "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["migrated"] is True and st["pending"] == [] and st["documents"] == 0


def test_db_path_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FC_DB", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[paths]\ndata_dir = '{tmp_path.as_posix()}'\n")
    assert cli.main(["--config", str(cfg), "migrate", "--create"]) == 0
    assert (tmp_path / cli.DB_FILENAME).exists()


def _make_root(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"alpha")
    return root


def test_ingest_json_summary(tmp_path, capsys):
    dbp = str(tmp_path / "fc.db")
    root = _make_root(tmp_path)
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "ingest", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["new"] == 1 and out["changed"] == 0 and out["missing"] == 0
    assert all(isinstance(out[k], int) for k in ("new", "changed", "missing"))


def test_ingest_requires_migrated_db(tmp_path):
    root = _make_root(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "ingest", "--root", str(root)])


def test_ingest_root_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FC_ROOT", raising=False)
    monkeypatch.delenv("FC_DB", raising=False)
    root = _make_root(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[paths]\ndata_dir = '{tmp_path.as_posix()}'\nroot = '{root.as_posix()}'\n")
    assert cli.main(["--config", str(cfg), "migrate", "--create"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(cfg), "--json", "ingest"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["root"] == str(root) and out["new"] == 1


def test_ingest_without_root_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("FC_ROOT", raising=False)
    dbp = str(tmp_path / "fc.db")
    cfg = tmp_path / "config.toml"
    cfg.write_text("[paths]\n")
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["--db", dbp, "--config", str(cfg), "ingest"])


def test_ingest_extensions_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FC_ROOT", raising=False)
    root = _make_root(tmp_path)
    (root / "scan.jp2").write_bytes(b"jp2")
    dbp = str(tmp_path / "fc.db")
    cfg = tmp_path / "config.toml"
    cfg.write_text("[ingest]\nextensions = ['.jp2']\n")
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    capsys.readouterr()
    assert cli.main(
        ["--db", dbp, "--config", str(cfg), "--json", "ingest", "--root", str(root)]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scanned"] == 1 and out["new"] == 1


def test_snapshot_verb_json(tmp_path, capsys):
    dbp = str(tmp_path / "fc.db")
    snaps = tmp_path / "snaps"
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--snapshot-dir", str(snaps), "--json", "snapshot"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["snapshot"]).is_file() and out["rotated"] == []
    assert out["snapshot_dir"] == str(snaps)


def test_snapshot_requires_migrated_db(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "--snapshot-dir", str(tmp_path), "snapshot"])


def test_snapshot_dir_from_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FC_SNAPSHOT_DIR", raising=False)
    monkeypatch.delenv("FC_DB", raising=False)
    snaps = tmp_path / "snaps"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"[paths]\ndata_dir = '{tmp_path.as_posix()}'\nsnapshot_dir = '{snaps.as_posix()}'\n"
    )
    assert cli.main(["--config", str(cfg), "migrate", "--create"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(cfg), "--json", "snapshot"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snapshot_dir"] == str(snaps) and Path(out["snapshot"]).parent == snaps


def test_snapshot_without_snapshot_dir_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("FC_SNAPSHOT_DIR", raising=False)
    dbp = str(tmp_path / "fc.db")
    cfg = tmp_path / "config.toml"
    cfg.write_text("[paths]\n")
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["--db", dbp, "--config", str(cfg), "snapshot"])


def test_restore_latest_json(tmp_path, capsys):
    dbp = tmp_path / "fc.db"
    snaps = tmp_path / "snaps"
    root = _make_root(tmp_path)
    assert cli.main(["--db", str(dbp), "migrate", "--create"]) == 0
    assert cli.main(["--db", str(dbp), "ingest", "--root", str(root)]) == 0
    assert cli.main(["--db", str(dbp), "--snapshot-dir", str(snaps), "snapshot"]) == 0
    # mutate the live index after the snapshot: the restore must roll it back
    (root / "b.pdf").write_bytes(b"beta")
    assert cli.main(["--db", str(dbp), "ingest", "--root", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(
        ["--db", str(dbp), "--snapshot-dir", str(snaps), "--json", "restore", "latest"]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["row_counts"]["document"] == 1
    assert Path(out["rescue_copy"]).is_file()
    assert out["restored_from"].startswith(str(snaps))


def test_restore_dry_run_changes_nothing(tmp_path, capsys):
    dbp = tmp_path / "fc.db"
    snaps = tmp_path / "snaps"
    assert cli.main(["--db", str(dbp), "migrate", "--create"]) == 0
    assert cli.main(["--db", str(dbp), "--snapshot-dir", str(snaps), "snapshot"]) == 0
    before = dbp.read_bytes()
    capsys.readouterr()
    assert cli.main(
        ["--db", str(dbp), "--snapshot-dir", str(snaps), "--json", "restore", "latest",
         "--dry-run"]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["integrity"] == "ok"
    assert dbp.read_bytes() == before
    assert not (tmp_path / "rescue").exists()


def test_restore_without_snapshots_errors(tmp_path):
    dbp = str(tmp_path / "fc.db")
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    assert cli.main([ "--db", dbp, "migrate", "--create"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["--db", dbp, "--snapshot-dir", str(snaps), "restore", "latest"])


def test_restore_invalid_snapshot_exits_two(tmp_path, capsys):
    dbp = tmp_path / "fc.db"
    bad = tmp_path / "filingcabinet-20260101T000000Z.db"
    bad.write_bytes(b"definitely not sqlite" * 20)
    assert cli.main(["--db", str(dbp), "migrate", "--create"]) == 0
    before = dbp.read_bytes()
    capsys.readouterr()
    assert cli.main(["--db", str(dbp), "restore", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
    assert dbp.read_bytes() == before


def test_restore_with_the_index_held_open_exits_two(tmp_path, capsys):
    """A held index is a routine Windows state; it must report, not raise a traceback."""
    snaps = tmp_path / "snaps"
    dbp = tmp_path / "fc.db"
    assert cli.main(["--db", str(dbp), "migrate", "--create"]) == 0
    assert cli.main(["--db", str(dbp), "--snapshot-dir", str(snaps), "snapshot"]) == 0

    reader = db.connect(dbp)  # stands in for a second `fc` run or a DB browser
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM document").fetchone()  # pins an older snapshot
    conn = db.connect(dbp)
    with conn:
        conn.execute(
            "INSERT INTO document (sha256, size_bytes, first_seen_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("c" * 64, 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    conn.close()
    capsys.readouterr()
    try:
        argv = ["--db", str(dbp), "--snapshot-dir", str(snaps), "restore", "latest"]
        assert cli.main(argv) == 2
    finally:
        reader.close()

    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "rescue").exists()


def test_restore_from_the_live_db_path_exits_two_and_keeps_the_index(tmp_path, capsys):
    dbp = tmp_path / "fc.db"
    assert cli.main(["--db", str(dbp), "migrate", "--create"]) == 0
    before = dbp.read_bytes()
    capsys.readouterr()

    assert cli.main(["--db", str(dbp), "restore", str(dbp)]) == 2

    err = capsys.readouterr().err
    assert "error:" in err and "itself" in err
    assert dbp.read_bytes() == before
    assert not (tmp_path / "rescue").exists()


def _migrated_db(tmp_path, capsys):
    dbp = str(tmp_path / "fc.db")
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    capsys.readouterr()
    return dbp


def test_dupes_report_json_on_empty_index(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    assert cli.main(["--db", dbp, "--json", "dupes", "report", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    for key in ("exact_groups", "exact_documents", "near", "subset", "queued"):
        assert out[key] == 0
    assert out["max_distance"] == dedup.DEFAULT_PHASH_MAX_DISTANCE
    assert out["root"] == str(root) and isinstance(out["phash_available"], bool)


def test_dupes_report_reports_exact_duplicates(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    root = tmp_path / "docs"
    (root / "copy").mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"alpha")
    (root / "copy" / "a.pdf").write_bytes(b"alpha")
    assert cli.main(["--db", dbp, "ingest", "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "dupes", "report", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exact_groups"] == 1 and out["exact_documents"] == 2


def test_dupes_report_requires_migrated_db(tmp_path):
    root = _make_root(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "dupes", "report", "--root", str(root)])


def test_dupes_report_threshold_precedence(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[dedup]\nphash_max_distance = 3\n")
    empty_cfg = tmp_path / "empty.toml"
    empty_cfg.write_text("[paths]\n")

    def report(*extra):
        argv = ["--db", dbp, "--json", *extra, "dupes", "report", "--root", str(root)]
        assert cli.main(argv) == 0
        return json.loads(capsys.readouterr().out)["max_distance"]

    assert report("--config", str(empty_cfg)) == dedup.DEFAULT_PHASH_MAX_DISTANCE
    assert report("--config", str(cfg)) == 3
    assert cli.main(
        ["--db", dbp, "--json", "--config", str(cfg), "dupes", "report", "--root", str(root),
         "--max-distance", "1"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["max_distance"] == 1


def test_dupes_report_rejects_a_non_numeric_threshold(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[dedup]\nphash_max_distance = "six"\n')
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--db", dbp, "--config", str(cfg), "dupes", "report", "--root", str(root)])
    assert "phash_max_distance" in str(excinfo.value)


def _two_documents(dbp):
    conn = db.connect(dbp)
    try:
        with conn:
            for sha in ("sha-a", "sha-b"):
                conn.execute(
                    "INSERT INTO document (sha256, size_bytes, page_count, first_seen_at, "
                    "updated_at) VALUES (?, 1, 1, '2026-01-01', '2026-01-01')",
                    (sha,),
                )
        return [r["document_id"] for r in conn.execute("SELECT document_id FROM document")]
    finally:
        conn.close()


def test_dupes_label_list_pair_export_round_trip(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    a, b = _two_documents(dbp)

    assert cli.main(["--db", dbp, "--json", "dupes", "label", "--list"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == []

    assert cli.main(
        ["--db", dbp, "--json", "dupes", "label", "--pair", str(a), str(b),
         "--kind", "near", "--verdict", "not-dup"]
    ) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["verdict"] == "not_dup" and recorded["document_a"] == a

    assert cli.main(["--db", dbp, "--json", "dupes", "label", "--export"]) == 0
    labels = json.loads(capsys.readouterr().out)["labels"]
    assert len(labels) == 1 and labels[0]["kind"] == "near"


def test_dupes_label_pair_needs_kind_and_verdict(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    a, b = _two_documents(dbp)
    with pytest.raises(SystemExit):
        cli.main(["--db", dbp, "dupes", "label", "--pair", str(a), str(b)])


@pytest.mark.skipif(ocr.pymupdf_version() is None, reason="optional `ocr` extra (PyMuPDF)")
def test_ocr_run_json_summary(tmp_path, capsys):
    """`a.pdf` is not really a PDF, so this also covers the unopenable-document path.

    Without PyMuPDF the run degrades to `skipped` before any document is opened, so the
    error count is only meaningful with the `ocr` extra installed (CI installs the bare package).
    """
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    assert cli.main(["--db", dbp, "--json", "ingest", "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "ocr", "run", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ladder"] == ["local", "vision"] and out["min_confidence"] == 0.75
    assert out["errors"] == 1 and out["documents"] == 0
    assert all(
        isinstance(out[key], int)
        for key in ("documents", "pages", "ok", "degraded", "errors")
    )


def test_ocr_ladder_from_config(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[ocr]\nladder = ["local"]\nmin_confidence = 0.4\n')
    assert cli.main(
        ["--db", dbp, "--config", str(cfg), "--json", "ocr", "run", "--root", str(root)]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ladder"] == ["local"] and out["min_confidence"] == 0.4


def test_ocr_requires_migrated_db(tmp_path):
    root = _make_root(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "ocr", "run", "--root", str(root)])


def test_ocr_submit_commits_agent_text(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    document_id = _two_documents(dbp)[0]
    text_file = tmp_path / "page.txt"
    text_file.write_text("handwritten meter reading 41215", encoding="utf-8")
    assert cli.main(
        ["--db", dbp, "--json", "ocr", "submit", "--document", str(document_id),
         "--page", "1", "--text-file", str(text_file)]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ocr_source"] == "vision" and out["page"] == 1

    assert cli.main(["--db", dbp, "--json", "find", "meter reading"]) == 0
    hits = json.loads(capsys.readouterr().out)["hits"]
    assert [hit["document_id"] for hit in hits] == [document_id]


def test_ocr_submit_rejects_bad_input(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    document_id = _two_documents(dbp)[0]
    text_file = tmp_path / "page.txt"
    text_file.write_text("   ", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(
            ["--db", dbp, "ocr", "submit", "--document", str(document_id),
             "--page", "1", "--text-file", str(text_file)]
        )
    with pytest.raises(SystemExit):
        cli.main(
            ["--db", dbp, "ocr", "submit", "--document", str(document_id),
             "--page", "1", "--text-file", str(tmp_path / "missing.txt")]
        )


def test_find_json_shape(tmp_path, capsys):
    dbp = _migrated_db(tmp_path, capsys)
    document_id = _two_documents(dbp)[0]
    conn = db.connect(dbp)
    try:
        with conn:
            conn.execute(
                "UPDATE document SET ocr_text = ? WHERE document_id = ?",
                ("Northwind invoice for office chairs", document_id),
            )
    finally:
        conn.close()

    assert cli.main(["--db", dbp, "--json", "find", "northwind", "--limit", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == "northwind" and out["count"] == 1
    hit = out["hits"][0]
    assert set(hit) == {
        "document_id",
        "sha256",
        "rel_path",
        "mime",
        "page_count",
        "snippet",
        "rank",
    }
    assert hit["document_id"] == document_id and "[Northwind]" in hit["snippet"]

    assert cli.main(["--db", dbp, "--json", "find", "nothing-matches-this"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "query": "nothing-matches-this",
        "count": 0,
        "hits": [],
    }


def test_find_requires_migrated_db(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "find", "anything"])


def test_doctor_json_reports_tesseract(tmp_path, capsys, monkeypatch):
    dbp = _migrated_db(tmp_path, capsys)
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    assert cli.main(["--db", dbp, "--json", "doctor"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"tesseract", "pymupdf", "ladder", "min_confidence", "db", "migrated"}
    assert out["tesseract"] == {"present": True, "version": "tesseract v5.4.0"}
    assert out["migrated"] is True

    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    assert cli.main(["--db", dbp, "--json", "doctor"]) == 0  # absence is a report, not a failure
    out = json.loads(capsys.readouterr().out)
    assert out["tesseract"] == {"present": False, "version": None}


def test_doctor_without_a_database_still_reports(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FC_DB", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[paths]\n")
    assert cli.main(["--config", str(cfg), "--json", "doctor"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "db" not in out and out["pymupdf"]["present"] in (True, False)


def test_ingest_unaffected_by_fts_triggers(tmp_path, capsys):
    """The FTS update trigger is guarded, so a re-scan neither rewrites nor loses the index."""
    dbp = _migrated_db(tmp_path, capsys)
    root = _make_root(tmp_path)
    assert cli.main(["--db", dbp, "--json", "ingest", "--root", str(root)]) == 0
    capsys.readouterr()
    conn = db.connect(dbp)
    try:
        with conn:
            conn.execute("UPDATE document SET ocr_text = 'northwind invoice'")
    finally:
        conn.close()

    assert cli.main(["--db", dbp, "--json", "ingest", "--root", str(root)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["unchanged"] == 1 and second["changed"] == 0

    assert cli.main(["--db", dbp, "--json", "find", "northwind"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


# --- propose / classify (phase 5) --------------------------------------------------------

TAXONOMY_TOML = """
version = 1
doc_types = ["invoice"]

[parties.northwind]
display = "Northwind"
aliases = ["northwind ltd"]

[[rules]]
id = "northwind-invoice"
party = "northwind"
doc_type = "invoice"
any = ["invoice"]
folder = "Suppliers/Northwind"
priority = 100
"""


def _propose_fixture(tmp_path, monkeypatch, text=b"Northwind invoice no 7"):
    """A migrated index over a one-document root, plus a taxonomy file. Returns (db, root, tax)."""
    for name in ("FC_ROOT", "FC_DB", "FC_TAXONOMY", "FC_PLAN_DIR", "FC_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    dbp = str(tmp_path / "fc.db")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "scan.pdf").write_bytes(text)
    tax = tmp_path / "taxonomy.toml"
    tax.write_text(TAXONOMY_TOML, encoding="utf-8")
    assert cli.main(["--db", dbp, "migrate", "--create"]) == 0
    assert cli.main(["--db", dbp, "ingest", "--root", str(root)]) == 0
    # ocr_text is phase 4's job; set it directly so the phase-5 tests do not need PyMuPDF.
    conn = db.connect(dbp)
    conn.execute("UPDATE document SET ocr_text = ?", ("Northwind invoice no 7 dated 2026-02-03",))
    conn.commit()
    conn.close()
    return dbp, root, tax


def test_propose_json_shape_and_plan_file(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    plan_dir = tmp_path / "plans"
    capsys.readouterr()
    assert cli.main([
        "--db", dbp, "--json", "propose", "--root", str(root),
        "--taxonomy", str(tax), "--plan-dir", str(plan_dir),
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {"db", "root", "taxonomy", "taxonomy_exists", "taxonomy_rules", "template",
            "plan_id", "plan", "dry_run", "documents",
            "move", "noop", "unclassified", "collision", "errors", "rule_matched",
            "agent_matched", "entries"} <= set(out)
    entry = out["entries"][0]
    assert entry["provenance"] == "rule" and entry["rule_id"] == "northwind-invoice"
    assert entry["target_path"] == "Suppliers/Northwind/2026-02-03_Northwind_invoice.pdf"
    assert out["move"] == 1 and out["rule_matched"] == 1
    written = json.loads(Path(out["plan"]).read_text(encoding="utf-8"))
    assert written["plan_version"] == organize.PLAN_VERSION
    assert written["plan_id"] == out["plan_id"]
    assert {"current_mtime", "current_size"} <= set(written["entries"][0])


def test_propose_exits_zero_on_an_all_unclassified_corpus(tmp_path, monkeypatch, capsys):
    dbp, root, _ = _propose_fixture(tmp_path, monkeypatch)
    conn = db.connect(dbp)
    conn.execute("UPDATE document SET ocr_text = 'nothing recognisable'")
    conn.commit()
    conn.close()
    capsys.readouterr()
    assert cli.main([
        "--db", dbp, "--json", "propose", "--root", str(root),
        "--taxonomy", str(tmp_path / "absent.toml"), "--plan-dir", str(tmp_path / "plans"),
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unclassified"] == 1 and out["move"] == 0


def test_propose_dry_run_writes_no_file(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    plan_dir = tmp_path / "plans"
    capsys.readouterr()
    assert cli.main([
        "--db", dbp, "--json", "propose", "--root", str(root), "--taxonomy", str(tax),
        "--plan-dir", str(plan_dir), "--dry-run",
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["plan"] is None
    assert not plan_dir.exists()


def test_propose_never_touches_the_document_tree(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    before = sorted(p.name for p in root.rglob("*"))
    capsys.readouterr()
    assert cli.main([
        "--db", dbp, "--json", "propose", "--root", str(root), "--taxonomy", str(tax),
        "--plan-dir", str(tmp_path / "plans"),
    ]) == 0
    assert sorted(p.name for p in root.rglob("*")) == before


def test_propose_refuses_a_plan_dir_inside_the_document_root(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--db", dbp, "propose", "--root", str(root), "--taxonomy", str(tax),
            "--plan-dir", str(root / "plans"),
        ])
    assert "inside the document root" in str(exc.value)


def test_propose_refuses_an_out_path_inside_the_document_root(tmp_path, monkeypatch):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--db", dbp, "propose", "--root", str(root), "--taxonomy", str(tax),
            "--out", str(root / "plan.json"),
        ])
    assert "inside the document root" in str(exc.value)


def test_propose_requires_a_migrated_db(tmp_path, monkeypatch):
    monkeypatch.delenv("FC_DB", raising=False)
    root = tmp_path / "docs"
    root.mkdir()
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "fc.db"), "propose", "--root", str(root)])


def test_taxonomy_path_precedence(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    env_tax = tmp_path / "env.toml"
    env_tax.write_text(TAXONOMY_TOML, encoding="utf-8")
    cfg_tax = tmp_path / "cfg.toml"
    cfg_tax.write_text(TAXONOMY_TOML, encoding="utf-8")
    plan_dir = (tmp_path / "p").as_posix()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"[paths]\ntaxonomy = '{cfg_tax.as_posix()}'\nplan_dir = '{plan_dir}'\n",
        encoding="utf-8",
    )

    def taxonomy_used(*extra):
        capsys.readouterr()
        assert cli.main(["--db", dbp, "--config", str(cfg), "--json", "propose",
                         "--root", str(root), "--dry-run", *extra]) == 0
        return json.loads(capsys.readouterr().out)["taxonomy"]

    assert taxonomy_used("--taxonomy", str(tax)) == str(tax)  # flag wins
    monkeypatch.setenv("FC_TAXONOMY", str(env_tax))
    assert taxonomy_used("--taxonomy", str(tax)) == str(tax)  # flag still wins
    assert taxonomy_used() == str(env_tax)  # env beats config
    monkeypatch.delenv("FC_TAXONOMY")
    assert taxonomy_used() == str(cfg_tax)  # config last


def test_config_relative_paths_resolve_beside_the_config_file(tmp_path, monkeypatch):
    """Relative [paths] values mean "beside config.toml", not "beside the shell's cwd" (#18)."""
    for name in ("FC_ROOT", "FC_DB", "FC_TAXONOMY", "FC_PLAN_DIR", "FC_CONFIG",
                 "FC_SNAPSHOT_DIR"):
        monkeypatch.delenv(name, raising=False)
    instance = tmp_path / "instance"
    (instance / "docs").mkdir(parents=True)
    (instance / "data").mkdir()
    (instance / "taxonomy.toml").write_text(TAXONOMY_TOML, encoding="utf-8")
    cfg = instance / "config.toml"
    cfg.write_text(
        "[paths]\nroot = 'docs'\ndata_dir = 'data'\nsnapshot_dir = 'snapshots'\n"
        "taxonomy = 'taxonomy.toml'\nplan_dir = 'plans'\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the failure this covers only shows up away from the config
    args = argparse.Namespace(config=str(cfg), db=None, root=None, snapshot_dir=None,
                              taxonomy=None, plan_dir=None, out=None)

    assert cli.resolve_taxonomy_path(args) == (instance / "taxonomy.toml").resolve()
    assert cli.resolve_db_path(args) == (instance / "data").resolve() / cli.DB_FILENAME
    assert cli.resolve_root(args) == (instance / "docs").resolve()
    assert cli.resolve_snapshot_dir(args) == (instance / "snapshots").resolve()
    assert cli.resolve_plan_dir(args, instance / "docs") == (instance / "plans").resolve()

    # An absent [paths].taxonomy still defaults to taxonomy.toml beside the config file.
    cfg.write_text("[paths]\nroot = 'docs'\n", encoding="utf-8")
    assert cli.resolve_taxonomy_path(args) == (instance / "taxonomy.toml").resolve()

    # A flag is a shell input: it stays relative to the working directory, made absolute.
    (elsewhere / "local.toml").write_text(TAXONOMY_TOML, encoding="utf-8")
    args.taxonomy = "local.toml"
    assert cli.resolve_taxonomy_path(args) == (elsewhere / "local.toml").resolve()


def test_propose_from_another_cwd_loads_the_config_relative_taxonomy(
    tmp_path, monkeypatch, capsys
):
    """The scaffolded `taxonomy = 'taxonomy.toml'` must classify from any cwd (#18)."""
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"[paths]\nroot = '{root.name}'\ntaxonomy = 'taxonomy.toml'\nplan_dir = 'plans'\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--config", str(cfg), "--json", "propose", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["taxonomy"] == str(tax.resolve())
    assert out["taxonomy_exists"] is True and out["taxonomy_rules"] == 1
    assert out["rule_matched"] == 1 and out["unclassified"] == 0
    assert not (elsewhere / "taxonomy.toml").exists()  # nothing was created in the cwd


def test_propose_reports_the_taxonomy_it_loaded(tmp_path, monkeypatch, capsys):
    """An all-unclassified run caused by a missing rules file must be explainable (#18)."""
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    absent = tmp_path / "absent.toml"
    capsys.readouterr()
    assert cli.main(["--db", dbp, "propose", "--root", str(root), "--taxonomy", str(absent),
                     "--plan-dir", str(tmp_path / "plans")]) == 0
    assert f"taxonomy: {absent.resolve()} (missing, 0 rules)" in capsys.readouterr().out

    assert cli.main(["--db", dbp, "propose", "--root", str(root), "--taxonomy", str(tax),
                     "--plan-dir", str(tmp_path / "plans")]) == 0
    assert f"taxonomy: {tax.resolve()} (1 rule(s))" in capsys.readouterr().out

    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "propose", "--root", str(root), "--taxonomy",
                     str(absent), "--dry-run", "--plan-dir", str(tmp_path / "plans")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["taxonomy"] == str(absent.resolve())
    assert out["taxonomy_exists"] is False and out["taxonomy_rules"] == 0
    assert out["unclassified"] == 1


def test_malformed_taxonomy_exits_two_without_a_traceback(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    tax.write_text('version = 1\n\n[[rules]]\nid = "r"\nregex = "(["\n', encoding="utf-8")
    assert cli.main(["--db", dbp, "propose", "--root", str(root), "--taxonomy", str(tax),
                     "--plan-dir", str(tmp_path / "plans")]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ") and "Traceback" not in captured.err


def test_naming_template_from_config_and_a_bad_value(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[naming]\ntemplate = "{party}-{doc_type}"\n', encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--config", str(cfg), "--json", "propose", "--root", str(root),
                     "--taxonomy", str(tax), "--dry-run", "--plan-dir", str(tmp_path / "p")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["template"] == "{party}-{doc_type}"
    assert out["entries"][0]["target_name"] == "Northwind-invoice.pdf"

    cfg.write_text("[naming]\ntemplate = 5\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", dbp, "--config", str(cfg), "propose", "--root", str(root),
                  "--taxonomy", str(tax), "--dry-run"])
    assert "[naming].template" in str(exc.value)


def test_classify_records_a_verdict_and_prints_a_rule_stanza(tmp_path, monkeypatch, capsys):
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    conn = db.connect(dbp)
    document_id = conn.execute("SELECT document_id FROM document").fetchone()["document_id"]
    conn.close()
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "classify", "--document", str(document_id),
                     "--party", "Acme Mutual", "--doc-type", "policy", "--tag", "insurance",
                     "--note", "letterhead"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provenance"] == "agent" and out["tags"] == ["insurance"]
    assert out["suggested_rule"].startswith("[[rules]]")

    # The verdict now outranks the rule that would otherwise have matched.
    assert cli.main(["--db", dbp, "--json", "propose", "--root", str(root), "--taxonomy",
                     str(tax), "--dry-run", "--plan-dir", str(tmp_path / "p")]) == 0
    entry = json.loads(capsys.readouterr().out)["entries"][0]
    assert entry["provenance"] == "agent" and "Acme_Mutual" in entry["target_name"]


def test_classify_rejects_an_unknown_document(tmp_path, monkeypatch):
    dbp, _, _ = _propose_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", dbp, "classify", "--document", "9999", "--party", "Acme"])
    assert "no document" in str(exc.value)


# --- apply / undo (phase 6) ---------------------------------------------------------------


def _planned(tmp_path, monkeypatch, capsys):
    """Propose over the phase-5 fixture and return (db, root, plan path, plan_id)."""
    dbp, root, tax = _propose_fixture(tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "propose", "--root", str(root),
                     "--taxonomy", str(tax), "--plan-dir", str(tmp_path / "plans")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["move"] == 1
    return dbp, root, Path(out["plan"]), out["plan_id"]


def test_apply_json_shape_and_exit_zero(tmp_path, monkeypatch, capsys):
    dbp, root, plan, plan_id = _planned(tmp_path, monkeypatch, capsys)
    assert cli.main(["--db", dbp, "--json", "apply", str(plan), "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {"db", "root", "plan_id", "entries", "moved", "reversed", "skipped", "ignored",
            "errors", "dry_run"} <= set(out)
    assert out["plan_id"] == plan_id and out["moved"] == 1 and out["errors"] == 0
    moved = out["entries"][0]
    assert moved["status"] == "moved" and moved["to_path"].startswith("Suppliers/Northwind/")
    assert (root / moved["to_path"]).is_file() and not (root / "scan.pdf").exists()


def test_apply_dry_run_moves_nothing(tmp_path, monkeypatch, capsys):
    dbp, root, plan, _ = _planned(tmp_path, monkeypatch, capsys)
    assert cli.main(["--db", dbp, "--json", "apply", str(plan), "--root", str(root),
                     "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["moved"] == 1
    assert (root / "scan.pdf").is_file() and not (root / "Suppliers").exists()


def test_undo_json_shape_and_second_undo_is_a_noop(tmp_path, monkeypatch, capsys):
    dbp, root, plan, plan_id = _planned(tmp_path, monkeypatch, capsys)
    assert cli.main(["--db", dbp, "apply", str(plan), "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "--json", "undo", plan_id, "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reversed"] == 1 and out["plan_id"] == plan_id
    assert (root / "scan.pdf").is_file()
    assert cli.main(["--db", dbp, "--json", "undo", plan_id, "--root", str(root)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["reversed"] == 0 and again["entries"] == []


def test_apply_refuses_a_plan_built_for_another_root(tmp_path, monkeypatch, capsys):
    dbp, root, plan, _ = _planned(tmp_path, monkeypatch, capsys)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert cli.main(["--db", dbp, "apply", str(plan), "--root", str(elsewhere)]) == 2
    assert "may not retarget" in capsys.readouterr().err
    assert (root / "scan.pdf").is_file()


def test_apply_of_an_unusable_plan_exits_two_without_a_traceback(tmp_path, monkeypatch, capsys):
    dbp, root, plan, _ = _planned(tmp_path, monkeypatch, capsys)
    plan.write_text("{ not json", encoding="utf-8")
    assert cli.main(["--db", dbp, "apply", str(plan), "--root", str(root)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_apply_requires_a_migrated_db(tmp_path, monkeypatch, capsys):
    _, root, plan, _ = _planned(tmp_path, monkeypatch, capsys)
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "absent.db"), "apply", str(plan), "--root", str(root)])


@pytest.mark.parametrize("argv", [["apply"], ["undo"]])
def test_apply_and_undo_refuse_to_run_without_their_argument(tmp_path, argv):
    """No implicit 'apply everything pending' - the plan/plan_id positional is required."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "fc.db"), *argv])
    assert exc.value.code == 2


def test_undo_text_report_says_nothing_left_to_reverse(tmp_path, monkeypatch, capsys):
    dbp, root, plan, plan_id = _planned(tmp_path, monkeypatch, capsys)
    assert cli.main(["--db", dbp, "apply", str(plan), "--root", str(root)]) == 0
    assert cli.main(["--db", dbp, "undo", plan_id, "--root", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", dbp, "undo", plan_id, "--root", str(root)]) == 0
    assert "nothing left to reverse" in capsys.readouterr().out

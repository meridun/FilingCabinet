import json
from pathlib import Path

import pytest

from filingcabinet import cli, db, dedup, ocr


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

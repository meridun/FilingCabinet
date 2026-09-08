import json

import pytest

from filingcabinet import cli, db, dedup


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

import json
from pathlib import Path

import pytest

from filingcabinet import cli


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

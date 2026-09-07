import json

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

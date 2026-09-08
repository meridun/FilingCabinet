"""Instance scaffold (`filingcabinet instance init`) - issue #10."""

import fnmatch
import json
from pathlib import Path

import pytest

from filingcabinet import __version__, cli, instance

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD = {"README.md", ".gitignore", "CLAUDE.md", "config.toml", "taxonomy.toml"}


def _init(tmp_path, capsys, *extra):
    assert cli.main(["--json", "instance", "init", str(tmp_path), *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_init_creates_the_whole_scaffold(tmp_path, capsys):
    target = tmp_path / "fc-data"
    out = _init(target, capsys)
    assert set(out["created"]) == SCAFFOLD
    assert out["skipped"] == []
    for name in SCAFFOLD:
        assert (target / name).is_file()


def test_readme_carries_access_warning(tmp_path):
    instance.init_instance(tmp_path)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Do not grant broad Drive or app access" in readme
    assert "ingest" in readme


def test_claude_md_pins_framework_version(tmp_path):
    instance.init_instance(tmp_path)
    assert __version__ in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_config_seeded_from_example(tmp_path):
    instance.init_instance(tmp_path)
    scaffolded = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert scaffolded == (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name", ["scan.pdf", "a.jpg", "x.heic", "filingcabinet.db", "index.sqlite3", "index.db-wal"]
)
def test_gitignore_blocks_documents_and_databases(tmp_path, name):
    instance.init_instance(tmp_path)
    patterns = [
        line.strip()
        for line in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert any(fnmatch.fnmatch(name, pat) for pat in patterns), name


@pytest.mark.parametrize("name", ["README.md", "CLAUDE.md", "config.toml"])
def test_gitignore_keeps_instance_files(tmp_path, name):
    instance.init_instance(tmp_path)
    patterns = [
        line.strip()
        for line in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert not any(fnmatch.fnmatch(name, pat) for pat in patterns), name


def test_init_is_idempotent_and_never_overwrites(tmp_path, capsys):
    _init(tmp_path, capsys)
    (tmp_path / "config.toml").write_text("# edited by the user\n", encoding="utf-8")
    out = _init(tmp_path, capsys)
    assert out["created"] == []
    assert set(out["skipped"]) == SCAFFOLD
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "# edited by the user\n"

    out = _init(tmp_path, capsys, "--force")
    assert set(out["created"]) == SCAFFOLD
    assert "[paths]" in (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_init_rejects_a_non_directory_target(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["instance", "init", str(target)])


def test_docs_instance_page_exists_and_mentions_scheduling():
    page = (REPO_ROOT / "docs" / "Development_Instance.md").read_text(encoding="utf-8")
    assert "ingest" in page
    assert "Task Scheduler" in page or "cron" in page
    assert "instance init" in page


def test_scaffolded_taxonomy_is_verbatim_and_loadable(tmp_path):
    """The example carries regex escapes and must not be str.format-ed on the way out."""
    from filingcabinet import taxonomy as taxonomy_mod

    instance.init_instance(tmp_path)
    scaffolded = tmp_path / "taxonomy.toml"
    assert scaffolded.read_text(encoding="utf-8") == instance.read_template(
        "taxonomy.example.toml"
    )
    tax = taxonomy_mod.load_taxonomy(scaffolded)
    assert {r.rule_id for r in tax.rules} >= {"northwind-invoice"}

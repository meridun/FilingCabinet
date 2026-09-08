"""Scaffold a private FilingCabinet *instance* directory (docs/Architecture.md §8).

The instance holds config, taxonomy/rules, index snapshots, and the move-log export. Documents
themselves stay in the Drive folder and are never copied here. This module only *creates* files
inside the caller-named target directory: it never moves, renames, or deletes anything, and it
never overwrites an existing file unless the caller passes ``force``.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from . import __version__

# Output filename -> template resource name in filingcabinet/templates/.
TEMPLATES: dict[str, str] = {
    "README.md": "README.md.tmpl",
    ".gitignore": "gitignore.tmpl",
    "CLAUDE.md": "CLAUDE.md.tmpl",
    "config.toml": "config.example.toml",
    "taxonomy.toml": "taxonomy.example.toml",
}

# These are copied verbatim: their bodies contain {doc_date}-style braces and regex escapes
# that are not placeholders and must survive untouched.
VERBATIM = frozenset({"config.example.toml", "taxonomy.example.toml"})


def read_template(name: str) -> str:
    return (resources.files("filingcabinet") / "templates" / name).read_text(encoding="utf-8")


def render(name: str, **ctx: str) -> str:
    """Load template ``name`` and substitute ``ctx``; verbatim templates are returned as-is."""
    text = read_template(name)
    if name in VERBATIM:
        return text
    return text.format(**ctx)


def init_instance(target: Path, *, force: bool = False) -> dict:
    """Scaffold an instance directory at ``target``. Idempotent; never overwrites without force."""
    target = Path(target)
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"{target} exists and is not a directory")
    target.mkdir(parents=True, exist_ok=True)

    ctx = {
        "instance_name": target.resolve().name,
        "framework_version": __version__,
        "config_path": (target / "config.toml").as_posix(),
    }

    created: list[str] = []
    skipped: list[str] = []
    for filename, template in TEMPLATES.items():
        dest = target / filename
        if dest.exists() and not force:
            skipped.append(filename)
            continue
        dest.write_text(render(template, **ctx), encoding="utf-8")
        created.append(filename)

    return {"path": str(target), "created": created, "skipped": skipped}

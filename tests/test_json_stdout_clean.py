"""Convention: with ``--json``, the CLI writes nothing to stdout except the JSON payload.

Every ``--json`` consumer (the smoke tests, the MCP wrapper's stdio stream, a scheduled run's log
parser) does ``json.loads(proc.stdout)``. A dependency that prints on import breaks all of them at
once: PyMuPDF 1.28 printed its ``fitz`` deprecation warning to stdout (#29). This test runs the real
CLI in a subprocess, the same way ``tests/test_smoke_*`` do, so an import-time print fails here with
a readable message instead of as sixteen ``JSONDecodeError`` smoke failures.
"""

import json
import subprocess
import sys


def _stdout(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_json_stdout_is_exactly_one_json_document(tmp_path):
    db = tmp_path / "fc.db"
    out = _stdout("--db", str(db), "--json", "migrate", "--create")
    assert out.lstrip().startswith("{"), f"stdout has text before the JSON payload:\n{out!r}"
    payload = json.loads(out)  # raises if anything trails the payload
    assert payload["created"] is True

    out = _stdout("--db", str(db), "--json", "status")
    assert out.lstrip().startswith("{"), f"stdout has text before the JSON payload:\n{out!r}"
    assert json.loads(out)["migrated"] is True


def test_importing_the_package_prints_nothing():
    # The MCP server imports the whole CLI before it starts speaking JSON-RPC on stdout, so a
    # module-level print anywhere in the package corrupts the handshake.
    proc = subprocess.run(
        [sys.executable, "-c", "import filingcabinet.cli, filingcabinet.mcp_server"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"import wrote to stdout:\n{proc.stdout!r}"

"""The Drive API seam behind the `drive` OCR rung: identity, temp copies, notes, secrets.

Every test runs on `FakeDriveClient` (tests/conftest.py) or on monkeypatched library calls -
no network, no real credential, and every "secret" is a synthetic marker string built here.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from filingcabinet import drive

SECRET_MARK = "planted-secret-zz9-plural-z-alpha"


def _credentials(data_dir: Path, *, scopes=drive.SCOPES) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / drive.CLIENT_SECRET_FILENAME).write_text(
        json.dumps({"installed": {"client_secret": SECRET_MARK}}), encoding="utf-8"
    )
    (data_dir / drive.TOKEN_FILENAME).write_text(
        json.dumps({"refresh_token": SECRET_MARK, "token": SECRET_MARK, "scopes": list(scopes)}),
        encoding="utf-8",
    )


# --- config ---------------------------------------------------------------------------------


def test_config_from_mapping_tolerates_bad_input():
    assert drive.DriveConfig.from_mapping(None) == drive.DriveConfig()
    assert drive.DriveConfig.from_mapping("nope") == drive.DriveConfig()
    config = drive.DriveConfig.from_mapping({"root_folder": "/Docs//Scans/", "temp_folder": 7})
    assert config.root_folder == "Docs/Scans" and config.root_parts == ("Docs", "Scans")
    assert config.temp_folder == drive.DEFAULT_TEMP_FOLDER
    assert drive.DriveConfig().root_parts == ()


@pytest.mark.parametrize(
    "name, valid",
    [
        ("filingcabinet-ocr-tmp", True),
        ("", False),
        (".", False),
        ("..", False),
        ("a/b", False),
        ("a\\b", False),
        ("tab\there", False),
        ("x" * 256, False),
    ],
)
def test_temp_folder_names_are_validated(name, valid):
    assert drive.DriveConfig(temp_folder=name).temp_folder_valid() is valid


def test_q_literal_escapes_backslash_then_quote():
    assert drive._q_literal("O'Brien") == "O\\'Brien"
    assert drive._q_literal("a\\b") == "a\\\\b"
    assert drive._q_literal("\\'") == "\\\\\\'"


# --- file identity --------------------------------------------------------------------------


def test_path_walk_resolves_through_root_folder_and_nested_path(fake_drive, drive_session):
    target = fake_drive.add_path("Docs/2026/scans/invoice.pdf", size=42)
    session = drive_session(drive.DriveConfig(root_folder="Docs"))
    assert session._resolve("2026/scans/invoice.pdf", 42) == target
    # Folder ids are memoised for the run: a second lookup walks only the last step.
    before = len(fake_drive.called("children"))
    assert session._resolve("2026/scans/invoice.pdf", 42) == target
    assert len(fake_drive.called("children")) == before + 1


def test_same_title_in_two_folders_resolves_by_folder_and_size(fake_drive, drive_session):
    scans = fake_drive.add_path("scans/invoice.pdf", size=10)
    fake_drive.add_path("archive/invoice.pdf", size=20)
    session = drive_session()
    assert session._resolve("scans/invoice.pdf", 10) == scans
    assert session._resolve("scans/invoice.pdf", 20) is None  # size mismatch is no match


def test_ambiguous_matches_are_refused(fake_drive, drive_session):
    folder = fake_drive.add_folder("root", "scans")
    fake_drive.add_file(folder, "invoice.pdf", size=10)
    fake_drive.add_file(folder, "invoice.pdf", size=10)
    session = drive_session()
    assert session._resolve("scans/invoice.pdf", 10) is None  # two size matches
    fake_drive.add_folder("root", "twins")
    fake_drive.add_folder("root", "twins")
    assert session._resolve("twins/a.pdf", 1) is None  # an ambiguous folder step
    assert session._resolve("../escape.pdf", 1) is None
    assert session._resolve("scans/invoice.pdf", None) is None


def test_fetch_tries_occurrences_in_order_then_not_found(fake_drive, drive_session):
    fake_drive.add_path("second/a.pdf", size=5, text="found it")
    session = drive_session()
    fetched = session.fetch([("first/a.pdf", 5), ("second/a.pdf", 5)])
    assert (fetched.text, fetched.note) == ("found it", None)
    missing = session.fetch([("nowhere/a.pdf", 5)])
    assert (missing.text, missing.note) == (None, drive.NOTE_NOT_FOUND)


def test_fetch_caps_the_occurrences_tried(fake_drive, drive_session):
    session = drive_session()
    many = [(f"d{n}/a.pdf", 1) for n in range(drive.MAX_OCCURRENCES + 3)]
    fake_drive.add_path(f"d{drive.MAX_OCCURRENCES + 1}/a.pdf", size=1, text="late")
    assert session.fetch(many).note == drive.NOTE_NOT_FOUND


# --- temporary copies -----------------------------------------------------------------------


def test_copy_lands_only_in_the_marked_folder_and_is_deleted(fake_drive, drive_session):
    source = fake_drive.add_path("scans/a.pdf", size=3, text="text layer")
    session = drive_session()
    assert session.fetch([("scans/a.pdf", 3)]).text == "text layer"
    assert session.fetch([("scans/a.pdf", 3)]).text == "text layer"

    folders = fake_drive.called("create_marked_folder")
    assert len(folders) == 1  # created once, then reused
    temp_ids = {
        node_id
        for node_id, node in fake_drive.nodes.items()
        if node["mark"] == drive.TEMP_FOLDER_MARK
    }
    assert len(temp_ids) == 1
    for call in fake_drive.called("copy_as_doc"):
        assert call[1] == source and call[2] in temp_ids
    # Every write is the folder, a copy into it, or the permanent delete of that copy.
    copies = [call for call in fake_drive.called("copy_as_doc")]
    deleted = {call[1] for call in fake_drive.called("delete")}
    assert len(deleted) == len(copies) == 2
    assert all(node["mark"] != drive.COPY_MARK for node in fake_drive.nodes.values())
    assert {call[0] for call in fake_drive.writes()} == set(
        ("create_marked_folder", "copy_as_doc", "delete")
    )
    assert source in fake_drive.nodes  # the document itself is never written


def test_existing_marked_folder_is_reused_not_recreated(fake_drive, drive_session):
    existing = fake_drive.add_folder(
        "root", drive.DEFAULT_TEMP_FOLDER, mark=drive.TEMP_FOLDER_MARK
    )
    fake_drive.add_folder("root", drive.DEFAULT_TEMP_FOLDER)  # same name, not ours
    fake_drive.add_path("a.pdf", size=1, text="x")
    session = drive_session()
    session.fetch([("a.pdf", 1)])
    assert fake_drive.called("create_marked_folder") == []
    assert fake_drive.called("copy_as_doc")[0][2] == existing


def test_failed_export_still_deletes_the_copy(fake_drive, drive_session):
    fake_drive.add_path("a.pdf", size=1, text="x")
    fake_drive.errors["export_text"] = drive.NOTE_CONVERT_FAILED
    session = drive_session()
    fetched = session.fetch([("a.pdf", 1)])
    assert fetched.note == drive.NOTE_CONVERT_FAILED
    exported = fake_drive.called("export_text")[0][1]
    # The finally ran: the copy that failed to export got the permanent delete, and is gone.
    assert fake_drive.calls[-1] == ("delete", exported)
    assert exported not in fake_drive.nodes


def test_failed_delete_is_counted_then_swept_next_run(fake_drive, drive_session):
    fake_drive.add_path("a.pdf", size=1, text="x")
    fake_drive.errors["delete"] = drive.NOTE_CONVERT_FAILED
    first = drive_session()
    assert first.fetch([("a.pdf", 1)]).text == "x"  # a stuck copy never fails the fetch
    assert first.delete_failures == 1
    assert sum(node["mark"] == drive.COPY_MARK for node in fake_drive.nodes.values()) == 1

    del fake_drive.errors["delete"]
    second = drive_session()
    assert second.sweep() == 1
    assert second.swept == 1
    assert all(node["mark"] != drive.COPY_MARK for node in fake_drive.nodes.values())


def test_sweep_deletes_only_marked_copies(fake_drive, drive_session):
    folder = fake_drive.add_folder("root", drive.DEFAULT_TEMP_FOLDER, mark=drive.TEMP_FOLDER_MARK)
    stray = fake_drive.add_file(folder, "fc-ocr-old", mark=drive.COPY_MARK)
    human = fake_drive.add_file(folder, "dropped-here-by-a-human.pdf")
    elsewhere = fake_drive.add_file("root", "marked-but-elsewhere", mark=drive.COPY_MARK)
    session = drive_session()
    assert session.sweep() == 1
    assert stray not in fake_drive.nodes
    assert human in fake_drive.nodes and elsewhere in fake_drive.nodes
    assert fake_drive.called("create_marked_folder") == []  # a sweep never creates a folder


def test_sweep_swallows_errors(fake_drive, drive_session):
    fake_drive.errors["marked_folders"] = drive.NOTE_OFFLINE
    session = drive_session()
    assert session.sweep() == 0
    assert session.tripped == drive.NOTE_OFFLINE


# --- notes and the breaker ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, note",
    [
        ("children", drive.NOTE_OFFLINE),
        ("children", drive.NOTE_UNAUTHENTICATED),
        ("copy_as_doc", drive.NOTE_QUOTA),
        ("copy_as_doc", drive.NOTE_CONVERT_FAILED),
        ("export_text", drive.NOTE_NOT_FOUND),
    ],
)
def test_client_errors_surface_as_their_note(fake_drive, drive_session, method, note):
    fake_drive.add_path("a.pdf", size=1, text="x")
    fake_drive.errors[method] = note
    fetched = drive_session().fetch([("a.pdf", 1)])
    assert (fetched.text, fetched.note) == (None, note)


def test_breaker_stops_calls_after_an_environment_failure(fake_drive, drive_session):
    fake_drive.add_path("a.pdf", size=1, text="x")
    fake_drive.errors["children"] = drive.NOTE_OFFLINE
    session = drive_session()
    assert session.fetch([("a.pdf", 1)]).note == drive.NOTE_OFFLINE
    calls = len(fake_drive.calls)
    del fake_drive.errors["children"]  # even once the network is back, this run stays off
    assert session.fetch([("a.pdf", 1)]).note == drive.NOTE_OFFLINE
    assert session.sweep() == 0
    assert len(fake_drive.calls) == calls
    assert session.status == drive.NOTE_OFFLINE


def test_quality_failures_do_not_trip_the_breaker(fake_drive, drive_session):
    fake_drive.add_path("b.pdf", size=1, text="y")
    session = drive_session()
    assert session.fetch([("a.pdf", 1)]).note == drive.NOTE_NOT_FOUND
    assert session.fetch([("b.pdf", 1)]).text == "y"
    assert session.tripped is None


class _Resp:
    def __init__(self, status):
        self.status = status


class _FakeHttpError(Exception):
    """The shape of googleapiclient's HttpError that `_map_error` reads: resp.status, content."""

    def __init__(self, status, content):
        super().__init__(f"https://example.invalid/{SECRET_MARK}")
        self.resp = _Resp(status)
        self.content = content


class _FakeRefreshError(Exception):
    pass


class _FakeTransportError(Exception):
    pass


@pytest.fixture()
def google_errors(monkeypatch):
    """Stand-in Google exception modules, so the mapping is tested with the extra absent."""
    import types

    modules = {
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.errors": types.ModuleType("googleapiclient.errors"),
        "google": types.ModuleType("google"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.exceptions": types.ModuleType("google.auth.exceptions"),
    }
    modules["googleapiclient.errors"].HttpError = _FakeHttpError
    modules["google.auth.exceptions"].RefreshError = _FakeRefreshError
    modules["google.auth.exceptions"].TransportError = _FakeTransportError
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _http_error(status, reason=None):
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}] if reason else [], "message": SECRET_MARK}}
    ).encode()
    return _FakeHttpError(status, content)


@pytest.mark.parametrize(
    "status, reason, note",
    [
        (401, None, drive.NOTE_UNAUTHENTICATED),
        (403, "insufficientPermissions", drive.NOTE_UNAUTHENTICATED),
        (403, "userRateLimitExceeded", drive.NOTE_QUOTA),
        (403, "storageQuotaExceeded", drive.NOTE_QUOTA),
        (429, None, drive.NOTE_QUOTA),
        (404, None, drive.NOTE_NOT_FOUND),
        (400, None, drive.NOTE_CONVERT_FAILED),
        (403, "cannotExportFile", drive.NOTE_CONVERT_FAILED),
        (500, None, drive.NOTE_OFFLINE),
        (503, None, drive.NOTE_OFFLINE),
    ],
)
def test_http_errors_map_to_notes(google_errors, status, reason, note):
    assert drive._map_error(_http_error(status, reason)) == note


def test_transport_and_refresh_errors_map_to_notes(google_errors):
    assert drive._map_error(TimeoutError()) == drive.NOTE_OFFLINE
    assert drive._map_error(ConnectionResetError()) == drive.NOTE_OFFLINE
    assert drive._map_error(RuntimeError(SECRET_MARK)) == drive.NOTE_CONVERT_FAILED
    assert drive._map_error(_FakeRefreshError("invalid_grant")) == drive.NOTE_UNAUTHENTICATED
    assert drive._map_error(_FakeTransportError("down")) == drive.NOTE_OFFLINE


def test_google_client_reduces_library_errors_to_a_bare_note(tmp_path):
    client = drive.GoogleDriveClient(tmp_path / "missing-token.json")

    def boom():
        raise RuntimeError(f"https://www.googleapis.com/drive/v3/files?key={SECRET_MARK}")

    with pytest.raises(drive.DriveError) as caught:
        client._call(boom)
    assert caught.value.note == drive.NOTE_CONVERT_FAILED
    assert SECRET_MARK not in str(caught.value) and caught.value.__cause__ is None


# --- transient-error retry ------------------------------------------------------------------


@pytest.fixture()
def recorded_sleep(monkeypatch):
    """Backoff is recorded, never waited."""
    delays = []
    monkeypatch.setattr(drive, "_sleep", delays.append)
    return delays


class _Flaky:
    """A library call that raises the planted exceptions in order, then returns ``value``."""

    def __init__(self, *exceptions, value="ok"):
        self.pending = list(exceptions)
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.pending:
            raise self.pending.pop(0)
        return self.value


def test_one_transient_error_is_retried_then_succeeds(google_errors, recorded_sleep, tmp_path):
    client = drive.GoogleDriveClient(tmp_path / "token.json")
    flaky = _Flaky(_http_error(500, "internalError"))
    assert client._call(flaky) == "ok"
    assert flaky.calls == 2
    assert len(recorded_sleep) == 1
    assert drive.RETRY_BASE_S <= recorded_sleep[0] <= 2 * drive.RETRY_BASE_S


@pytest.mark.parametrize(
    "exc, note",
    [
        (_http_error(500, "internalError"), drive.NOTE_OFFLINE),
        (_http_error(502), drive.NOTE_OFFLINE),
        (_http_error(503, "backendError"), drive.NOTE_OFFLINE),
        (_http_error(504), drive.NOTE_OFFLINE),
        (_http_error(429, "rateLimitExceeded"), drive.NOTE_QUOTA),
        (_FakeTransportError(SECRET_MARK), drive.NOTE_OFFLINE),
        (TimeoutError(), drive.NOTE_OFFLINE),
        (ConnectionResetError(), drive.NOTE_OFFLINE),
    ],
)
def test_sustained_transient_errors_exhaust_the_attempts_then_map(
    google_errors, recorded_sleep, tmp_path, exc, note
):
    client = drive.GoogleDriveClient(tmp_path / "token.json")
    flaky = _Flaky(*[exc] * (drive.RETRY_ATTEMPTS + 2))
    with pytest.raises(drive.DriveError) as caught:
        client._call(flaky)
    assert caught.value.note == note
    assert flaky.calls == drive.RETRY_ATTEMPTS  # bounded: never one more
    assert len(recorded_sleep) == drive.RETRY_ATTEMPTS - 1
    assert recorded_sleep == sorted(recorded_sleep)  # exponential: each wait at least the last
    for attempt, delay in enumerate(recorded_sleep):
        base = drive.RETRY_BASE_S * 2**attempt
        assert base <= delay <= base + drive.RETRY_BASE_S  # jitter stays within one base step
    assert SECRET_MARK not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize(
    "exc, note",
    [
        (_http_error(401), drive.NOTE_UNAUTHENTICATED),
        (_http_error(403, "userRateLimitExceeded"), drive.NOTE_QUOTA),
        (_http_error(404), drive.NOTE_NOT_FOUND),
        (_http_error(400), drive.NOTE_CONVERT_FAILED),
        (_FakeRefreshError("invalid_grant"), drive.NOTE_UNAUTHENTICATED),
        (RuntimeError(SECRET_MARK), drive.NOTE_CONVERT_FAILED),
    ],
)
def test_non_transient_errors_are_not_retried(google_errors, recorded_sleep, tmp_path, exc, note):
    client = drive.GoogleDriveClient(tmp_path / "token.json")
    flaky = _Flaky(exc)
    with pytest.raises(drive.DriveError) as caught:
        client._call(flaky)
    assert caught.value.note == note
    assert flaky.calls == 1 and recorded_sleep == []


def test_a_drive_error_raised_inside_the_call_is_never_retried(recorded_sleep, tmp_path):
    client = drive.GoogleDriveClient(tmp_path / "token.json")
    flaky = _Flaky(drive.DriveError(drive.NOTE_UNAUTHENTICATED))  # what `_drive()` raises
    with pytest.raises(drive.DriveError) as caught:
        client._call(flaky)
    assert caught.value.note == drive.NOTE_UNAUTHENTICATED
    assert flaky.calls == 1 and recorded_sleep == []


class _FlakyCopyClient:
    """The fake Drive, with `copy_as_doc` going through the real client's retrying `_call`.

    Each planted library exception is raised by one `files.copy` attempt, in order; once the
    list is empty the fake's copy succeeds. Everything else is the plain fake.
    """

    def __init__(self, fake, *exceptions):
        self._fake = fake
        self._google = drive.GoogleDriveClient(Path("unused-token.json"))
        self.pending = list(exceptions)

    def __getattr__(self, name):
        return getattr(self._fake, name)

    def copy_as_doc(self, file_id, folder_id):
        def attempt():
            if self.pending:
                raise self.pending.pop(0)
            return self._fake.copy_as_doc(file_id, folder_id)

        return self._google._call(attempt)


def test_session_survives_one_transient_copy_failure_without_tripping(
    google_errors, recorded_sleep, fake_drive, drive_session
):
    fake_drive.add_path("scans/a.pdf", size=3, text="text layer")
    client = _FlakyCopyClient(fake_drive, _http_error(500, "internalError"))
    session = drive_session(client=client)
    fetched = session.fetch([("scans/a.pdf", 3)])
    assert (fetched.text, fetched.note) == ("text layer", None)
    assert session.tripped is None and session.status == "ok"
    assert len(recorded_sleep) == 1
    # The one copy that succeeded was exported and permanently deleted; nothing is left over.
    assert len(fake_drive.called("copy_as_doc")) == 1
    assert all(node["mark"] != drive.COPY_MARK for node in fake_drive.nodes.values())
    # The rung keeps working for the rest of the run.
    fake_drive.add_path("scans/b.pdf", size=4, text="more")
    assert session.fetch([("scans/b.pdf", 4)]).text == "more"


def test_session_trips_once_transient_copy_failures_exhaust_the_retries(
    google_errors, recorded_sleep, fake_drive, drive_session
):
    fake_drive.add_path("scans/a.pdf", size=3, text="text layer")
    client = _FlakyCopyClient(fake_drive, *[_http_error(500, "internalError")] * 10)
    session = drive_session(client=client)
    fetched = session.fetch([("scans/a.pdf", 3)])
    assert (fetched.text, fetched.note) == (None, drive.NOTE_OFFLINE)
    assert session.tripped == drive.NOTE_OFFLINE and session.status == drive.NOTE_OFFLINE
    assert len(client.pending) == 10 - drive.RETRY_ATTEMPTS  # exactly the bounded attempts
    assert len(recorded_sleep) == drive.RETRY_ATTEMPTS - 1
    assert fake_drive.called("copy_as_doc") == []  # the fake copy was never reached
    calls = len(fake_drive.calls)
    client.pending.clear()  # even once Drive recovers, this run stays off
    assert session.fetch([("scans/a.pdf", 3)]).note == drive.NOTE_OFFLINE
    assert len(fake_drive.calls) == calls
    assert SECRET_MARK not in json.dumps(session.as_dict())


# --- sessions -------------------------------------------------------------------------------


def test_open_session_reports_each_missing_piece(tmp_path, fake_drive, monkeypatch):
    factory = lambda token_path: fake_drive  # noqa: E731
    config = drive.DriveConfig()

    monkeypatch.setattr(drive, "library_available", lambda: False)
    assert drive.open_session(config, tmp_path).reason == drive.REASON_LIBRARY_MISSING
    assert drive.open_session(config, None, client_factory=factory).reason == (
        drive.REASON_DATA_DIR_UNSET
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert drive.open_session(config, data_dir, client_factory=factory).reason == (
        drive.REASON_CLIENT_SECRET_MISSING
    )
    (data_dir / drive.CLIENT_SECRET_FILENAME).write_text("{}", encoding="utf-8")
    missing = drive.open_session(config, data_dir, client_factory=factory)
    assert missing.reason == drive.REASON_TOKEN_MISSING and missing.needs_auth
    assert missing.as_dict()["hint"] == drive.AUTH_COMMAND
    (data_dir / drive.TOKEN_FILENAME).write_text("{}", encoding="utf-8")
    bad_name = drive.open_session(
        drive.DriveConfig(temp_folder="a/b"), data_dir, client_factory=factory
    )
    assert bad_name.reason == drive.REASON_TEMP_FOLDER_INVALID

    usable = drive.open_session(config, data_dir, client_factory=factory)
    assert usable.available and usable.reason is None and usable.status == "ok"
    assert "hint" not in usable.as_dict()


def test_unavailable_session_never_calls_the_client(fake_drive):
    session = drive.DriveSession(drive.DriveConfig(), reason=drive.REASON_TOKEN_MISSING)
    assert session.fetch([("a.pdf", 1)]).note == "rung_unavailable:drive"
    assert session.sweep() == 0 and fake_drive.calls == []


# --- authorize ------------------------------------------------------------------------------


class _FakeCredentials:
    scopes = list(drive.SCOPES)

    def to_json(self):
        return json.dumps({"refresh_token": SECRET_MARK, "scopes": list(drive.SCOPES)})


@pytest.fixture()
def fake_flow(monkeypatch):
    calls = []

    def flow(secret_path, scopes):
        calls.append((Path(secret_path), tuple(scopes)))
        return _FakeCredentials()

    monkeypatch.setattr(drive, "_run_installed_app_flow", flow)
    return calls


def test_authorize_copies_the_secret_and_writes_the_token(tmp_path, fake_flow):
    supplied = tmp_path / "downloaded-client.json"
    supplied.write_text(json.dumps({"installed": {"client_secret": SECRET_MARK}}))
    data_dir = tmp_path / "data"
    result = drive.authorize(data_dir, client_secret=supplied, forbidden=[tmp_path / "root"])
    assert result == {
        "token_path": str(data_dir / drive.TOKEN_FILENAME),
        "scopes": ["drive.file", "drive.readonly"],
    }
    assert (data_dir / drive.CLIENT_SECRET_FILENAME).read_bytes() == supplied.read_bytes()
    assert json.loads((data_dir / drive.TOKEN_FILENAME).read_text())["refresh_token"] == (
        SECRET_MARK
    )
    assert fake_flow == [(data_dir / drive.CLIENT_SECRET_FILENAME, drive.SCOPES)]
    assert SECRET_MARK not in json.dumps(result)
    assert not [p for p in data_dir.iterdir() if p.name.endswith(".tmp")]  # atomic, no debris


def test_authorize_reuses_a_secret_already_in_data_dir(tmp_path, fake_flow):
    data_dir = tmp_path / "data"
    _credentials(data_dir)
    (data_dir / drive.TOKEN_FILENAME).unlink()
    drive.authorize(data_dir, forbidden=[])
    assert (data_dir / drive.TOKEN_FILENAME).is_file()


def test_authorize_without_a_secret_refuses(tmp_path, fake_flow):
    with pytest.raises(ValueError, match="client secret"):
        drive.authorize(tmp_path / "data", forbidden=[])
    with pytest.raises(ValueError, match="no client secret file"):
        drive.authorize(tmp_path / "data", client_secret=tmp_path / "absent.json", forbidden=[])
    assert fake_flow == []


@pytest.mark.parametrize("under", ["root", "snapshots"])
def test_authorize_refuses_a_data_dir_under_a_synced_dir(tmp_path, fake_flow, under):
    supplied = tmp_path / "client.json"
    supplied.write_text("{}")
    forbidden = [tmp_path / "root", tmp_path / "snapshots"]
    for data_dir in (tmp_path / under / "data", tmp_path / under.upper() / "data"):
        with pytest.raises(ValueError, match="Drive syncs"):
            drive.authorize(data_dir, client_secret=supplied, forbidden=forbidden)
        assert not data_dir.exists()  # refused before anything was written
    assert fake_flow == []


def test_authorize_flow_failure_hides_library_text(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _credentials(data_dir)

    def flow(secret_path, scopes):
        raise RuntimeError(f"https://accounts.example.invalid/?code={SECRET_MARK}")

    monkeypatch.setattr(drive, "_run_installed_app_flow", flow)
    with pytest.raises(ValueError) as caught:
        drive.authorize(data_dir, forbidden=[])
    assert SECRET_MARK not in str(caught.value)


# --- doctor ---------------------------------------------------------------------------------


def test_doctor_report_carries_presence_and_scope_names_only(tmp_path):
    data_dir = tmp_path / "data"
    _credentials(data_dir)
    report = drive.doctor_report(
        drive.DriveConfig(),
        data_dir,
        excludes=(".*", "FilingCabinet-OCR-TMP"),
        forbidden=[tmp_path / "root"],
    )
    assert report["client_secret"] is True and report["token"] is True
    assert report["scopes"] == ["drive.file", "drive.readonly"]
    assert report["temp_folder_excluded"] is True  # matched case-insensitively
    assert report["data_dir_synced"] is False
    assert SECRET_MARK not in json.dumps(report)


def test_doctor_report_without_data_dir_or_exclusion(tmp_path):
    report = drive.doctor_report(drive.DriveConfig(), None, excludes=(".*",))
    assert (report["client_secret"], report["token"], report["scopes"]) == (False, False, [])
    assert report["temp_folder_excluded"] is False and report["data_dir"] is None
    synced = drive.doctor_report(
        drive.DriveConfig(), tmp_path / "root" / "data", excludes=(), forbidden=[tmp_path / "root"]
    )
    assert synced["data_dir_synced"] is True


def test_module_imports_no_google_library_at_import_time():
    import subprocess

    probe = (
        "import sys, filingcabinet.drive, filingcabinet.ocr, filingcabinet.cli; "
        "print(sorted(m for m in sys.modules if m.startswith(('google', 'googleapiclient'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    ).stdout.strip()
    assert out == "[]"

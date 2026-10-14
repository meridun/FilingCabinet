"""Google Drive API access for the unattended `drive` OCR rung (docs/Architecture.md §5, §9).

Everything Google lives here: the `[ocr.drive]` config, the OAuth desktop credentials, the
client seam tests fake, file identity by path walk, and the temporary-copy lifecycle Drive needs
to hand back its OCR text layer. `ocr.py` keeps the ladder semantics and only calls
:meth:`DriveSession.fetch`.

Posture (each a reviewed decision, see the module's tests):

* **Opt-in and dependency-free by default.** The Google client libraries are the optional
  `drive` extra and are imported lazily, inside the functions that need them; nothing at module
  level imports them, so the engine, the CLI and their tests run with the extra absent.
* **Secrets only in `[paths].data_dir`.** The client secret and the token are read and written
  there and nowhere else; :func:`authorize` refuses a `data_dir` under `[paths].root` or
  `[paths].snapshot_dir` (Drive-synced). Reports carry presence and scope names, never values.
* **Scopes `drive.readonly` + `drive.file`, never wider.** Reads are read-only; the only writes
  are app-created objects that carry an `appProperties` mark - one temp folder and the converted
  copies inside it - and only marked objects are ever deleted or swept. Deletes are permanent
  (`files.delete`), never trash.
* **Library exception text never reaches output.** Every failure is mapped to a machine-readable
  note token; :class:`DriveError` carries the token only.
* **Document content never reaches a shell, and Drive names never become local paths.** Queries
  are built through :func:`_q_literal`; ids come only from API responses.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import random
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol

SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)
CLIENT_SECRET_FILENAME = "drive_client_secret.json"
TOKEN_FILENAME = "drive_token.json"
DEFAULT_TEMP_FOLDER = "filingcabinet-ocr-tmp"
APP_PROPERTY_KEY = "filingcabinet"
TEMP_FOLDER_MARK = "ocr-tmp-folder"
COPY_MARK = "ocr-copy"
MAX_OCCURRENCES = 5
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
HTTP_TIMEOUT_S = 60
# Transient failures (a 5xx, a 429, a dropped connection) are retried a bounded number of times
# before they become a note and trip the breaker: a sustained outage still costs one failing
# call per run, a single flaky `files.copy` no longer degrades the rest of the run.
RETRY_ATTEMPTS = 3
RETRY_BASE_S = 1.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
AUTH_COMMAND = "filingcabinet ocr auth"
_MAX_LIST_PAGES = 10  # a name lookup returns a handful of entries; never page forever

# Notes: why the rung produced nothing. Environment class (counted in `degraded`, retried on the
# next run, and tripping the per-run breaker) versus quality class (not retried).
NOTE_OFFLINE = "drive_offline"
NOTE_UNAUTHENTICATED = "drive_unauthenticated"
NOTE_QUOTA = "drive_quota"
NOTE_NOT_FOUND = "drive_not_found"
NOTE_CONVERT_FAILED = "drive_convert_failed"
NOTE_TOO_LONG = "drive_too_long"
ENVIRONMENT_NOTES = frozenset({NOTE_OFFLINE, NOTE_UNAUTHENTICATED, NOTE_QUOTA})
_UNAVAILABLE_NOTE = "rung_unavailable:drive"

# Why a session is not usable. Any of these makes the rung `rung_unavailable:drive`.
REASON_LIBRARY_MISSING = "library_missing"
REASON_DATA_DIR_UNSET = "data_dir_unset"
REASON_CLIENT_SECRET_MISSING = "client_secret_missing"
REASON_TOKEN_MISSING = "token_missing"
REASON_TEMP_FOLDER_INVALID = "temp_folder_invalid"
# Reasons the one-command re-auth fixes.
AUTH_REASONS = frozenset({REASON_CLIENT_SECRET_MISSING, REASON_TOKEN_MISSING})

_QUOTA_REASONS = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "dailylimitexceeded",
        "quotaexceeded",
        "storagequotaexceeded",
        "sharingratelimitexceeded",
        "rate_limit_exceeded",
        "resource_exhausted",
    }
)
_SCOPE_REASONS = frozenset(
    {
        "insufficientpermissions",
        "insufficientscopes",
        "access_token_scope_insufficient",
        "autherror",
    }
)


@dataclass(frozen=True)
class DriveConfig:
    """`[ocr.drive]`: where `[paths].root` sits in My Drive, and the temp folder's name."""

    root_folder: str = ""
    temp_folder: str = DEFAULT_TEMP_FOLDER

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "DriveConfig":
        """Read `[ocr.drive]`, tolerating a missing or malformed table (config is user input)."""
        if not isinstance(mapping, dict):
            return cls()
        raw_root = mapping.get("root_folder", "")
        root_folder = "/".join(_split_path(raw_root)) if isinstance(raw_root, str) else ""
        raw_temp = mapping.get("temp_folder", DEFAULT_TEMP_FOLDER)
        temp_folder = raw_temp.strip() if isinstance(raw_temp, str) else DEFAULT_TEMP_FOLDER
        return cls(root_folder=root_folder, temp_folder=temp_folder)

    @property
    def root_parts(self) -> tuple[str, ...]:
        """The root folder's path components below My Drive; empty means My Drive itself."""
        return tuple(_split_path(self.root_folder))

    def temp_folder_valid(self) -> bool:
        """A single plain folder name: never a path, never `.`/`..`, no control characters."""
        name = self.temp_folder
        if not name or len(name) > 255 or name in (".", ".."):
            return False
        return not any(ch in "/\\" or ord(ch) < 32 or ord(ch) == 127 for ch in name)


def _split_path(value: str) -> list[str]:
    return [part for part in str(value).replace("\\", "/").split("/") if part.strip()]


class DriveError(Exception):
    """A Drive failure reduced to its note token. Never carries library exception text."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


@dataclass(frozen=True)
class DriveEntry:
    id: str
    name: str
    mime_type: str
    size: int | None


class DriveClient(Protocol):
    """The seam tests fake. Every method raises :class:`DriveError` and nothing else."""

    def children(self, parent_id: str, name: str) -> list[DriveEntry]: ...

    def marked_folders(self, name: str) -> list[DriveEntry]: ...

    def create_marked_folder(self, name: str) -> str: ...

    def marked_copies(self, folder_id: str) -> list[str]: ...

    def copy_as_doc(self, file_id: str, folder_id: str) -> str: ...

    def export_text(self, doc_id: str) -> bytes: ...

    def delete(self, file_id: str) -> None: ...


def _q_literal(value: str) -> str:
    """Escape a value for a single-quoted Drive query literal: backslashes first, then quotes."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _http_reasons(exc) -> set[str]:
    """The machine `reason` codes on a googleapiclient HttpError, lower-cased."""
    reasons: set[str] = set()
    details = getattr(exc, "error_details", None)
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"].lower())
    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, bytearray)):
        try:
            body = json.loads(content.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            for item in error.get("errors") or []:
                if isinstance(item, dict) and isinstance(item.get("reason"), str):
                    reasons.add(item["reason"].lower())
            if isinstance(error.get("status"), str):
                reasons.add(error["status"].lower())
    return reasons


def _map_error(exc: BaseException) -> str:
    """One mapping from a library exception to a note token. Never inspects message text."""
    try:
        from googleapiclient.errors import HttpError
    except Exception:  # pragma: no cover - depends on the optional extra
        HttpError = None  # noqa: N806
    try:
        from google.auth.exceptions import RefreshError, TransportError
    except Exception:  # pragma: no cover - depends on the optional extra
        RefreshError = TransportError = None  # noqa: N806
    try:
        from httplib2 import HttpLib2Error
    except Exception:  # pragma: no cover - depends on the optional extra
        HttpLib2Error = None  # noqa: N806

    if RefreshError is not None and isinstance(exc, RefreshError):
        return NOTE_UNAUTHENTICATED
    if HttpError is not None and isinstance(exc, HttpError):
        try:
            status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        reasons = _http_reasons(exc)
        if status == 401:
            return NOTE_UNAUTHENTICATED
        if status == 429:
            return NOTE_QUOTA
        if status == 403:
            if reasons & _QUOTA_REASONS or any("quota" in reason for reason in reasons):
                return NOTE_QUOTA
            if reasons & _SCOPE_REASONS:
                return NOTE_UNAUTHENTICATED
            return NOTE_CONVERT_FAILED
        if status == 404:
            return NOTE_NOT_FOUND
        if status >= 500 or status == 0:
            return NOTE_OFFLINE
        return NOTE_CONVERT_FAILED
    if TransportError is not None and isinstance(exc, TransportError):
        return NOTE_OFFLINE
    if HttpLib2Error is not None and isinstance(exc, HttpLib2Error):
        return NOTE_OFFLINE
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return NOTE_OFFLINE
    return NOTE_CONVERT_FAILED


def _is_transient(exc: BaseException) -> bool:
    """Whether one more attempt may succeed: a 5xx/429 HttpError or a transport-level failure.

    Reads only the status code and the exception type, never message text.
    """
    try:
        from googleapiclient.errors import HttpError
    except Exception:  # pragma: no cover - depends on the optional extra
        HttpError = None  # noqa: N806
    try:
        from google.auth.exceptions import RefreshError, TransportError
    except Exception:  # pragma: no cover - depends on the optional extra
        RefreshError = TransportError = None  # noqa: N806
    try:
        from httplib2 import HttpLib2Error
    except Exception:  # pragma: no cover - depends on the optional extra
        HttpLib2Error = None  # noqa: N806

    if RefreshError is not None and isinstance(exc, RefreshError):
        return False  # a dead token is fixed by re-auth, never by waiting
    if HttpError is not None and isinstance(exc, HttpError):
        try:
            status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        return status in _RETRY_STATUSES
    if TransportError is not None and isinstance(exc, TransportError):
        return True
    if HttpLib2Error is not None and isinstance(exc, HttpLib2Error):
        return True
    return isinstance(exc, (OSError, TimeoutError, ConnectionError))


def _retry_delay(attempt: int) -> float:
    """Exponential backoff from `RETRY_BASE_S` with jitter: about 1-2 s, then 2-3 s."""
    return RETRY_BASE_S * (2**attempt) + random.uniform(0, RETRY_BASE_S)  # noqa: S311


_sleep = time.sleep  # the seam tests replace so backoff is recorded, not waited


def _execute_with_retry(fn: Callable):
    """Run one library call, retrying a transient failure up to `RETRY_ATTEMPTS` times in all.

    Re-raises the last exception untouched once attempts are exhausted; the caller maps it to
    a note. The exception is never logged or formatted here.
    """
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except DriveError:
            raise
        except Exception as exc:
            if attempt + 1 >= RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            _sleep(_retry_delay(attempt))
    raise AssertionError("unreachable")  # pragma: no cover


class GoogleDriveClient:
    """:class:`DriveClient` over the Drive v3 API. Built lazily; no network until first use.

    Every call goes through :meth:`_call`, which retries transient failures (see
    :func:`_execute_with_retry`) and reduces whatever remains to a :class:`DriveError`. Writes
    are retried too: a `files.copy` or `files.create` whose first attempt landed server-side
    before the 5xx came back leaves one extra *marked* object, which the next run's sweep (or
    the oldest-folder rule) absorbs; a retried `files.delete` of an already-deleted copy
    answers 404 and is counted as a delete failure, nothing more.
    """

    def __init__(self, token_path: Path):
        self._token_path = Path(token_path)
        self._service = None

    def _drive(self):
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build
            import httplib2

            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self._token_path), list(SCOPES)
                )
            except (OSError, ValueError):
                # A missing, malformed or refresh-token-less token: the fix is the re-auth.
                raise DriveError(NOTE_UNAUTHENTICATED) from None
            http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
            self._service = build("drive", "v3", http=http, cache_discovery=False)
        return self._service.files()

    def _call(self, fn: Callable):
        try:
            return _execute_with_retry(fn)
        except DriveError:
            raise
        except Exception as exc:  # library-private types; reduced to a note, text dropped
            raise DriveError(_map_error(exc)) from None

    def _list(self, query: str, *, order_by: str | None = None) -> list[dict]:
        def run():
            files: list[dict] = []
            token = None
            for _ in range(_MAX_LIST_PAGES):
                kwargs = {
                    "q": query,
                    "corpora": "user",
                    "spaces": "drive",
                    "pageSize": 100,
                    "fields": "nextPageToken, files(id,name,mimeType,size)",
                }
                if order_by:
                    kwargs["orderBy"] = order_by
                if token:
                    kwargs["pageToken"] = token
                response = self._drive().list(**kwargs).execute() or {}
                files.extend(
                    item for item in response.get("files") or [] if isinstance(item, dict)
                )
                token = response.get("nextPageToken")
                if not token:
                    break
            return files

        return self._call(run)

    @staticmethod
    def _entry(item: dict) -> DriveEntry | None:
        file_id = item.get("id")
        if not isinstance(file_id, str) or not file_id:
            return None
        try:
            size = int(item["size"]) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        return DriveEntry(
            id=file_id,
            name=str(item.get("name") or ""),
            mime_type=str(item.get("mimeType") or ""),
            size=size,
        )

    def _entries(self, items: list[dict]) -> list[DriveEntry]:
        return [entry for entry in (self._entry(item) for item in items) if entry is not None]

    def children(self, parent_id: str, name: str) -> list[DriveEntry]:
        query = (
            f"'{_q_literal(parent_id)}' in parents and name = '{_q_literal(name)}' "
            "and trashed = false"
        )
        return self._entries(self._list(query))

    def marked_folders(self, name: str) -> list[DriveEntry]:
        query = (
            f"'root' in parents and name = '{_q_literal(name)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false "
            f"and appProperties has {{ key='{APP_PROPERTY_KEY}' and value='{TEMP_FOLDER_MARK}' }}"
        )
        return self._entries(self._list(query, order_by="createdTime"))

    def create_marked_folder(self, name: str) -> str:
        body = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": ["root"],
            "appProperties": {APP_PROPERTY_KEY: TEMP_FOLDER_MARK},
        }
        created = self._call(lambda: self._drive().create(body=body, fields="id").execute())
        return self._required_id(created)

    def marked_copies(self, folder_id: str) -> list[str]:
        # No `trashed = false`: a marked copy someone trashed is still ours to purge.
        query = (
            f"'{_q_literal(folder_id)}' in parents "
            f"and appProperties has {{ key='{APP_PROPERTY_KEY}' and value='{COPY_MARK}' }}"
        )
        return [entry.id for entry in self._entries(self._list(query))]

    def copy_as_doc(self, file_id: str, folder_id: str) -> str:
        body = {
            "name": f"fc-ocr-{secrets.token_hex(8)}",
            "parents": [folder_id],
            "mimeType": GOOGLE_DOC_MIME,
            "appProperties": {APP_PROPERTY_KEY: COPY_MARK},
        }
        copied = self._call(
            lambda: self._drive().copy(fileId=file_id, body=body, fields="id").execute()
        )
        return self._required_id(copied)

    def export_text(self, doc_id: str) -> bytes:
        data = self._call(
            lambda: self._drive().export(fileId=doc_id, mimeType="text/plain").execute()
        )
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data or b"")

    def delete(self, file_id: str) -> None:
        # files.delete is permanent: it skips the trash by construction.
        self._call(lambda: self._drive().delete(fileId=file_id).execute())

    @staticmethod
    def _required_id(response) -> str:
        file_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise DriveError(NOTE_CONVERT_FAILED)
        return file_id


@dataclass
class DriveText:
    text: str | None
    note: str | None


class DriveSession:
    """One `ocr run`'s Drive access: identity, the temp folder, the sweep, and a breaker.

    The breaker trips on the first environment-class failure (offline, unauthenticated, quota)
    and makes every later fetch in the same run answer with that note without an API call, so
    a dead network costs one failing call per run, not one per document.
    """

    def __init__(
        self,
        config: DriveConfig,
        client: DriveClient | None = None,
        *,
        reason: str | None = None,
    ):
        self.config = config
        self._client = client
        self.reason = reason
        self.available = client is not None and reason is None
        self.fetched = 0
        self.empty = 0
        self.failed = 0
        self.swept = 0
        self.delete_failures = 0
        self.tripped: str | None = None
        # Per run, in memory only: never persisted, so no Drive id is cached on `occurrence`.
        self._folder_ids: dict[tuple[str, ...], str] = {}
        self._temp_folder_id: str | None = None

    # --- reporting -------------------------------------------------------------------------

    @property
    def status(self) -> str:
        if not self.available:
            return "unavailable"
        return self.tripped or "ok"

    @property
    def needs_auth(self) -> bool:
        return self.reason in AUTH_REASONS or self.tripped == NOTE_UNAUTHENTICATED

    def record(self, outcome: str) -> None:
        """Count one document's outcome: `fetched`, `empty`, or `failed`."""
        if outcome in ("fetched", "empty", "failed"):
            setattr(self, outcome, getattr(self, outcome) + 1)

    def as_dict(self) -> dict:
        payload = {
            "available": self.available,
            "reason": self.reason,
            "status": self.status,
            "fetched": self.fetched,
            "empty": self.empty,
            "failed": self.failed,
            "swept": self.swept,
            "delete_failures": self.delete_failures,
        }
        if self.needs_auth:
            payload["hint"] = AUTH_COMMAND
        return payload

    # --- API calls -------------------------------------------------------------------------

    def _call(self, fn: Callable, *args):
        if self.tripped:
            raise DriveError(self.tripped)
        try:
            return fn(*args)
        except DriveError as exc:
            if exc.note in ENVIRONMENT_NOTES:
                self.tripped = exc.note
            raise

    def sweep(self) -> int:
        """Permanently delete stray marked copies left by an earlier crash. Never raises.

        Touches only marked folders and only marked copies inside them, and never creates a
        folder - an unmarked file someone dropped into the folder is left alone.
        """
        if not self.available or self.tripped:
            return 0
        swept = 0
        try:
            folders = self._call(self._client.marked_folders, self.config.temp_folder)
            if folders and self._temp_folder_id is None:
                self._temp_folder_id = folders[0].id
            for folder in folders:
                for copy_id in self._call(self._client.marked_copies, folder.id):
                    try:
                        self._call(self._client.delete, copy_id)
                    except DriveError as exc:
                        if exc.note in ENVIRONMENT_NOTES:
                            raise
                        self.delete_failures += 1
                        continue
                    swept += 1
        except DriveError:
            pass  # environment failures tripped the breaker; the run degrades, never fails
        self.swept += swept
        return swept

    def fetch(self, occurrences: Iterable[tuple[str, int | None]]) -> DriveText:
        """The document's Drive text through copy, export, permanent delete. Never raises."""
        if not self.available:
            return DriveText(None, _UNAVAILABLE_NOTE)
        if self.tripped:
            return DriveText(None, self.tripped)
        try:
            file_id = None
            for rel_path, size in list(occurrences)[:MAX_OCCURRENCES]:
                file_id = self._resolve(rel_path, size)
                if file_id:
                    break
            if not file_id:
                return DriveText(None, NOTE_NOT_FOUND)
            folder_id = self._temp_folder()
            copy_id = self._call(self._client.copy_as_doc, file_id, folder_id)
            try:
                raw = self._call(self._client.export_text, copy_id)
            finally:
                try:
                    self._client.delete(copy_id)
                except DriveError:
                    self.delete_failures += 1  # left for the next run's sweep
        except DriveError as exc:
            return DriveText(None, exc.note)
        if isinstance(raw, str):
            return DriveText(raw, None)
        return DriveText(bytes(raw or b"").decode("utf-8", errors="replace"), None)

    def _resolve(self, rel_path: str, size: int | None) -> str | None:
        """Walk My Drive by name to the occurrence; trust a title hit only on a size match.

        Every folder step needs exactly one folder; the last step needs exactly one file of the
        recorded size. Anything else - a miss, an ambiguity, a size mismatch - is no match.
        """
        parts = _split_path(rel_path)
        if not parts or any(part in (".", "..") for part in parts) or size is None:
            return None
        parent = "root"
        prefix: tuple[str, ...] = ()
        for name in (*self.config.root_parts, *parts[:-1]):
            prefix = (*prefix, name)
            cached = self._folder_ids.get(prefix)
            if cached is not None:
                parent = cached
                continue
            folders = [
                entry
                for entry in self._call(self._client.children, parent, name)
                if entry.mime_type == FOLDER_MIME
            ]
            if len(folders) != 1:
                return None
            parent = folders[0].id
            self._folder_ids[prefix] = parent
        hits = [
            entry
            for entry in self._call(self._client.children, parent, parts[-1])
            if entry.mime_type != FOLDER_MIME and entry.size == int(size)
        ]
        return hits[0].id if len(hits) == 1 else None

    def _temp_folder(self) -> str:
        """The oldest marked temp folder at the My Drive root, created once when none exists."""
        if self._temp_folder_id is None:
            folders = self._call(self._client.marked_folders, self.config.temp_folder)
            if folders:
                self._temp_folder_id = folders[0].id
            else:
                self._temp_folder_id = self._call(
                    self._client.create_marked_folder, self.config.temp_folder
                )
        return self._temp_folder_id


def library_available() -> bool:
    """Whether the optional `drive` extra's runtime modules are importable. Imports nothing."""
    for module in ("googleapiclient", "google.oauth2", "google_auth_httplib2", "httplib2"):
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def open_session(
    config: DriveConfig,
    data_dir: Path | None,
    *,
    client_factory: Callable[[Path], DriveClient] | None = None,
) -> DriveSession:
    """A usable session, or an unavailable one carrying the first missing piece as `reason`.

    Checks only that files exist; credential contents are read solely by the Google library.
    """
    if client_factory is None and not library_available():
        return DriveSession(config, reason=REASON_LIBRARY_MISSING)
    if data_dir is None:
        return DriveSession(config, reason=REASON_DATA_DIR_UNSET)
    data_dir = Path(data_dir)
    if not (data_dir / CLIENT_SECRET_FILENAME).is_file():
        return DriveSession(config, reason=REASON_CLIENT_SECRET_MISSING)
    token_path = data_dir / TOKEN_FILENAME
    if not token_path.is_file():
        return DriveSession(config, reason=REASON_TOKEN_MISSING)
    if not config.temp_folder_valid():
        return DriveSession(config, reason=REASON_TEMP_FOLDER_INVALID)
    factory = client_factory or GoogleDriveClient
    return DriveSession(config, factory(token_path))


def _scope_names(scopes) -> list[str]:
    """Short scope names (`drive.readonly`), never anything else from a credential."""
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, (list, tuple, set, frozenset)):
        return []
    return sorted({str(scope).rstrip("/").rsplit("/", 1)[-1] for scope in scopes if scope})


def _is_inside(path: Path, parent: Path) -> bool:
    """`path` is `parent` or below it, compared case-insensitively on resolved components.

    Case-insensitive everywhere on purpose: on Windows and macOS a path that differs only in
    case is the same directory, and on a case-sensitive host the stricter answer only refuses.
    """
    try:
        child_parts = [part.casefold() for part in PurePath(Path(path).resolve()).parts]
        parent_parts = [part.casefold() for part in PurePath(Path(parent).resolve()).parts]
    except (OSError, ValueError):  # pragma: no cover - an unresolvable path is refused
        return True
    return child_parts[: len(parent_parts)] == parent_parts


def _atomic_write(target: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then `os.replace`; owner-only where honoured.

    NTFS ignores the mode bits - docs/Architecture.md §9 says how to restrict `data_dir`.
    """
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".fc-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _run_installed_app_flow(client_secret_path: Path, scopes: tuple[str, ...]):
    """The interactive loopback OAuth flow. Its prompts go to stderr so `--json` stays clean."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise ValueError(
            "the Google client libraries are not installed - pip install 'filingcabinet[drive]'"
        ) from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(scopes))
    with contextlib.redirect_stdout(sys.stderr):
        return flow.run_local_server(port=0)


def authorize(
    data_dir: Path,
    *,
    client_secret: Path | None = None,
    forbidden: Iterable[Path] = (),
) -> dict:
    """Run the one-time desktop OAuth flow and store the token in `data_dir`.

    Refuses a `data_dir` under any forbidden directory (`[paths].root`, `[paths].snapshot_dir`:
    both Drive-synced). Raises ValueError on anything unusable; returns the token path and the
    granted scope names only.
    """
    data_dir = Path(data_dir)
    for parent in forbidden:
        if parent and _is_inside(data_dir, Path(parent)):
            raise ValueError(
                f"data_dir {data_dir} is inside {parent}, which Drive syncs - Drive credentials "
                "must live in a local-only [paths].data_dir"
            )
    secret_path = data_dir / CLIENT_SECRET_FILENAME
    if client_secret is not None:
        source = Path(client_secret)
        if not source.is_file():
            raise ValueError(f"no client secret file at {source}")
        data_dir.mkdir(parents=True, exist_ok=True)
        if source.resolve() != secret_path.resolve():
            _atomic_write(secret_path, source.read_bytes())
    if not secret_path.is_file():
        raise ValueError(
            f"no client secret in {data_dir} - pass --client-secret FILE (the OAuth desktop "
            "client JSON from the Google Cloud console)"
        )
    try:
        credentials = _run_installed_app_flow(secret_path, SCOPES)
    except ValueError:
        raise
    except Exception as exc:  # the flow's own failure text may carry URLs; keep only the type
        raise ValueError(f"authorization did not complete ({type(exc).__name__})") from None
    token_json = credentials.to_json()
    token_path = data_dir / TOKEN_FILENAME
    _atomic_write(token_path, token_json.encode("utf-8"))
    granted = getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None)
    return {"token_path": str(token_path), "scopes": _scope_names(granted or SCOPES)}


def _token_scopes(token_path: Path) -> list[str]:
    """The token's granted scope names - the only value ever read out of a credential file."""
    try:
        with token_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    return _scope_names(payload.get("scopes")) if isinstance(payload, dict) else []


def doctor_report(
    config: DriveConfig,
    data_dir: Path | None,
    *,
    excludes: tuple[str, ...],
    forbidden: Iterable[Path] = (),
) -> dict:
    """Presence and names only - never a secret value - for `doctor`'s text and `--json`."""
    from .ingest import _matches_any

    secret = token = False
    scopes: list[str] = []
    synced = False
    if data_dir is not None:
        data_dir = Path(data_dir)
        secret = (data_dir / CLIENT_SECRET_FILENAME).is_file()
        token_path = data_dir / TOKEN_FILENAME
        token = token_path.is_file()
        scopes = _token_scopes(token_path) if token else []
        synced = any(parent and _is_inside(data_dir, Path(parent)) for parent in forbidden)
    return {
        "library": library_available(),
        "data_dir": str(data_dir) if data_dir is not None else None,
        "data_dir_synced": synced,
        "client_secret": secret,
        "token": token,
        "scopes": scopes,
        "temp_folder": config.temp_folder,
        "temp_folder_valid": config.temp_folder_valid(),
        "temp_folder_excluded": _matches_any(config.temp_folder, tuple(excludes)),
        "root_folder": config.root_folder,
    }

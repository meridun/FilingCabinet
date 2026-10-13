"""Shared fixtures. `FakeDriveClient` is the in-memory Drive the `drive` rung's tests run on.

No test touches the network or a real credential: the fake holds a tree of synthetic folders
and files built inside each test, records every call, and can be told to fail a method with a
note - the same `DriveError` contract the real client honours.
"""

import pytest

from filingcabinet import drive

WRITE_METHODS = ("create_marked_folder", "copy_as_doc", "delete")


class FakeDriveClient:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.errors: dict[str, str] = {}  # method -> note raised on every call
        self.texts: dict[str, object] = {}  # source file id -> exported text (str or bytes)
        self._counter = 0

    # --- building the tree -----------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def add_folder(self, parent: str, name: str, *, mark: str | None = None) -> str:
        node_id = self._new_id("folder")
        self.nodes[node_id] = {
            "name": name,
            "mime": drive.FOLDER_MIME,
            "size": None,
            "parent": parent,
            "mark": mark,
        }
        return node_id

    def add_file(
        self,
        parent: str,
        name: str,
        *,
        size: int | None = 1,
        text: object = None,
        mark: str | None = None,
    ) -> str:
        node_id = self._new_id("file")
        self.nodes[node_id] = {
            "name": name,
            "mime": "application/pdf",
            "size": size,
            "parent": parent,
            "mark": mark,
        }
        if text is not None:
            self.texts[node_id] = text
        return node_id

    def add_path(self, rel_path: str, *, size: int | None = 1, text: object = None) -> str:
        """A file at ``rel_path`` below My Drive, creating (or reusing) each folder on the way."""
        *folders, name = rel_path.split("/")
        parent = "root"
        for folder in folders:
            existing = [
                node_id
                for node_id, node in self.nodes.items()
                if node["parent"] == parent
                and node["name"] == folder
                and node["mime"] == drive.FOLDER_MIME
            ]
            parent = existing[0] if existing else self.add_folder(parent, folder)
        return self.add_file(parent, name, size=size, text=text)

    # --- inspection ------------------------------------------------------------------------

    def called(self, method: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == method]

    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] in WRITE_METHODS]

    def _entry(self, node_id: str) -> drive.DriveEntry:
        node = self.nodes[node_id]
        return drive.DriveEntry(node_id, node["name"], node["mime"], node["size"])

    def _enter(self, method: str, *args) -> None:
        self.calls.append((method, *args))
        note = self.errors.get(method)
        if note:
            raise drive.DriveError(note)

    # --- the DriveClient protocol ----------------------------------------------------------

    def children(self, parent_id, name):
        self._enter("children", parent_id, name)
        return [
            self._entry(node_id)
            for node_id, node in self.nodes.items()
            if node["parent"] == parent_id and node["name"] == name
        ]

    def marked_folders(self, name):
        self._enter("marked_folders", name)
        return [
            self._entry(node_id)
            for node_id, node in self.nodes.items()
            if node["parent"] == "root"
            and node["name"] == name
            and node["mime"] == drive.FOLDER_MIME
            and node["mark"] == drive.TEMP_FOLDER_MARK
        ]

    def create_marked_folder(self, name):
        self._enter("create_marked_folder", name)
        return self.add_folder("root", name, mark=drive.TEMP_FOLDER_MARK)

    def marked_copies(self, folder_id):
        self._enter("marked_copies", folder_id)
        return [
            node_id
            for node_id, node in self.nodes.items()
            if node["parent"] == folder_id and node["mark"] == drive.COPY_MARK
        ]

    def copy_as_doc(self, file_id, folder_id):
        self._enter("copy_as_doc", file_id, folder_id)
        copy_id = self._new_id("copy")
        self.nodes[copy_id] = {
            "name": f"fc-ocr-{copy_id}",
            "mime": drive.GOOGLE_DOC_MIME,
            "size": None,
            "parent": folder_id,
            "mark": drive.COPY_MARK,
            "source": file_id,
        }
        return copy_id

    def export_text(self, doc_id):
        self._enter("export_text", doc_id)
        text = self.texts.get(self.nodes[doc_id].get("source"), "")
        return text.encode("utf-8") if isinstance(text, str) else text

    def delete(self, file_id):
        self._enter("delete", file_id)
        self.nodes.pop(file_id, None)


@pytest.fixture()
def fake_drive():
    return FakeDriveClient()


@pytest.fixture()
def drive_session(fake_drive):
    """A usable session over ``fake_drive``; pass a DriveConfig to override the defaults."""

    def make(config: drive.DriveConfig | None = None, client=None) -> drive.DriveSession:
        return drive.DriveSession(config or drive.DriveConfig(), client or fake_drive)

    return make

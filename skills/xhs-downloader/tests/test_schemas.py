"""Keep the published schemas honest about what the collector actually writes.

The schemas' whole purpose is that a reader can know a file's shape without
running a collection, so a schema that has drifted from the code is worse than
no schema. These tests run one collection against the fake browser and check the
three real output files field by field against their contracts: every key the
payload carries must be described, and every key the schema requires must be
present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_playbook as playbook
from test_playbook import NOTE, FakeNoteBrowser, patch_browser

SCHEMAS = Path(__file__).parent.parent / "schemas"

SCHEMA_FILES = [
    "note-output.schema.json",
    "comments-output.schema.json",
    "downloads-output.schema.json",
]


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


@pytest.fixture
def collected(tmp_path, monkeypatch):
    """One collected note, in the three files the schemas describe."""
    browser = FakeNoteBrowser()
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(playbook.comments.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(playbook.media, "IMAGE_READY_TIMEOUT", 0.0)
    note_dir = tmp_path / NOTE
    playbook.collect.collect_note(
        1,
        note_dir=note_dir,
        output_path=note_dir / "note.json",
        parts=("note", "image", "comment"),
        comment_limit=3,
    )
    return {
        "note-output.schema.json": note_dir / "note.json",
        "comments-output.schema.json": note_dir / "comments.json",
        "downloads-output.schema.json": note_dir / "downloads.json",
    }


def resolve(schema: dict, ref: str) -> dict:
    assert ref.startswith("#/"), f"only local refs are used here: {ref}"
    node = schema
    for step in ref[2:].split("/"):
        node = node[step]
    return node


def check(schema: dict, payload: object, root: dict, path: str = "$") -> None:
    """Every payload key is described; every required key is there."""
    if "$ref" in schema:
        return check(resolve(root, schema["$ref"]), payload, root, path)
    if schema.get("type") == "array" or "items" in schema:
        assert isinstance(payload, list), f"{path}: 期望数组，实为 {type(payload).__name__}"
        for index, item in enumerate(payload):
            check(schema["items"], item, root, f"{path}[{index}]")
        return
    if not isinstance(payload, dict):
        return
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        assert key in payload, f"{path}: 契约要求 {key}，实际没有"
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(payload) - set(properties))
        assert not unexpected, f"{path}: 契约未描述的字段 {unexpected}"
    for key, value in payload.items():
        if key in properties:
            check(properties[key], value, root, f"{path}.{key}")


DOCS: dict[str, dict] = {name: load_schema(name) for name in SCHEMA_FILES}


def test_the_schemas_are_valid_documents() -> None:
    for name, schema in DOCS.items():
        assert schema["$schema"].endswith("2020-12/schema"), name
        assert schema["type"] == "object", name
        assert schema["required"], name


def test_every_ref_resolves() -> None:
    """A dangling `$ref` makes the contract unreadable where it matters most."""
    for name, schema in DOCS.items():
        for ref in refs_in(schema):
            assert ref.startswith("#/$defs/"), f"{name}: 仅支持本地 $defs 引用：{ref}"
            assert ref.split("/")[-1] in schema["$defs"], f"{name}: 悬空引用 {ref}"


def refs_in(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if "$ref" in node:
            found.append(node["$ref"])
        for value in node.values():
            found.extend(refs_in(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(refs_in(value))
    return found


def test_the_output_files_match_their_schemas(collected) -> None:
    """The real shape, not a hand-written sample of it."""
    for name, path in collected.items():
        check(DOCS[name], json.loads(path.read_text(encoding="utf-8")), DOCS[name])


def test_the_note_file_stays_free_of_collection_detail(collected) -> None:
    """How it was collected is downloads.json's job; the note file is content."""
    note = json.loads(collected["note-output.schema.json"].read_text(encoding="utf-8"))

    assert "source" not in note
    assert "media" not in note and "images" not in note
    assert "comments" not in note and "commentsFile" not in note


def test_the_comment_file_carries_no_source(collected) -> None:
    payload = json.loads(collected["comments-output.schema.json"].read_text(encoding="utf-8"))

    assert "source" not in payload
    assert "rawText" not in payload
    assert set(payload) == set(DOCS["comments-output.schema.json"]["properties"])


REAL_NOTES = Path(
    os.environ.get("XHS_TEST_NOTES", Path.home() / "Documents/travel_agent/notes")
)


def test_a_real_collection_matches_too() -> None:
    """The fixture above is written to fit the contract; a real run is not.

    One collection is machine-local: nothing guarantees a checkout has ever run
    against a live browser, so this skips when the directory is absent. Files
    written by an older layout are skipped rather than checked — each file is
    validated against the contract version it declares, which is what keeps a
    newer schema from retroactively condemning yesterday's output.
    """
    files = {
        "note-output.schema.json": "note.json",
        "comments-output.schema.json": "comments.json",
        "downloads-output.schema.json": "downloads.json",
    }
    checked = 0
    for note_dir in sorted(REAL_NOTES.glob("*")) if REAL_NOTES.is_dir() else []:
        for name, filename in files.items():
            path = note_dir / filename
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schemaVersion") != DOCS[name]["properties"]["schemaVersion"]["const"]:
                continue
            check(DOCS[name], payload, DOCS[name], path=str(path))
            checked += 1
    if not checked:
        pytest.skip(f"尚无当前版本的产出可校验（{REAL_NOTES}）")


def test_a_missing_jsonschema_only_costs_a_deeper_check(collected) -> None:
    """With the dependency present, validate for real rather than by key.

    It is a dev-only extra that is not installed everywhere, so its absence
    skips this one test instead of failing the suite — the key-level checks
    above always run.
    """
    jsonschema = pytest.importorskip("jsonschema")
    for name, path in collected.items():
        jsonschema.validate(
            json.loads(path.read_text(encoding="utf-8")), DOCS[name]
        )

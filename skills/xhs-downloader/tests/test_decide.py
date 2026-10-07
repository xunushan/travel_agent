"""Tests for the screening ledger's CLI.

The whole point of `decide.py` is that it never opens a page, so these tests
never let it near one: everything is driven through `main()` against a real
sqlite file under `tmp_path`, which is also the only way to prove a judgement
reached both the run and the note's status. `sys.argv` is patched rather than
the parser because the argument shape — one comma-separated `--note-id` — is
part of what is being tested.
"""

from __future__ import annotations

import json
import sys

import db
import decide
import pytest

NOTE_ID = "6ac48ca100000000140034ed"
SECOND_ID = "6b0d1e2f3a4b5c6d7e8f9a0b"
ABSENT_ID = "f" * 24
URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=token"


@pytest.fixture
def database(tmp_path) -> str:
    return str(tmp_path / "xhs.db")


def seed(database: str, items: list[dict], **kwargs) -> int:
    """Open a run and record its candidates, the way phase one does."""
    with db.open_db(database) as conn:
        run_id = db.start_run(conn, kind="screen", **kwargs)
        db.add_run_items(conn, run_id, items)
        return run_id


def a_candidate(note_id: str, rank: int, **overrides) -> dict:
    item = {"noteId": note_id, "rank": rank, "href": f"https://x/{rank}"}
    item.update(overrides)
    return item


def record(database: str, run_id: int, note_id: str, decision: str) -> None:
    """Record one judgement straight through `db`, to set up a `show` scenario."""
    with db.open_db(database) as conn:
        db.decide(conn, run_id, [note_id], decision)


def run(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["decide.py", *argv])
    return decide.main()


# --- keep and drop ---------------------------------------------------------


def test_keep_records_the_decision_and_mirrors_it_onto_the_note(database, monkeypatch) -> None:
    run_id = seed(database, [a_candidate(NOTE_ID, 1, href=URL)])
    with db.open_db(database) as conn:
        db.store_note(conn, {"noteId": NOTE_ID, "title": "川西赏秋时间表"},
                      status=db.STATUS_SCREENED)

    code = run(
        ["keep", "--run-id", str(run_id), "--note-id", NOTE_ID,
         "--reason", "有时间表和机位", "--db", database],
        monkeypatch,
    )

    assert code == 0
    with db.open_db(database) as conn:
        item = db.run_items(conn, run_id)[0]
        assert item["decision"] == db.DECISION_KEEP
        assert item["reason"] == "有时间表和机位"
        assert db.note_row(conn, NOTE_ID)["status"] == db.STATUS_APPROVED


def test_one_flag_accepts_several_ids_and_tolerates_stray_spaces(database, monkeypatch) -> None:
    """The user rejected a repeatable flag; the ids arrive in one comma-separated value."""
    run_id = seed(database, [a_candidate(NOTE_ID, 1), a_candidate(SECOND_ID, 2)])

    code = run(
        ["drop", "--run-id", str(run_id),
         "--note-id", f"  {NOTE_ID} , {SECOND_ID}  ", "--db", database],
        monkeypatch,
    )

    assert code == 0
    with db.open_db(database) as conn:
        decisions = {item["note_id"]: item["decision"] for item in db.run_items(conn, run_id)}
    assert decisions == {NOTE_ID: db.DECISION_DROP, SECOND_ID: db.DECISION_DROP}


def test_an_empty_note_id_is_refused_rather_than_recording_nothing(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [a_candidate(NOTE_ID, 1)])

    code = run(["keep", "--run-id", str(run_id), "--note-id", " , , ", "--db", database], monkeypatch)

    assert code != 0
    assert "--note-id" in capsys.readouterr().out


# --- ids that were never candidates ----------------------------------------


def test_an_id_outside_the_run_is_reported_but_the_good_one_still_lands(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [a_candidate(NOTE_ID, 1)])

    code = run(
        ["keep", "--run-id", str(run_id), "--note-id", f"{NOTE_ID},{ABSENT_ID}",
         "--db", database],
        monkeypatch,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert ABSENT_ID in out
    with db.open_db(database) as conn:
        assert db.run_items(conn, run_id)[0]["decision"] == db.DECISION_KEEP


def test_recording_nothing_at_all_is_a_nonzero_exit(database, monkeypatch, capsys) -> None:
    """Zero recorded means the run id is wrong far more often than the run is empty."""
    run_id = seed(database, [a_candidate(NOTE_ID, 1)])

    code = run(
        ["keep", "--run-id", str(run_id), "--note-id", ABSENT_ID, "--db", database],
        monkeypatch,
    )

    assert code == 1
    assert ABSENT_ID in capsys.readouterr().out


def test_an_unknown_run_id_errors_without_creating_a_run(database, monkeypatch, capsys) -> None:
    code = run(["keep", "--run-id", "999", "--note-id", NOTE_ID, "--db", database], monkeypatch)

    assert code != 0
    assert "999" in capsys.readouterr().out
    with db.open_db(database) as conn:
        assert db.run_row(conn, 999) is None
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# --- show ------------------------------------------------------------------


def test_json_round_trips_and_carries_the_fields_batch_needs(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [a_candidate(NOTE_ID, 1, href=URL, keyword="川西秋色")])

    code = run(["show", "--run-id", str(run_id), "--json", "--db", database], monkeypatch)

    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)  # stdout must be a bare array, nothing beside it
    assert "合计" not in out
    assert payload[0]["noteId"] == NOTE_ID
    assert payload[0]["href"] == URL
    assert payload[0]["decision"] == db.DECISION_PENDING
    assert payload[0]["keywords"] == ["川西秋色"]


def test_show_prints_a_one_line_tally_of_the_whole_run(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [
        a_candidate(NOTE_ID, 1),
        a_candidate(SECOND_ID, 2),
        a_candidate(ABSENT_ID, 3),
    ])
    record(database, run_id, NOTE_ID, db.DECISION_KEEP)
    record(database, run_id, SECOND_ID, db.DECISION_DROP)

    code = run(["show", "--run-id", str(run_id), "--db", database], monkeypatch)

    assert code == 0
    assert "合计 3 候选：keep 1 / drop 1 / 待定 1" in capsys.readouterr().out


def test_show_uses_the_stored_title_and_falls_back_to_the_bare_id(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [a_candidate(NOTE_ID, 1), a_candidate(SECOND_ID, 2)])
    with db.open_db(database) as conn:
        db.store_note(conn, {"noteId": NOTE_ID, "title": "川西赏秋时间表"}, status=db.STATUS_SCREENED)

    code = run(["show", "--run-id", str(run_id), "--db", database], monkeypatch)

    out = capsys.readouterr().out
    assert code == 0
    assert "川西赏秋时间表" in out
    # The unknown one is still a candidate; its id is the only label it has.
    assert f"{SECOND_ID}  待定  {SECOND_ID}" in out


def test_show_can_be_narrowed_to_one_decision_and_kept_short(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [
        a_candidate(NOTE_ID, 1),
        a_candidate(SECOND_ID, 2),
        a_candidate(ABSENT_ID, 3),
    ])
    record(database, run_id, SECOND_ID, db.DECISION_DROP)

    run(
        ["show", "--run-id", str(run_id), "--decision", db.DECISION_DROP,
         "--limit", "1", "--json", "--db", database],
        monkeypatch,
    )

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["noteId"] == SECOND_ID
    assert payload[0]["decision"] == db.DECISION_DROP


def test_an_empty_run_is_not_an_error(database, monkeypatch, capsys) -> None:
    run_id = seed(database, [])

    code = run(["show", "--run-id", str(run_id), "--db", database], monkeypatch)

    assert code == 0
    assert "没有任何候选" in capsys.readouterr().out

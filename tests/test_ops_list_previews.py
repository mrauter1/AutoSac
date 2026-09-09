from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, JSON, MetaData, String, Table, Uuid, create_engine, event
from sqlalchemy.orm import Session

from app.ops_list_previews import load_ops_list_previews, text_excerpt


def test_excerpt_normalizes_plain_text_and_bounds_preview():
    assert text_excerpt(" a\n b\t c ", 5) == "a b c"
    assert text_excerpt("abcdef", 5) == "abcde…"
    assert text_excerpt(None, 5) == ""
    assert text_excerpt({"unexpected": "object"}, 5) == ""


def test_empty_list_does_not_query():
    assert load_ops_list_previews(None, []) == {}


def test_bulk_previews_select_latest_deterministically_without_summary_fallback():
    # Minimal real SQL tables exercise the production SELECTs without unrelated schema setup.
    metadata = MetaData()
    users = Table("users", metadata, Column("id", Uuid, primary_key=True), Column("display_name", String))
    messages = Table(
        "ticket_messages", metadata,
        Column("id", Uuid, primary_key=True), Column("ticket_id", Uuid),
        Column("body_text", String), Column("author_type", String),
        Column("author_user_id", Uuid), Column("visibility", String), Column("created_at", DateTime),
    )
    runs = Table(
        "ai_runs", metadata,
        Column("id", Uuid, primary_key=True), Column("ticket_id", Uuid),
        Column("status", String), Column("final_output_contract", String),
        Column("final_output_json", JSON), Column("created_at", DateTime),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    first, second, empty, excluded, author = [uuid4() for _ in range(5)]
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.execute(users.insert(), [{"id": author, "display_name": "Maria"}])
        db.execute(messages.insert(), [
            {"id": UUID(int=number), "ticket_id": ticket_id, "body_text": body,
             "author_type": author_type, "author_user_id": user_id,
             "visibility": visibility, "created_at": now}
            for number, ticket_id, body, author_type, user_id, visibility in [
                (1, first, "Old public message", "requester", author, "public"),
                (2, first, " Latest\n internal message ", "operator", author, "internal"),
                (3, second, "AI reply", "ai", None, "public"),
                (4, excluded, "Not in results", "ai", None, "internal"),
            ]
        ])
        db.execute(runs.insert(), [
            {"id": UUID(int=number), "ticket_id": ticket_id, "status": status,
             "final_output_contract": contract, "final_output_json": payload, "created_at": now}
            for number, ticket_id, status, contract, payload in [
                (1, first, "succeeded", "triage_result", {"summary_internal": "Old summary"}),
                (2, first, "human_review", "specialist_result", {}),
                (3, second, "succeeded", "human_handoff_result", {"summary_internal": "Current summary"}),
                (4, second, "failed", "specialist_result", {"summary_internal": "Failed summary"}),
                (5, excluded, "succeeded", "triage_result", {"summary_internal": "Excluded"}),
            ]
        ])
        statements = []
        event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
        previews = load_ops_list_previews(db, [{"ticket": SimpleNamespace(id=id)} for id in (first, second, empty)])
        assert len(statements) == 2
        assert set(previews) == {first, second, empty}
        assert previews[first]["message"] == {
            "excerpt": "Latest internal message", "author_name": "Maria",
            "author_type": "operator", "visibility": "internal",
        }
        assert previews[first]["summary"] == ""
        assert previews[second]["summary"] == "Current summary"
        assert previews[second]["message"]["author_name"] is None
        assert previews[empty] == {}
    engine.dispose()

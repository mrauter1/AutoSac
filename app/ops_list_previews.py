"""Read-only, list-only previews: two queries regardless of the row count."""
from types import SimpleNamespace

from sqlalchemy import func, select

from app.ai_run_presenters import present_ai_run_output
from shared.models import AIRun, TicketMessage, User


def text_excerpt(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def load_ops_list_previews(db, rows: list[dict]) -> dict:
    ticket_ids = [row["ticket"].id for row in rows]
    if not ticket_ids:
        return {}
    messages = select(
        TicketMessage.ticket_id, TicketMessage.body_text,
        TicketMessage.author_type, TicketMessage.author_user_id, TicketMessage.visibility,
        func.row_number().over(
            partition_by=TicketMessage.ticket_id,
            order_by=(TicketMessage.created_at.desc(), TicketMessage.id.desc()),
        ).label("position"),
    ).where(TicketMessage.ticket_id.in_(ticket_ids)).subquery()
    previews = {ticket_id: {} for ticket_id in ticket_ids}
    for message in db.execute(
        select(messages, User.display_name.label("author_name"))
        .outerjoin(User, User.id == messages.c.author_user_id)
        .where(messages.c.position == 1)
    ).mappings():
        previews[message["ticket_id"]]["message"] = {
            "excerpt": text_excerpt(message["body_text"], 180),
            "author_name": message["author_name"],
            "author_type": message["author_type"],
            "visibility": message["visibility"],
        }
    # Match the detail page's latest eligible analysis, not the latest nonempty summary.
    analyses = select(
        AIRun.ticket_id, AIRun.final_output_contract,
        AIRun.final_output_json["summary_internal"].as_string().label("summary_internal"),
        func.row_number().over(
            partition_by=AIRun.ticket_id,
            order_by=(AIRun.created_at.desc(), AIRun.id.desc()),
        ).label("position"),
    ).where(
        AIRun.ticket_id.in_(ticket_ids),
        AIRun.status.in_(("succeeded", "human_review")),
        AIRun.final_output_json.is_not(None),
    ).subquery()
    for analysis in db.execute(select(analyses).where(analyses.c.position == 1)).mappings():
        output = present_ai_run_output(SimpleNamespace(
            final_output_contract=analysis["final_output_contract"],
            final_output_json={"summary_internal": analysis["summary_internal"]},
        ))
        previews[analysis["ticket_id"]]["summary"] = text_excerpt(output["summary_internal"], 500)
    return previews

"""Assistant tool for evolving the data structure from chat — adding custom
fields/columns. Low-risk metadata (a FieldDef), so the assistant applies it
directly and confirms; back-filling the values is a separate, reviewed step.
"""

from app.graph import queries
from app.graph.driver import get_driver
from app.graph.models import APPLIES_TO, field_key


async def add_field(
    label: str, description: str, applies_to_kind: str = "all", field_type: str = "list"
) -> dict:
    """Add a new custom field / column for companies. Use when the user asks to add
    a field or column. `label` is the display name (e.g. "Service Lines"),
    `description` says what to research for it, `applies_to_kind` is one of
    service_provider / isv / cloud_provider (or "all"), and `field_type` is "list"
    (several values) or "text". After adding, tell the user the column now exists
    and they can ask you to research it to fill it in for existing companies."""
    if applies_to_kind not in APPLIES_TO:
        return {"error": f"applies_to_kind must be one of {APPLIES_TO}"}
    if field_type not in ("list", "text"):
        field_type = "list"
    return await queries.add_field_def(
        get_driver(), field_key(label), label, description, applies_to_kind, field_type
    )

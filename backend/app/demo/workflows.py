"""A workflow built from the platform's own business tools.

    get_shipment ──▶ get_charges ──▶ is_exception ──┬─ true ─▶ cancel ──▶ confirm
                                                    └─ false ─▶ (end)

Five steps, and every one of them does something real: two lookups, a branch on
what the first one found, a destructive action that stops for a person, and a
second lookup that confirms what the action did. Data flows between them by
reference - the charges step reads the same input the first did, and the branch
reads the shipment's status out of what ``get_shipment`` returned.

``cancel_shipment`` is classified destructive, so the tool framework refuses it
without a decision and the run pauses there. That is the approval: it names the
exact execution it authorises, which is what makes "the approved action ran at
most once" a statement about a row rather than about a code path. There is no
separate approval step in front of it, because a second gate would authorise
nothing the first did not.

This is a *demonstration* definition, not a fixture the application seeds
automatically. Something has to create it deliberately - a script, a test, or a
person through the API - for the same reason the demo dataset does: an
application that invents business processes at start-up is one nobody can trust
in staging.
"""

from __future__ import annotations

from typing import Any

SHIPMENT_EXCEPTION_WORKFLOW = "Shipment exception review"

SHIPMENT_EXCEPTION_DESCRIPTION = (
    "Look a shipment up, total what is owed on it, and - if it is in exception - "
    "ask somebody whether to cancel it, then confirm what happened."
)


def shipment_exception_definition() -> dict[str, Any]:
    """The definition document, as it is stored and as the API accepts it.

    A plain dictionary rather than the parsed model: this is what somebody would
    POST, so building it here keeps the demo honest about the shape a user
    actually writes.

    Two paths end the workflow, which is what graph validation requires: the
    false branch of the condition, for a shipment that is fine, and ``confirm``
    for one that was cancelled.
    """
    return {
        "entry": "get_shipment",
        "steps": [
            {
                "id": "get_shipment",
                "type": "tool_call",
                "description": "Where is the shipment, and who is it for?",
                "tool": "get_shipment",
                "arguments": {"shipment_reference": "$.input.shipment_reference"},
                "next": "get_charges",
            },
            {
                "id": "get_charges",
                "type": "tool_call",
                "description": "What is billed against it, and what is still owed?",
                "tool": "get_shipment_charges",
                "arguments": {"shipment_reference": "$.input.shipment_reference"},
                "next": "is_exception",
            },
            {
                "id": "is_exception",
                "type": "condition",
                "description": "Only a shipment in exception is a candidate for cancelling.",
                "condition": {
                    "field": "$.steps.get_shipment.output.shipment.status",
                    "operator": "equals",
                    "value": "exception",
                },
                "on_true": "cancel",
                # Nothing to do for a shipment that is travelling normally, so
                # the run ends here rather than inventing a step to say so.
                "on_false": None,
            },
            {
                "id": "cancel",
                "type": "tool_call",
                "description": "Destructive: the framework stops here for a person.",
                "tool": "cancel_shipment",
                "arguments": {
                    "shipment_reference": "$.input.shipment_reference",
                    "reason": "$.input.reason",
                },
                "next": "confirm",
            },
            {
                "id": "confirm",
                "type": "tool_call",
                "description": "Read the shipment back, so the result is what the record says.",
                "tool": "get_shipment",
                "arguments": {"shipment_reference": "$.input.shipment_reference"},
                "next": None,
            },
        ],
    }

"""Shared ground for the business tools.

The one thing worth stating: a business tool holds a **session**, not a
repository. Repositories are built inside `execute`, from the organization on
the trusted `ToolExecutionContext`. That ordering is the tenant guarantee - a
tool cannot hold a repository scoped to the wrong tenant, because it does not
hold one at all until it knows which tenant it is acting for.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.base import Tool
from app.tools.exceptions import ToolError


class BusinessRecordNotFoundError(ToolError):
    """The record does not exist for this organization.

    One answer for "no such shipment" and "that shipment belongs to somebody
    else", because distinguishing them would confirm another tenant's data to
    anyone who could guess a reference. The message names no organization.
    """

    status_code = 404
    code = "business_record_not_found"
    message = "No matching record was found."


def not_found(kind: str) -> BusinessRecordNotFoundError:
    """A not-found error naming the kind of record, but nothing about who owns it."""
    error = BusinessRecordNotFoundError(
        f"No {kind} was found with that reference.", code=f"{kind}_not_found"
    )
    return error


class BusinessTool[InputT: BaseModel, OutputT: BaseModel](Tool[InputT, OutputT]):
    """A tool that reads one organization's operational records.

    Constructed with the request's session so its queries take part in the same
    transaction as the rest of the request - which is also what lets a test roll
    everything back afterwards.
    """

    __abstract__ = True

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

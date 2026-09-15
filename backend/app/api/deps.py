"""Shared FastAPI dependencies.

Annotated aliases keep endpoint signatures short and make the wiring explicit
in one place instead of being repeated across routers.

The authorization chain, in order:

``CurrentUser``
    Who is calling, from a verified access token.
``CurrentMembership``
    Which organization they are acting in, and their role in it. Chosen by the
    client but *verified* against the database on every request - a header
    naming an organization the caller does not belong to is refused, not
    honoured.
``CurrentOrganization``
    The organization itself, derived from the membership.
``require_role(...)``
    A dependency factory refusing callers whose role is not sufficient.

Membership and role are read from the database each time rather than carried in
the token, so revoking a membership or reducing a role takes effect on the next
request instead of when the token happens to expire.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, ClassVar

from fastapi import Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.ai.gateway import LLMGateway
from app.core.config import Settings
from app.core.database import get_session
from app.core.exceptions import AppError, PermissionDeniedError, UnauthorizedError
from app.core.security import TokenError, decode_access_token
from app.models.enums import MemberRole, OrganizationStatus, UserStatus
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.repositories.membership import MembershipLookup
from app.repositories.user import UserRepository
from app.services.agent_execution import AgentExecutionService, RunView
from app.services.ai import AIService
from app.services.approvals import ApprovalService
from app.services.auth import AuthService
from app.services.health import HealthService
from app.services.membership import MembershipService
from app.services.workflow_execution import WorkflowService
from app.tools.business import build_business_registry
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

ORGANIZATION_HEADER = "X-Organization-ID"

# auto_error=False so a missing header raises our own 401 in the standard error
# envelope rather than Starlette's bare {"detail": ...}.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Access token from POST /api/v1/auth/login, sent as: Authorization: Bearer <token>",
)


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running application was built with.

    Read from application state rather than the process-wide loader, so an
    application constructed with explicit settings - as tests do - behaves
    consistently everywhere, including inside endpoints.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
"""Settings of the running application."""

SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""Database session scoped to the request; commits on success, rolls back on error."""


def get_health_service(settings: SettingsDep) -> HealthService:
    return HealthService(settings)


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
"""Health and readiness reporting."""


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
"""Registration and login."""


# -- Authentication -----------------------------------------------------------


class InvalidCredentials(UnauthorizedError):
    """401 carrying the WWW-Authenticate header the Bearer scheme requires."""

    headers: ClassVar[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Resolve the caller from their access token.

    Every rejection - absent, malformed, expired, wrongly signed, or naming a
    user who no longer exists or is no longer active - produces the same 401.
    """
    if credentials is None or not credentials.credentials:
        raise InvalidCredentials("Not authenticated.")

    try:
        claims = decode_access_token(credentials.credentials, settings)
    except TokenError as exc:
        raise InvalidCredentials("Could not validate credentials.") from exc

    user = await UserRepository(session).get(claims.user_id)

    # A token outliving its user, or issued before the account was suspended,
    # must stop working immediately - which is why this is checked per request.
    if user is None or user.status is not UserStatus.ACTIVE:
        raise InvalidCredentials("Could not validate credentials.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
"""The authenticated user."""


# -- Tenant context -----------------------------------------------------------


class OrganizationRequired(AppError):
    """The caller belongs to several organizations and named none of them."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "organization_required"
    message = (
        f"You belong to more than one organization. Send the {ORGANIZATION_HEADER} "
        "header to choose which one this request acts on."
    )


async def get_current_membership(
    session: SessionDep,
    user: CurrentUser,
    organization_id: Annotated[
        uuid.UUID | None,
        Header(
            alias=ORGANIZATION_HEADER,
            description=(
                "Organization this request acts on. Required only when you belong to "
                "more than one. Always checked against your membership."
            ),
        ),
    ] = None,
) -> OrganizationMember:
    """Resolve which organization the caller is acting in, and their role.

    The header is a *request*, not a grant. It is resolved against the caller's
    own active memberships, so naming another tenant's id yields a 403 rather
    than access. When it is absent and the caller belongs to exactly one
    organization, that one is used.

    Three things must hold, and all three are checked against the database on
    every request: the user is active (``get_current_user``), the membership is
    active (the lookup filters on it), and the organization itself is active.
    """
    lookup = MembershipLookup(session)

    if organization_id is not None:
        membership = await lookup.get_for_user_and_organization(user.id, organization_id)
        # Deliberately identical whether the organization does not exist, exists
        # and the caller is not in it, or exists and is suspended: otherwise
        # this endpoint would confirm the existence and state of other tenants.
        if membership is None or not _organization_is_usable(membership):
            raise PermissionDeniedError("You do not have access to that organization.")
        return membership

    memberships = [m for m in await lookup.list_for_user(user.id) if _organization_is_usable(m)]
    if not memberships:
        raise PermissionDeniedError("You do not have access to an active organization.")
    if len(memberships) > 1:
        raise OrganizationRequired()

    return memberships[0]


def _organization_is_usable(membership: OrganizationMember) -> bool:
    """Whether the organization behind a membership may still be acted on.

    Suspending or archiving an organization has to stop its members working in
    it. Without this the status column would be decorative: the user's status
    and the membership's status are both enforced, and an organization that had
    been suspended for abuse or non-payment would carry on regardless.
    """
    return membership.organization.status is OrganizationStatus.ACTIVE


CurrentMembership = Annotated[OrganizationMember, Depends(get_current_membership)]
"""The caller's verified membership of the organization this request acts on."""


async def get_current_organization(membership: CurrentMembership) -> Organization:
    """The organization this request acts on.

    Derived from the verified membership, never from the request directly.
    """
    return membership.organization


CurrentOrganization = Annotated[Organization, Depends(get_current_organization)]
"""The organization this request acts on."""


async def get_current_organization_id(membership: CurrentMembership) -> uuid.UUID:
    """The tenant id to scope repositories with."""
    return membership.organization_id


CurrentOrganizationId = Annotated[uuid.UUID, Depends(get_current_organization_id)]
"""Tenant id for constructing a TenantScopedRepository."""


# -- Authorization ------------------------------------------------------------

# Ordered from least to most privileged. A role satisfies a requirement if it
# appears at or above the required rank, so require_role(ADMIN) also admits an
# owner without every call site having to list both.
ROLE_RANK: dict[MemberRole, int] = {
    MemberRole.MEMBER: 0,
    MemberRole.ADMIN: 1,
    MemberRole.OWNER: 2,
}


RoleDependency = Callable[..., Awaitable[OrganizationMember]]


def require_role(minimum: MemberRole) -> RoleDependency:
    """Build a dependency admitting only roles at or above *minimum*.

        @router.delete("/members/{user_id}", dependencies=[Depends(require_role(MemberRole.ADMIN))])

    Authentication failures stay 401; a valid caller whose role is too low gets
    403, so a client can tell "log in" from "you cannot do this".
    """

    async def dependency(membership: CurrentMembership) -> OrganizationMember:
        if ROLE_RANK[membership.role] < ROLE_RANK[minimum]:
            raise PermissionDeniedError(f"This action requires the {minimum.value} role or higher.")
        return membership

    return dependency


RequireOwner = Annotated[OrganizationMember, Depends(require_role(MemberRole.OWNER))]
"""Owner-only operations."""

RequireAdmin = Annotated[OrganizationMember, Depends(require_role(MemberRole.ADMIN))]
"""Administrative operations; owners qualify too."""

RequireMember = Annotated[OrganizationMember, Depends(require_role(MemberRole.MEMBER))]
"""Any active member of the organization."""


# -- Language models ----------------------------------------------------------


def get_llm_gateway(request: Request, settings: SettingsDep) -> LLMGateway:
    """The application's gateway, built once and reused.

    Cached on application state rather than in a module-level singleton, so
    each application built by ``create_app`` - including each one a test builds
    - has its own and they cannot interfere.

    Built on first use rather than at start-up because a deployment with no
    provider credential must still start; the failure belongs on the first
    model call, not on boot. Two concurrent first requests may each build one
    and discard a duplicate, which is harmless and happens at most once.
    """
    gateway: LLMGateway | None = getattr(request.app.state, "llm_gateway", None)
    if gateway is None:
        gateway = LLMGateway.from_settings(settings)
        request.app.state.llm_gateway = gateway
    return gateway


LLMGatewayDep = Annotated[LLMGateway, Depends(get_llm_gateway)]
"""The configured LLM gateway."""


def get_ai_service(gateway: LLMGatewayDep, settings: SettingsDep) -> AIService:
    return AIService(gateway, settings)


AIServiceDep = Annotated[AIService, Depends(get_ai_service)]
"""Application-level model access. Endpoints depend on this, never on a provider."""


def get_agent_registry(request: Request, settings: SettingsDep) -> AgentRegistry:
    """The deployment's agents, built once and reused.

    Cached on application state for the same reason the gateway is: each
    application ``create_app`` builds - including each one a test builds - gets
    its own, and a test that registers an agent cannot leak it into another.
    """
    registry: AgentRegistry | None = getattr(request.app.state, "agent_registry", None)
    if registry is None:
        registry = AgentRegistry.from_settings(settings)
        request.app.state.agent_registry = registry
    return registry


AgentRegistryDep = Annotated[AgentRegistry, Depends(get_agent_registry)]
"""The agents this deployment offers."""


def get_tool_registry(session: SessionDep) -> ToolRegistry:
    """The tools this deployment has, built for this request.

    Per request rather than cached on application state, unlike the agent
    registry. The business tools read the database, so each one is bound to the
    session this request is already using - which keeps a tool inside the
    caller's transaction instead of opening a second one that cannot see it.

    Constructing four small objects per request is not a cost worth optimising
    away; sharing a session across requests would be a correctness bug.
    """
    return build_business_registry(session)


ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]
"""The tools this deployment offers."""


def get_tool_executor(registry: ToolRegistryDep, settings: SettingsDep) -> ToolExecutor:
    return ToolExecutor(registry, settings)


ToolExecutorDep = Annotated[ToolExecutor, Depends(get_tool_executor)]
"""The controlled boundary every tool call passes through."""


def get_agent_runtime(
    gateway: LLMGatewayDep,
    registry: AgentRegistryDep,
    settings: SettingsDep,
    tools: ToolExecutorDep,
) -> AgentRuntime:
    return AgentRuntime(gateway, registry, settings, tools)


AgentRuntimeDep = Annotated[AgentRuntime, Depends(get_agent_runtime)]
"""Controlled agent execution. Endpoints depend on this, never on a provider."""


def get_agent_execution_service(
    request: Request,
    session: SessionDep,
    membership: CurrentMembership,
    settings: SettingsDep,
    executor: ToolExecutorDep,
    registry: ToolRegistryDep,
) -> AgentExecutionService:
    """Durable agent execution, fixed to the caller's organization.

    The runtime is built on demand rather than injected. Most of what this
    service does - reading a run back, listing recent runs, cancelling a paused
    one - never calls a model, and an eagerly built runtime made every one of
    those endpoints fail with a provider error on a deployment that has no
    provider credential configured. The same reasoning, and the same fix, as the
    approval and workflow services.
    """

    def runtime() -> AgentRuntime:
        return AgentRuntime(
            resolve_llm_gateway(request, settings),
            get_agent_registry(request, settings),
            settings,
            executor,
        )

    return AgentExecutionService(session, runtime, settings, membership, registry)


AgentExecutionServiceDep = Annotated[AgentExecutionService, Depends(get_agent_execution_service)]
"""Durable runs, stored conversations, and the idempotency guarantee."""


def resolve_llm_gateway(request: Request, settings: Settings) -> LLMGateway:
    """The application's gateway, for the places that build one on demand.

    Two services deliberately do *not* take the gateway as a dependency, because
    building it fails without a provider credential and most of what they do
    never touches a model. They resolve it here instead, when they actually need
    one.

    That means stepping outside FastAPI's dependency graph, so this consults the
    override table itself. Without that, an application whose gateway has been
    replaced - which is how every test puts a scripted provider behind it -
    would quietly get the real one back at exactly the moment it mattered.
    """
    override = request.app.dependency_overrides.get(get_llm_gateway)
    if override is not None:
        gateway: LLMGateway = override()
        return gateway
    return get_llm_gateway(request, settings)


class LazyAgentStepRunner:
    """The agent execution service, built only if a workflow step needs one.

    Most workflow work never touches a model: drafting a definition, activating
    it, listing runs, reading steps, and running a workflow made of tool and
    condition steps. Building the LLM gateway eagerly would make every one of
    those fail on a deployment that has no provider credential - a 500 about a
    provider, answering a request that never wanted one.

    So the gateway is resolved here, on the first agent step, through the same
    cached dependency the AI endpoints use. A deployment without a credential
    can therefore manage and run workflows, and an *agent* step in one still
    fails with the configuration error it should.
    """

    def __init__(
        self,
        request: Request,
        session: AsyncSession,
        settings: Settings,
        membership: OrganizationMember,
        executor: ToolExecutor,
        registry: ToolRegistry,
    ) -> None:
        self._request = request
        self._session = session
        self._settings = settings
        self._membership = membership
        self._executor = executor
        self._registry = registry
        self._service: AgentExecutionService | None = None

    def _runtime(self) -> AgentRuntime:
        return AgentRuntime(
            resolve_llm_gateway(self._request, self._settings),
            get_agent_registry(self._request, self._settings),
            self._settings,
            self._executor,
        )

    def _resolve(self) -> AgentExecutionService:
        if self._service is None:
            self._service = AgentExecutionService(
                self._session, self._runtime, self._settings, self._membership, self._registry
            )
        return self._service

    async def run_for_workflow(
        self, *, agent_id: str, message: str, idempotency_key: str, request_id: str | None
    ) -> RunView:
        return await self._resolve().run_for_workflow(
            agent_id=agent_id,
            message=message,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    async def get(self, run_id: uuid.UUID) -> RunView:
        return await self._resolve().get(run_id)


def get_workflow_service(
    request: Request,
    session: SessionDep,
    membership: CurrentMembership,
    settings: SettingsDep,
    executor: ToolExecutorDep,
    registry: ToolRegistryDep,
    agents: AgentRegistryDep,
) -> WorkflowService:
    """Workflow definitions and runs, fixed to the caller's organization.

    It is handed the *existing* tool executor and a lazily-built handle on the
    *existing* agent execution service rather than anything of its own: a
    workflow orchestrates what the platform can already do, and a second runtime
    or a second executor is the thing this design exists to not have.
    """
    return WorkflowService(
        session,
        settings,
        membership,
        tools=executor,
        registry=registry,
        agents=LazyAgentStepRunner(request, session, settings, membership, executor, registry),
        agent_registry=agents,
    )


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]
"""Workflow definitions, durable runs, and the engine that drives them."""


def get_approval_service(
    request: Request,
    session: SessionDep,
    membership: CurrentMembership,
    settings: SettingsDep,
    executor: ToolExecutorDep,
    registry: ToolRegistryDep,
    workflows: WorkflowServiceDep,
) -> ApprovalService:
    """Reading and deciding approvals, fixed to the caller's organization.

    Building the service does not authorise anything: whether this caller may
    *decide* is settled by the role dependency on the endpoint, which is the
    security boundary. A member may hold one of these and only ever read.

    The workflow service comes in because a decision resumes whatever was
    waiting - an agent run, a workflow run, or both when an agent step inside a
    workflow paused. One approval subsystem, two kinds of process.
    """

    def runtime() -> AgentRuntime:
        """The agent runtime, built only if an agent run has to be resumed."""
        return AgentRuntime(
            resolve_llm_gateway(request, settings),
            get_agent_registry(request, settings),
            settings,
            executor,
        )

    return ApprovalService(session, runtime, settings, membership, executor, registry, workflows)


ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]
"""The approval queue, and the decisions that resume a paused run."""


def get_membership_service(session: SessionDep, membership: CurrentMembership) -> MembershipService:
    """Membership administration, fixed to the caller's organization."""
    return MembershipService(session, membership)


MembershipServiceDep = Annotated[MembershipService, Depends(get_membership_service)]
"""Membership administration for the current organization."""

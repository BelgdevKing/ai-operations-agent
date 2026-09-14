"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep, CurrentUser, SessionDep
from app.repositories.membership import MembershipLookup
from app.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    MembershipSummary,
    OrganizationSummary,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserProfile,
)
from app.schemas.common import ErrorResponse

router = APIRouter()

UNAUTHENTICATED: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"}
}


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a user and their organization",
    responses={
        409: {"model": ErrorResponse, "description": "Email already registered"},
        422: {"model": ErrorResponse, "description": "Invalid email or password"},
    },
)
async def register(payload: RegisterRequest, service: AuthServiceDep) -> RegisterResponse:
    """Create an account.

    Creates the user, an organization for them, and the membership joining the
    two with the **owner** role - an organization with no owner could never be
    administered. An access token is returned so the caller does not have to
    log in immediately afterwards.
    """
    user, organization, role = await service.register(payload)
    token = service.issue_token(user.id)

    return RegisterResponse(
        user=UserProfile.model_validate(user),
        organization=OrganizationSummary.model_validate(organization),
        role=role,
        token=token,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access token",
    responses={
        401: {"model": ErrorResponse, "description": "Incorrect email or password"},
    },
)
async def login(payload: LoginRequest, service: AuthServiceDep) -> TokenResponse:
    """Log in.

    A wrong password, an address that was never registered, and a suspended
    account all produce the same 401 with the same message, and cost roughly
    the same time - so this endpoint cannot be used to discover which
    addresses have accounts.
    """
    user = await service.authenticate(payload)
    return service.issue_token(user.id)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The authenticated user's profile",
    responses=UNAUTHENTICATED,
)
async def read_current_user(user: CurrentUser, session: SessionDep) -> CurrentUserResponse:
    """Return the caller's profile and the organizations they belong to.

    Built from an explicit schema, so nothing from the ``users`` row beyond the
    listed fields can be returned - ``password_hash`` in particular.
    """
    memberships = await MembershipLookup(session).list_for_user(user.id)

    return CurrentUserResponse(
        user=UserProfile.model_validate(user),
        memberships=[
            MembershipSummary(
                organization=OrganizationSummary.model_validate(membership.organization),
                role=membership.role,
            )
            for membership in memberships
        ],
    )

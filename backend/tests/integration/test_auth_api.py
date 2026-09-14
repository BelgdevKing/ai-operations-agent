"""Registration, login and /me against a real database."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import decode_access_token
from app.models.enums import MemberRole, MembershipStatus, OrganizationStatus, UserStatus
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from tests.integration.auth_helpers import PASSWORD, login, register

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def registration_payload(**overrides: object) -> dict[str, object]:
    unique = uuid.uuid4().hex[:10]
    payload: dict[str, object] = {
        "email": f"user-{unique}@example.com",
        "password": PASSWORD,
        "organization_name": f"Org {unique}",
    }
    payload.update(overrides)
    return payload


# -- Registration -------------------------------------------------------------


async def test_registration_creates_user_organization_and_owner_membership(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    response = await api_client.post(REGISTER, json=registration_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == MemberRole.OWNER

    user = await session.get(User, uuid.UUID(body["user"]["id"]))
    organization = await session.get(Organization, uuid.UUID(body["organization"]["id"]))
    assert user is not None
    assert organization is not None
    assert user.status is UserStatus.ACTIVE
    assert organization.status is OrganizationStatus.ACTIVE

    membership = (
        await session.execute(
            select(OrganizationMember).where(OrganizationMember.user_id == user.id)
        )
    ).scalar_one()
    assert membership.organization_id == organization.id
    assert membership.role is MemberRole.OWNER
    assert membership.status is MembershipStatus.ACTIVE


async def test_registration_never_returns_the_password_hash(api_client: AsyncClient) -> None:
    """The single most important thing this endpoint must not leak."""
    response = await api_client.post(REGISTER, json=registration_payload())

    assert "password" not in response.text.lower()
    assert "argon2" not in response.text
    assert set(response.json()["user"]) == {
        "id",
        "email",
        "first_name",
        "last_name",
        "status",
    }


async def test_registration_stores_a_hash_not_the_password(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    response = await api_client.post(REGISTER, json=registration_payload())
    user = await session.get(User, uuid.UUID(response.json()["user"]["id"]))

    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash != PASSWORD
    assert user.password_hash.startswith("$argon2id$")


async def test_registration_returns_a_usable_token(api_client: AsyncClient) -> None:
    response = await api_client.post(REGISTER, json=registration_payload())
    body = response.json()

    token = body["token"]["access_token"]
    me = await api_client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200
    assert me.json()["user"]["id"] == body["user"]["id"]


async def test_duplicate_email_is_rejected(api_client: AsyncClient) -> None:
    payload = registration_payload()
    assert (await api_client.post(REGISTER, json=payload)).status_code == 201

    response = await api_client.post(REGISTER, json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_email_case_and_whitespace_do_not_create_a_second_account(
    api_client: AsyncClient,
) -> None:
    """Otherwise Ada@Example.com and ada@example.com would be two accounts,
    which is an account-takeover footgun."""
    payload = registration_payload(email="Ada.Lovelace@Example.COM")
    first = await api_client.post(REGISTER, json=payload)
    assert first.status_code == 201
    assert first.json()["user"]["email"] == "ada.lovelace@example.com"

    duplicate = await api_client.post(
        REGISTER, json=registration_payload(email="  ada.lovelace@example.com  ")
    )

    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "@example.com", "user@", "user example@test.com", ""],
)
async def test_invalid_emails_are_rejected(email: str, api_client: AsyncClient) -> None:
    response = await api_client.post(REGISTER, json=registration_payload(email=email))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("password", ["", "short", "12345678901"])
async def test_weak_passwords_are_rejected(password: str, api_client: AsyncClient) -> None:
    response = await api_client.post(REGISTER, json=registration_payload(password=password))

    assert response.status_code == 422


async def test_a_password_containing_the_email_is_rejected(api_client: AsyncClient) -> None:
    response = await api_client.post(
        REGISTER,
        json=registration_payload(email="jonathan@example.com", password="jonathan-jonathan"),
    )

    assert response.status_code == 422


async def test_an_over_long_password_is_rejected(api_client: AsyncClient) -> None:
    """Argon2 cost grows with input; an unbounded password is a cheap DoS."""
    response = await api_client.post(REGISTER, json=registration_payload(password="x" * 1000))

    assert response.status_code == 422


async def test_organization_slug_is_derived_and_made_unique(api_client: AsyncClient) -> None:
    first = await api_client.post(REGISTER, json=registration_payload(organization_name="Acme Ltd"))
    second = await api_client.post(
        REGISTER, json=registration_payload(organization_name="Acme Ltd")
    )

    assert first.json()["organization"]["slug"] == "acme-ltd"
    assert second.json()["organization"]["slug"].startswith("acme-ltd-")
    assert first.json()["organization"]["slug"] != second.json()["organization"]["slug"]


# -- Login --------------------------------------------------------------------


async def test_login_with_valid_credentials_returns_a_token(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.post(LOGIN, json={"email": account.email, "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["access_token"]


async def test_login_token_identifies_the_user(api_client: AsyncClient, settings: Settings) -> None:
    account = await register(api_client)
    token = await login(api_client, account.email)

    claims = decode_access_token(token, settings)

    assert claims.user_id == account.user_id


async def test_login_is_case_insensitive_in_the_email(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.post(
        LOGIN, json={"email": account.email.upper(), "password": PASSWORD}
    )

    assert response.status_code == 200


async def test_login_with_a_wrong_password_fails(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.post(
        LOGIN, json={"email": account.email, "password": "not the password"}
    )

    assert response.status_code == 401


async def test_login_with_an_unknown_email_fails(api_client: AsyncClient) -> None:
    response = await api_client.post(
        LOGIN, json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert response.status_code == 401


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    api_client: AsyncClient,
) -> None:
    """Otherwise this endpoint enumerates which addresses have accounts."""
    account = await register(api_client)

    wrong_password = await api_client.post(
        LOGIN, json={"email": account.email, "password": "not the password"}
    )
    unknown_email = await api_client.post(
        LOGIN, json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["error"] == unknown_email.json()["error"]


async def test_a_suspended_account_cannot_log_in(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    user = await session.get(User, account.user_id)
    assert user is not None
    user.status = UserStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(LOGIN, json={"email": account.email, "password": PASSWORD})

    assert response.status_code == 401
    # Same message as a wrong password: that an address belongs to a suspended
    # account is not something a stranger should be able to confirm.
    assert response.json()["error"]["message"] == "Incorrect email or password."


async def test_login_response_contains_only_the_token(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.post(LOGIN, json={"email": account.email, "password": PASSWORD})

    assert set(response.json()) == {"access_token", "token_type", "expires_in"}


# -- /me ----------------------------------------------------------------------


async def test_me_returns_the_caller_and_their_memberships(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.get(ME, headers=account.headers())

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(account.user_id)
    assert body["user"]["email"] == account.email
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["role"] == MemberRole.OWNER
    assert body["memberships"][0]["organization"]["id"] == str(account.organization_id)


async def test_me_never_returns_the_password_hash(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    user = await session.get(User, account.user_id)
    assert user is not None and user.password_hash is not None

    response = await api_client.get(ME, headers=account.headers())

    assert user.password_hash not in response.text
    assert "password_hash" not in response.text


async def test_me_requires_authentication(api_client: AsyncClient) -> None:
    response = await api_client.get(ME)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "token abc.def.ghi"},
    ],
)
async def test_me_rejects_bad_authorization_headers(
    header: dict[str, str], api_client: AsyncClient
) -> None:
    response = await api_client.get(ME, headers=header)

    assert response.status_code == 401


async def test_a_token_for_a_deleted_user_stops_working(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Identity is re-checked per request rather than trusted from the token."""
    account = await register(api_client)
    assert (await api_client.get(ME, headers=account.headers())).status_code == 200

    user = await session.get(User, account.user_id)
    assert user is not None
    user.status = UserStatus.DEACTIVATED
    await session.flush()

    assert (await api_client.get(ME, headers=account.headers())).status_code == 401

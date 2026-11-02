"""Explicit OIDC account associations, persisted without a schema extension."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy.auth import oidc_identity as identity
from litellm.proxy.auth.oidc_settings import require_admin
from litellm.proxy._types import UserAPIKeyAuth

PREFIX = "independent_oidc_link:"
LOCK_ID = 319067301


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class ApproveRequest(RevisionRequest):
    user_id: str = Field(min_length=1)


class LinkView(StrictModel):
    identity_key: str
    issuer: str
    subject: str
    email: str | None
    proposed_role: str
    state: Literal["pending", "linked", "rejected", "unlinked"]
    revision: int
    user_id: str | None
    current_configuration: bool
    candidates: list[str]
    updated_at: str


class UserChoice(StrictModel):
    user_id: str
    user_email: str | None
    user_role: str | None
    sso_status: Literal["none", "independent_oidc", "unconfirmed_oidc", "legacy"]


class LinksResponse(StrictModel):
    enabled: bool
    links: list[LinkView]
    users: list[UserChoice]


router = APIRouter(prefix="/oidc/links", tags=["OIDC account linking"])


def problem(status: int, code: str, message: str):
    raise HTTPException(
        status,
        {"code": code, "message": message},
        headers={"Cache-Control": "no-store"},
    )


def signing_key() -> bytes:
    from litellm.proxy.common_utils.encrypt_decrypt_utils import get_salt_key

    key = get_salt_key()
    if not key:
        problem(
            503,
            "linking_unavailable",
            "A stable encryption key is required for account linking.",
        )
    return key.encode()


def signed(value: dict) -> dict:
    data = {k: v for k, v in value.items() if k != "signature"}
    body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return {
        **data,
        "signature": hmac.new(
            signing_key(), b"oidc-link\0" + body, hashlib.sha256
        ).hexdigest(),
    }


def verified(row) -> dict | None:
    if row is None:
        return None
    value = row.param_value
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not isinstance(value.get("signature"), str):
        problem(
            503, "linking_unavailable", "Account association integrity check failed."
        )
    if not secrets.compare_digest(value["signature"], signed(value)["signature"]):
        problem(
            503, "linking_unavailable", "Account association integrity check failed."
        )
    if row.param_name != PREFIX + value.get("identity_key", ""):
        problem(
            503, "linking_unavailable", "Account association integrity check failed."
        )
    return value


@asynccontextmanager
async def transaction():
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        problem(
            503, "linking_unavailable", "Account linking requires the user database."
        )
    async with prisma_client.writer_db.tx(timeout=timedelta(seconds=15)) as tx:
        # One short transaction serializes decisions across independent proxy workers.
        await tx.query_raw(
            "SELECT pg_advisory_xact_lock($1::bigint)::text AS locked", LOCK_ID
        )
        yield tx


async def load(tx, key: str) -> dict | None:
    return verified(
        await tx.litellm_config.find_unique(where={"param_name": PREFIX + key})
    )


async def save(tx, value: dict) -> None:
    value["updated_at"] = datetime.now(UTC).isoformat()
    serialized = json.dumps(signed(value))
    await tx.litellm_config.upsert(
        where={"param_name": PREFIX + value["identity_key"]},
        data={
            "create": {
                "param_name": PREFIX + value["identity_key"],
                "param_value": serialized,
            },
            "update": {"param_value": serialized},
        },
    )


def email_key(value: str | None) -> str | None:
    # No alias, plus-address or dot normalization. Address matching only suggests.
    return (
        value.strip().casefold() if isinstance(value, str) and value.strip() else None
    )


def candidates(record: dict, users) -> list[str]:
    email = email_key(record.get("email"))
    return [u.user_id for u in users if email and email_key(u.user_email) == email]


def view(record: dict, config, users) -> LinkView:
    return LinkView(
        **{
            k: record[k]
            for k in LinkView.model_fields
            if k not in ("current_configuration", "candidates")
        },
        current_configuration=bool(
            config and record.get("config") == config.fingerprint
        ),
        candidates=candidates(record, users),
    )


async def locked_user(tx, user_id: str):
    await tx.query_raw(
        'SELECT user_id FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR UPDATE', user_id
    )
    return await tx.litellm_usertable.find_unique(where={"user_id": user_id})


def new_record(config, claims, key, *, state="pending", user_id=None):
    return {
        "identity_key": key,
        "issuer": config.issuer,
        "subject": identity.claim_value(claims, config.subject_claim),
        "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
        "proposed_role": config.role_for(claims),
        "state": state,
        "revision": 1,
        "user_id": user_id,
        "config": config.fingerprint,
        "updated_at": datetime.now(UTC).isoformat(),
    }


@dataclass(frozen=True)
class ResolvedAccount:
    user: Any
    revision: int


def verified_browser_identity(config, user) -> tuple[str, dict] | None:
    """One fail-closed predicate for signed legacy adoption and session identity."""
    if user is None:
        return None
    state = identity.verified_identity_state(config, user.user_id, user.metadata or {})
    claims = state.get("claims")
    if not isinstance(claims, dict):
        return None
    try:
        key = identity.user_id_for_claims(config, claims)
    except HTTPException:
        return None
    return (key, claims) if key == user.sso_user_id else None


async def invalidate_browser_state(tx, config, user):
    metadata = dict(user.metadata or {})
    metadata[identity.METADATA_KEY] = identity.sign_identity_state(
        config,
        user.user_id,
        {
            "active": False,
            "epoch": secrets.token_urlsafe(24),
            "config": config.fingerprint,
        },
    )
    await tx.litellm_usertable.update(
        where={"user_id": user.user_id}, data={"metadata": json.dumps(metadata)}
    )
    await tx.litellm_verificationtoken.delete_many(
        where={
            "user_id": user.user_id,
            "token": {"startswith": identity.session_prefix(user.user_id)},
        }
    )


async def deny_browser_account(config, claims):
    """A denied old identity cannot revoke a newly relinked user's sessions."""
    key = identity.user_id_for_claims(config, claims)
    async with transaction() as tx:
        record = await load(tx, key)
        if record and record["state"] == "linked":
            user = await locked_user(tx, record["user_id"])
        elif record is None:
            user = await tx.litellm_usertable.find_unique(where={"sso_user_id": key})
            if verified_browser_identity(config, user) is None:
                return None
            # Preserve verified pre-feature provenance before revoking its policy.
            await save(
                tx,
                new_record(config, claims, key, state="linked", user_id=user.user_id),
            )
        else:
            return None
        if user is not None and user.sso_user_id == key:
            await invalidate_browser_state(tx, config, user)
            return user.user_id
    return None


async def resolve_account(config, claims):
    """Resolve a verified, admitted browser or API identity to one native user."""
    key = identity.user_id_for_claims(config, claims)
    pending = False
    async with transaction() as tx:
        record = await load(tx, key)
        if record is not None:
            if record["state"] == "linked":
                user = await locked_user(tx, record["user_id"])
                if user is None or user.sso_user_id != key:
                    problem(
                        409,
                        "link_conflict",
                        "The linked account changed. Administrator review is required.",
                    )
                return ResolvedAccount(user, record["revision"])
            # Fresh verified claims refresh the review, never remove rejection/unlink state.
            updated = new_record(config, claims, key)
            if any(
                updated[k] != record.get(k)
                for k in ("config", "email", "proposed_role")
            ):
                record.update(
                    {k: updated[k] for k in ("config", "email", "proposed_role")}
                )
                record["revision"] += 1
                await save(tx, record)
            pending = True
        else:
            existing = await tx.litellm_usertable.find_unique(
                where={"sso_user_id": key}
            )
            if existing is not None:
                # Only integrity-protected pre-linking browser accounts can be adopted.
                trusted = verified_browser_identity(config, existing)
                if trusted is not None and trusted[0] == key:
                    record = new_record(
                        config, claims, key, state="linked", user_id=existing.user_id
                    )
                    await save(tx, record)
                    return ResolvedAccount(existing, record["revision"])
                record = new_record(config, claims, key)
                await save(tx, record)
                pending = True
            users = await tx.litellm_usertable.find_many()
            record = new_record(config, claims, key)
            if (
                pending
                or candidates(record, users)
                or any(u.user_id == key for u in users)
            ):
                await save(tx, record)
                pending = True
            else:
                user = await tx.litellm_usertable.create(
                    data={
                        "user_id": key,
                        "sso_user_id": key,
                        "user_role": config.default_role,
                        "user_email": record["email"],
                        "models": [],
                    }
                )
                record.update(state="linked", user_id=user.user_id)
                await save(tx, record)
                return ResolvedAccount(user, record["revision"])
    if pending:
        problem(
            403, "linking_required", "Account linking requires administrator review."
        )


async def update_browser_account(
    config,
    claims,
    user_id: str,
    expected_revision: int,
    values: dict,
    *,
    revoke_sessions: bool,
):
    key = identity.user_id_for_claims(config, claims)
    async with transaction() as tx:
        record = await load(tx, key)
        user = await locked_user(tx, user_id)
        if (
            not record
            or record["state"] != "linked"
            or record["user_id"] != user_id
            or record["revision"] != expected_revision
            or not user
            or user.sso_user_id != key
        ):
            problem(
                403,
                "linking_required",
                "Account linking requires administrator review.",
            )
        if revoke_sessions:
            await tx.litellm_verificationtoken.delete_many(
                where={
                    "user_id": user_id,
                    "token": {"startswith": identity.session_prefix(user_id)},
                }
            )
        await tx.litellm_usertable.update(where={"user_id": user_id}, data=values)


async def issue_browser_session(config, claims, user):
    """Create the handle under the same lock as unlink and association changes."""
    key = identity.user_id_for_claims(config, claims)
    if user is None:
        problem(
            403, "linking_required", "Account linking requires administrator review."
        )
    expected = identity.verified_identity_state(
        config, user.user_id, user.metadata or {}
    )
    async with transaction() as tx:
        record = await load(tx, key)
        current = await locked_user(tx, user.user_id)
        state = (
            identity.verified_identity_state(
                config, user.user_id, current.metadata or {}
            )
            if current
            else {}
        )
        if (
            not record
            or record["state"] != "linked"
            or record["user_id"] != user.user_id
            or not current
            or current.sso_user_id != key
            or not state.get("active")
            or state.get("epoch") != expected.get("epoch")
            or state.get("association_revision") != record["revision"]
            or state.get("config") != config.fingerprint
            or state.get("claims") != config.retained_policy_claims(claims)
            or current.user_role != config.role_for(claims)
        ):
            problem(
                403,
                "linking_required",
                "Account linking requires administrator review.",
            )
        session_id = secrets.token_urlsafe(32)
        session_hash = (
            identity.session_prefix(user.user_id)
            + hashlib.sha256(session_id.encode()).hexdigest()
        )
        await tx.litellm_verificationtoken.create(
            data={
                "token": session_hash,
                "user_id": user.user_id,
                "team_id": UI_SESSION_TOKEN_TEAM_ID,
                "models": [],
                "blocked": True,
                "expires": datetime.now(UTC)
                + timedelta(seconds=config.session_ttl_seconds),
                "metadata": json.dumps({"noenterprise_oidc_session": True}),
            }
        )
        return current, state, session_id, record["revision"]


async def association_active(
    config, user, *, expected_identity=None, expected_revision=None
) -> bool:
    """Bind browser claims and session generation to the currently linked identity."""
    trusted = verified_browser_identity(config, user)
    if trusted is None or (
        expected_identity is not None and expected_identity != trusted[0]
    ):
        return False
    async with transaction() as tx:
        record = await load(tx, trusted[0])
        if record is None:
            # Existing signed browser sessions remain usable; next login adopts the link.
            return expected_revision is None
        return (
            record["state"] == "linked"
            and record["user_id"] == user.user_id
            and (expected_revision is None or record["revision"] == expected_revision)
        )


@router.get("", response_model=LinksResponse)
async def list_links(auth: UserAPIKeyAuth = Depends(require_admin)):
    config = identity.settings()
    async with transaction() as tx:
        users = await tx.litellm_usertable.find_many()
        records = [
            verified(row)
            for row in await tx.litellm_config.find_many(
                where={"param_name": {"startswith": PREFIX}}
            )
        ]
        # Expose trustworthy existing browser accounts before their next login.
        known = {r["identity_key"] for r in records}
        if config:
            for user in users:
                if not user.sso_user_id or user.sso_user_id in known:
                    continue
                trusted = verified_browser_identity(config, user)
                if trusted is not None:
                    key, claims = trusted
                    record = new_record(
                        config, claims, key, state="linked", user_id=user.user_id
                    )
                    record["email"] = user.user_email
                    await save(tx, record)
                    records.append(record)
        unconfirmed = {r["identity_key"] for r in records if r["state"] == "pending"}
        linked = {
            r["user_id"]: r["identity_key"] for r in records if r["state"] == "linked"
        }
        return LinksResponse(
            enabled=config is not None,
            links=[view(r, config, users) for r in records],
            users=[
                UserChoice(
                    user_id=u.user_id,
                    user_email=u.user_email,
                    user_role=u.user_role,
                    sso_status="independent_oidc"
                    if u.sso_user_id and linked.get(u.user_id) == u.sso_user_id
                    else "unconfirmed_oidc"
                    if u.user_id == u.sso_user_id and u.user_id in unconfirmed
                    else "legacy"
                    if u.sso_user_id
                    else "none",
                )
                for u in users
            ],
        )


async def change(key: str, action: str, data: RevisionRequest, auth: UserAPIKeyAuth):
    config = identity.require_settings()
    changed_user = None
    async with transaction() as tx:
        record = await load(tx, key)
        if record is None:
            problem(404, "link_not_found", "Account linking request not found.")
        target = getattr(data, "user_id", None)
        retry = (
            record.get("last_action") == action
            and record.get("last_revision") == data.expected_revision
            and record.get("last_target") == target
        )
        if retry:
            return view(record, config, await tx.litellm_usertable.find_many())
        if record["revision"] != data.expected_revision:
            problem(
                409, "link_conflict", "This request changed. Reload before continuing."
            )
        before = {
            "state": record["state"],
            "user_id": record["user_id"],
            "revision": record["revision"],
        }
        if action == "approve":
            if (
                record["state"] != "pending"
                or record.get("config") != config.fingerprint
            ):
                problem(
                    409,
                    "link_conflict",
                    "A current verified pending request is required.",
                )
            user = await locked_user(tx, target)
            bound = await tx.litellm_usertable.find_unique(where={"sso_user_id": key})
            same_unconfirmed = (
                user is not None and user.user_id == key and user.sso_user_id == key
            )
            if (
                user is None
                or (user.sso_user_id and not same_unconfirmed)
                or (bound is not None and not same_unconfirmed)
            ):
                problem(
                    409,
                    "link_conflict",
                    "The account or identity already has an SSO association. No accounts were merged.",
                )
            await tx.litellm_usertable.update(
                where={"user_id": target}, data={"sso_user_id": key}
            )
            record.update(state="linked", user_id=target)
            changed_user = target
        elif action == "unlink":
            if record["state"] != "linked":
                problem(409, "link_conflict", "Only a linked identity can be unlinked.")
            user = await locked_user(tx, record["user_id"])
            if user is not None:
                if user.sso_user_id != key:
                    problem(
                        409,
                        "link_conflict",
                        "The account association changed. Reload before continuing.",
                    )
                await invalidate_browser_state(tx, config, user)
                await tx.litellm_usertable.update(
                    where={"user_id": user.user_id}, data={"sso_user_id": None}
                )
                changed_user = user.user_id
            record.update(state="unlinked")
        elif action == "reject":
            if record["state"] != "pending":
                problem(409, "link_conflict", "Only pending requests can be rejected.")
            record.update(state="rejected")
        elif action == "reopen":
            if record["state"] not in ("rejected", "unlinked"):
                problem(
                    409,
                    "link_conflict",
                    "Only rejected or unlinked requests can be reopened.",
                )
            record.update(state="pending")
        record.update(
            last_action=action,
            last_revision=data.expected_revision,
            last_target=target,
            revision=record["revision"] + 1,
        )
        await save(tx, record)
        # Reuse the native audit table without requiring an Enterprise audit callback.
        await tx.litellm_auditlog.create(
            data={
                "changed_by": auth.user_id or "native-administrator",
                "action": "update",
                "table_name": "LiteLLM_UserTable",
                "object_id": record["user_id"] or key,
                "before_value": json.dumps(before),
                "updated_values": json.dumps(
                    {
                        "oidc_action": action,
                        "identity_key": key,
                        "user_id": record["user_id"],
                        "state": record["state"],
                        "revision": record["revision"],
                    }
                ),
            }
        )
        result = view(record, config, await tx.litellm_usertable.find_many())
    if changed_user:
        await identity.invalidate_user_auth(changed_user)
    return result


@router.post("/{identity_key}/approve", response_model=LinkView)
async def approve(
    identity_key: str,
    data: ApproveRequest,
    auth: UserAPIKeyAuth = Depends(require_admin),
):
    return await change(identity_key, "approve", data, auth)


@router.post("/{identity_key}/reject", response_model=LinkView)
async def reject(
    identity_key: str,
    data: RevisionRequest,
    auth: UserAPIKeyAuth = Depends(require_admin),
):
    return await change(identity_key, "reject", data, auth)


@router.post("/{identity_key}/reopen", response_model=LinkView)
async def reopen(
    identity_key: str,
    data: RevisionRequest,
    auth: UserAPIKeyAuth = Depends(require_admin),
):
    return await change(identity_key, "reopen", data, auth)


@router.post("/{identity_key}/unlink", response_model=LinkView)
async def unlink(
    identity_key: str,
    data: RevisionRequest,
    auth: UserAPIKeyAuth = Depends(require_admin),
):
    return await change(identity_key, "unlink", data, auth)


async def guard_native_user_write(values: dict, *, creating: bool = False):
    """Native writes cannot mint identities or clear an association behind its lifecycle."""
    sso = values.get("sso_user_id")
    reserved_id = values.get("user_id")
    if (isinstance(sso, str) and sso.startswith("oidc-")) or (
        creating and isinstance(reserved_id, str) and reserved_id.startswith("oidc-")
    ):
        problem(
            409,
            "link_conflict",
            "Independent OIDC identifiers are managed through account linking.",
        )
    if not creating and "sso_user_id" in values:
        where = (
            {"user_id": values["user_id"]}
            if values.get("user_id")
            else {"user_email": values.get("user_email")}
        )
        users = await identity.user_repository().table.find_many(where=where)
        if any(u.sso_user_id and u.sso_user_id.startswith("oidc-") for u in users):
            problem(
                409,
                "link_conflict",
                "Change independent OIDC associations through account linking.",
            )
    if identity.METADATA_KEY in (values.get("metadata") or {}):
        problem(409, "link_conflict", "OIDC identity metadata is server-managed.")

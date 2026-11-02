"""Historical database input for imported-user compatibility, never test assertions.

These rows describe pre-linking API-only and signed browser accounts, an unrelated
legacy SSO account, and ambiguous native emails. All behavior is observed via HTTP.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from prisma import Prisma


async def main():
    db = Prisma()
    await db.connect()
    for subject, kind in [("historical-api", "api"), ("historical-browser", "browser")]:
        key = (
            "oidc-"
            + hashlib.sha256(("http://issuer:8080\0" + subject).encode()).hexdigest()
        )
        metadata = {}
        if kind == "browser":
            state = {
                "active": True,
                "claims": {"oid": subject, "roles": ["access"]},
                "epoch": "historical-epoch",
            }
            body = json.dumps(
                [key, state], sort_keys=True, separators=(",", ":")
            ).encode()
            state["signature"] = hmac.new(
                b"fixture-session-secret-32-characters-minimum",
                b"oidc-identity-state\0" + body,
                hashlib.sha256,
            ).hexdigest()
            metadata = {"noenterprise_oidc": state}
        await db.litellm_usertable.create(
            data={
                "user_id": key,
                "sso_user_id": key,
                "user_email": subject + "@example.test",
                "user_role": "internal_user_viewer",
                "models": [],
                "metadata": json.dumps(metadata),
            }
        )
    for user_id, email, sso in [
        (
            "historical-legacy",
            "historical-legacy@example.test",
            "different-legacy-subject",
        ),
        ("ambiguous-a", "ambiguous@example.test", None),
        ("ambiguous-b", "ambiguous@example.test", None),
    ]:
        await db.litellm_usertable.create(
            data={
                "user_id": user_id,
                "user_email": email,
                "sso_user_id": sso,
                "user_role": "internal_user",
                "models": [],
            }
        )
    api_id = "oidc-" + hashlib.sha256(b"http://issuer:8080\0historical-api").hexdigest()
    await db.litellm_budgettable.create(
        data={
            "budget_id": "historical-budget",
            "created_by": "fixture",
            "updated_by": "fixture",
            "max_budget": 50,
        }
    )
    await db.litellm_organizationtable.create(
        data={
            "organization_id": "historical-org",
            "organization_alias": "Historical fixture",
            "budget_id": "historical-budget",
            "models": [],
            "created_by": "fixture",
            "updated_by": "fixture",
        }
    )
    await db.litellm_teamtable.create(
        data={
            "team_id": "historical-team",
            "team_alias": "Historical fixture",
            "organization_id": "historical-org",
            "admins": [],
            "members": [api_id],
            "models": ["fixture-model"],
            "members_with_roles": json.dumps([{"user_id": api_id, "role": "user"}]),
        }
    )
    await db.litellm_usertable.update(
        where={"user_id": api_id},
        data={
            "organization_id": "historical-org",
            "teams": ["historical-team"],
            "max_budget": 42,
            "spend": 1.25,
        },
    )
    await db.litellm_organizationmembership.create(
        data={
            "user_id": api_id,
            "organization_id": "historical-org",
            "user_role": "internal_user",
        }
    )
    await db.litellm_teammembership.create(
        data={"user_id": api_id, "team_id": "historical-team", "spend": 1.25}
    )
    key = hashlib.sha256(b"sk-historical-key-fixture-only").hexdigest()
    await db.litellm_verificationtoken.create(
        data={
            "token": key,
            "user_id": api_id,
            "models": ["fixture-model"],
            "team_id": "historical-team",
        }
    )
    now = datetime.now(timezone.utc)
    await db.litellm_spendlogs.create(
        data={
            "request_id": "historical-request",
            "call_type": "acompletion",
            "api_key": key,
            "user": api_id,
            "team_id": "historical-team",
            "organization_id": "historical-org",
            "model": "fixture-model",
            "spend": 1.25,
            "total_tokens": 100,
            "startTime": now,
            "endTime": now,
        }
    )
    await db.disconnect()


asyncio.run(main())

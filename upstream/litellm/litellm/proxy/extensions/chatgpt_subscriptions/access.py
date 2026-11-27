"""Resolve authenticated gateway identities and configured grants."""
import hashlib
import re
import sys
from dataclasses import dataclass

from fastapi import HTTPException
from litellm.caching.dual_cache import DualCache

_directory = DualCache()


def proxy_db():
    return getattr(sys.modules.get("litellm.proxy.proxy_server"), "prisma_client", None)


def key_identity(value):
    if not isinstance(value, str) or not value:
        return None
    digest = value if re.fullmatch(r"[a-f0-9]{64}", value) else hashlib.sha256(value.encode()).hexdigest()
    return "key:" + digest


@dataclass(frozen=True)
class Caller:
    user: str | None
    team: str | None
    key: str | None = None

    @property
    def identity(self):
        return self.user or self.key

    @classmethod
    def from_auth(cls, auth):
        return cls(auth.user_id, auth.team_id, key_identity(auth.api_key) if not auth.user_id else None)

    @classmethod
    def from_request(cls, data):
        logger = (data or {}).get("litellm_logging_obj")
        bound = getattr(logger, "_subscription_caller", None)
        if isinstance(bound, cls):
            return bound
        for field in ("litellm_metadata", "metadata"):
            meta = (data or {}).get(field)
            if not isinstance(meta, dict):
                continue
            user = meta.get("user_api_key_user_id")
            team = meta.get("user_api_key_team_id")
            key = meta.get("user_api_key_hash") or meta.get("user_api_key")
            if user or team or key:
                return cls(user, team, None if user else key_identity(key))
        return cls(None, None)


async def directory(group, ids):
    """Map known IDs to memberships (users) or an empty list (teams), caching misses too."""
    wanted = set(ids)
    db = proxy_db()
    if not wanted or db is None:
        return {}
    prefix = "chatgpt:directory:" + group + ":"
    found = {i: _directory.get_cache(prefix + i) for i in wanted}
    missing = [i for i, v in found.items() if v is None]
    if missing:
        try:
            if group == "users":
                rows = await db.db.litellm_usertable.find_many(where={"user_id": {"in": missing}})
                present = {row.user_id: list(row.teams or []) for row in rows}
            else:
                rows = await db.db.litellm_teamtable.find_many(where={"team_id": {"in": missing}})
                present = {row.team_id: [] for row in rows}
        except Exception:
            raise HTTPException(503, "Subscription directory is unavailable.") from None
        for identifier in missing:
            found[identifier] = present.get(identifier, False)
            _directory.set_cache(prefix + identifier, found[identifier], ttl=60)
    return {i: teams for i, teams in found.items() if teams is not False}


class Grants:
    def __init__(self, users, teams, factors):
        self.users = users
        self.teams = teams
        self.factors = factors

    @classmethod
    async def resolve(cls, records, factors=None, callers=()):
        factors = factors or {"teams": {}, "users": {}}
        users, teams = set(factors["users"]), set(factors["teams"])
        for record in records:
            users.update(record["access"]["users"])
            teams.update(record["access"]["teams"])
        # Only unscoped keys need membership expansion.
        if teams:
            users.update(c.user for c in callers if c.user and not c.team)
        return cls(await directory("users", users), await directory("teams", teams), factors)

    def matching_teams(self, caller):
        teams = [caller.team] if caller.team else self.users.get(caller.user, ())
        return [t for t in teams if t in self.teams]

    def permits(self, access, caller):
        if not access["users"] and not access["teams"]:
            return True
        return (caller.user in self.users and caller.user in access["users"]
                or bool(set(self.matching_teams(caller)).intersection(access["teams"])))

    def factor(self, caller):
        if caller.user in self.users and caller.user in self.factors["users"]:
            return self.factors["users"][caller.user]
        weights = self.factors["teams"]
        return max((weights[t] for t in self.matching_teams(caller) if t in weights), default=1)

    def unknown(self, access):
        return {k: sorted((set(access[k]) | self.factors[k].keys()) - known.keys())
                for k, known in (("teams", self.teams), ("users", self.users))}

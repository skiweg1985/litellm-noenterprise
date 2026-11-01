"""Token file access shared with the patched LiteLLM authenticator.

Protocol, identical in litellm/llms/chatgpt/authenticator.py:
- writers hold an exclusive flock on "<auth file>.lock";
- files are replaced atomically, so readers never see partial content;
- the account "default" uses "<token dir>/auth.json", any other account
  "<token dir>/accounts/<name>/auth.json"; CHATGPT_ACCOUNTS lists the names.
"""

import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 30
DEFAULT_ACCOUNT = "default"
ACCOUNT_KEY_PREFIX = "chatgpt-account:"
ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_held = threading.local()
logger = logging.getLogger("chatgpt_subscriptions")


class LockTimeout(Exception):
    """Another process keeps the token file locked."""


def legacy_configured_accounts():
    """Account names from CHATGPT_ACCOUNTS; invalid names are skipped."""
    names = []
    for name in (os.getenv("CHATGPT_ACCOUNTS") or DEFAULT_ACCOUNT).split(","):
        name = name.strip()
        if not name or name in names:
            continue
        if ACCOUNT_NAME.fullmatch(name):
            names.append(name)
        else:
            logger.error("Ignoring invalid ChatGPT account name %r in CHATGPT_ACCOUNTS.", name)
    return names


def configured_accounts():
    """Legacy aliases plus durable dashboard accounts, excluding retired entries."""
    from .registry import Registry
    records = Registry().records()
    return [name for name in dict.fromkeys([*legacy_configured_accounts(), *records])
            if not records.get(name, {}).get("retired")]


def account_for_api_key(api_key):
    """The account a deployment's api_key names, like the patched authenticator;
    None without one, which the authenticator rejects with 401."""
    if isinstance(api_key, str) and api_key.startswith(ACCOUNT_KEY_PREFIX):
        return api_key[len(ACCOUNT_KEY_PREFIX):]
    return None


def auth_path(account=DEFAULT_ACCOUNT):
    if not ACCOUNT_NAME.fullmatch(account):
        raise ValueError(f"Invalid ChatGPT account name: {account!r}")
    directory = Path(os.getenv(
        "CHATGPT_TOKEN_DIR", os.path.expanduser("~/.config/litellm/chatgpt")
    ))
    if account != DEFAULT_ACCOUNT:
        directory = directory / "accounts" / account
    return directory / os.getenv("CHATGPT_AUTH_FILE", "auth.json")


def claims(token):
    try:
        part = token.split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError, TypeError, AttributeError):
        return {}


def build_record(tokens, previous=None):
    """Same fields as LiteLLM's Authenticator._build_auth_record."""
    access = tokens["access_token"]
    identity = claims(tokens.get("id_token") or access)
    auth = identity.get("https://api.openai.com/auth")
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    record = {
        key: value
        for key, value in (previous or {}).items()
        if key not in ("device_code_requested_at",)
    }
    record.update(
        {
            "access_token": access,
            "refresh_token": tokens.get("refresh_token")
            or (previous or {}).get("refresh_token"),
            "id_token": tokens.get("id_token") or (previous or {}).get("id_token"),
            "expires_at": claims(access).get("exp"),
            "account_id": account,
        }
    )
    return record


def identity(record):
    """Non-secret facts about the signed-in account, for admins only.

    account_id is the ChatGPT workspace; in a Team or Business plan all
    members share it. user_id and email name the person, so a quota seat is
    the pair of workspace and person.
    """
    token = (record or {}).get("id_token") or (record or {}).get("access_token")
    values = claims(token) if isinstance(token, str) else {}
    auth = values.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    text = lambda value: value if isinstance(value, str) and value else None
    return {
        "email": text(values.get("email")),
        "account_id": text((record or {}).get("account_id")) or text(auth.get("chatgpt_account_id")),
        "user_id": text(auth.get("chatgpt_user_id")) or text(auth.get("user_id")),
        "plan": text(auth.get("chatgpt_plan_type")),
    }


def same_identity(known, other):
    """Same workspace and same person; the person by user id, else by email."""
    if not known or not other or known.get("account_id") != other.get("account_id"):
        return False
    if known.get("user_id") and other.get("user_id"):
        return known["user_id"] == other["user_id"]
    email = lambda value: (value.get("email") or "").lower()
    return bool(email(known)) and email(known) == email(other)


@contextlib.contextmanager
def locked(path=None, timeout=LOCK_TIMEOUT_SECONDS):
    """Exclusive lock on "<path>.lock", re-entrant within one thread."""
    path = Path(path or auth_path())
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = {}
    key = str(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout() from None
                time.sleep(0.05)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read(path=None):
    """The JSON object in the file, or None if it is missing or invalid."""
    try:
        data = json.loads(Path(path or auth_path()).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(data, path=None):
    """Atomically replace the file with mode 0600, under the lock."""
    path = Path(path or auth_path())
    with locked(path):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def remove(path=None):
    path = Path(path or auth_path())
    with locked(path):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def expires_at(record):
    value = (record or {}).get("expires_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = claims((record or {}).get("access_token") or "").get("exp")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

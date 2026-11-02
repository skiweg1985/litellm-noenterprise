"""Synthetic database boundary coordinator; never loaded by the product.

A targeted BEFORE INSERT trigger holds only a fixture user's browser-session
insert. Public HTTP tests can order callback/unlink races without production hooks.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from pydantic import BaseModel
from prisma import Prisma
import uvicorn

GATE = 38190130
DB = Prisma()
release = asyncio.Event()
entered = asyncio.Event()
holder = None


@asynccontextmanager
async def lifespan(app):
    await DB.connect()
    yield
    release.set()
    if holder:
        await holder
    await DB.disconnect()


app = FastAPI(lifespan=lifespan)


class GateRequest(BaseModel):
    user_id: str


async def hold():
    async with DB.tx(timeout=timedelta(seconds=45)) as tx:
        await tx.query_raw("SELECT pg_advisory_xact_lock($1::bigint)::text", GATE)
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=35)
        except TimeoutError:
            pass


@app.post("/arm")
async def arm(data: GateRequest):
    global holder
    release.set()
    if holder:
        await holder
    await DB.execute_raw(
        "CREATE TABLE IF NOT EXISTS oidc_test_gate (user_id text PRIMARY KEY)"
    )
    await DB.execute_raw("""CREATE OR REPLACE FUNCTION oidc_test_pause_session() RETURNS trigger AS $$
      BEGIN
        IF NEW.token LIKE 'oidc-session:%' AND EXISTS(SELECT 1 FROM oidc_test_gate WHERE user_id=NEW.user_id) THEN
          PERFORM pg_advisory_xact_lock(38190130);
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    await DB.execute_raw(
        'CREATE TRIGGER oidc_test_session_gate BEFORE INSERT ON "LiteLLM_VerificationToken" FOR EACH ROW EXECUTE FUNCTION oidc_test_pause_session()'
    )
    await DB.execute_raw("DELETE FROM oidc_test_gate")
    await DB.execute_raw("INSERT INTO oidc_test_gate(user_id) VALUES($1)", data.user_id)
    release.clear()
    entered.clear()
    holder = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=5)
    return {"armed": True}


@app.get("/status")
async def status():
    rows = await DB.query_raw(
        """SELECT bool_or(locktype='advisory' AND objid=$1::oid) AS insert_waiting,
       bool_or(NOT (locktype='advisory' AND objid=$1::oid)) AS other_waiting
       FROM pg_locks WHERE NOT granted""",
        GATE,
    )
    return {key: bool(value) for key, value in rows[0].items()}


@app.post("/release")
async def unblock():
    release.set()
    if holder:
        await holder
    await DB.execute_raw(
        'DROP TRIGGER IF EXISTS oidc_test_session_gate ON "LiteLLM_VerificationToken"'
    )
    await DB.execute_raw("DROP FUNCTION IF EXISTS oidc_test_pause_session()")
    await DB.execute_raw("DROP TABLE IF EXISTS oidc_test_gate")
    return {"released": True}


uvicorn.run(app, host="0.0.0.0", port=8093)

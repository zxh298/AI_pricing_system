"""Session store on Postgres (Cloud SQL on GCP).

A session is one investigation, owned by the user it was created for. Identity always comes from the auth
layer, never from a request body or the LLM. The store enforces:

  * ownership: someone else's session is indistinguishable from one that does not exist (NotFound)
  * expiry: after `idle_minutes` without a message, and when the pricing week has rolled over (Expired);
    expired sessions stay in the audit log (turn_transcripts)
  * one turn at a time per session: a Postgres advisory lock held for the whole turn (TurnInProgress)
  * optimistic locking: a save only succeeds if `version` is unchanged (VersionConflict)
  * atomic turns: state, history and the audit row are written in one transaction, after the turn succeeded
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from shared import db
from shared.config import load_config
from shared.diagnostic_schema import ddl, schema


class SessionError(Exception):
    status_code = 400


class NotFound(SessionError):
    status_code = 404


class Expired(SessionError):
    status_code = 410


class TurnInProgress(SessionError):
    status_code = 409


class VersionConflict(SessionError):
    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    def __init__(self, database_url: str | None = None, idle_minutes: int | None = None,
                 clock: Callable[[], datetime] = _utcnow):
        self.url, self.s, self.clock = database_url, schema(), clock
        self.idle = timedelta(minutes=idle_minutes if idle_minutes is not None else load_config().session_idle_minutes)

    def ensure_schema(self) -> None:
        with db.connect(self.url) as con:
            con.execute(ddl())

    def create(self, user: str, week: str) -> dict:
        now = self.clock()
        session_id = secrets.token_urlsafe(24)                     # server-generated, unguessable
        with db.connect(self.url) as con:
            con.execute(f"""INSERT INTO {self.s}.sessions
                (session_id, user_id, week, created_at, last_active_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (session_id, user, week, now, now, now + self.idle))
        return self.get(session_id, user)

    def get(self, session_id: str, user: str, current_week: str | None = None) -> dict:
        """The session, if it exists, is `user`'s and is still live. `current_week` enables end-of-cycle expiry."""
        with db.connect(self.url) as con:
            row = con.execute(f"SELECT * FROM {self.s}.sessions WHERE session_id = %s", (session_id,)).fetchone()
        if row is None or row["user_id"] != user:
            raise NotFound("session not found")
        if row["expires_at"] <= self.clock():
            raise Expired("session expired (idle); start a new session")
        if current_week is not None and row["week"] != current_week:
            raise Expired(f"session expired: the pricing week moved on from {row['week']}; start a new session")
        return row

    @contextmanager
    def turn_lock(self, session_id: str):
        """Hold the session's advisory lock for the whole turn, or raise TurnInProgress straight away."""
        con = db.connect(self.url)
        try:
            ok = con.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (f"{self.s}:{session_id}",)).fetchone()["ok"]
            if not ok:
                raise TurnInProgress("another message is already being processed in this session")
            yield
        finally:
            con.close()                                             # closing the connection releases the lock

    def save_turn(self, session: dict, *, state: dict, history: list[dict], question: str, answer: str,
                  tool_calls: list[dict], messages: list[dict]) -> int:
        """Persist a finished turn. Raises VersionConflict if the session changed since `session` was read.
        Returns the new version, which is also the turn number."""
        now = self.clock()
        with db.connect(self.url) as con:
            row = con.execute(f"""UPDATE {self.s}.sessions SET state = %s, history = %s, version = version + 1,
                    last_active_at = %s, expires_at = %s WHERE session_id = %s AND version = %s RETURNING version""",
                              (Jsonb(state), Jsonb(history), now, now + self.idle, session["session_id"],
                               session["version"])).fetchone()
            if row is None:
                raise VersionConflict("the session changed while this message was processed; retry")
            con.execute(f"""INSERT INTO {self.s}.turn_transcripts
                    (session_id, turn_no, user_id, question, answer, tool_calls, messages, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (session["session_id"], row["version"], session["user_id"], question, answer,
                         Jsonb(tool_calls), Jsonb(messages), now))
        return row["version"]


    def conversation(self, session_id: str) -> list[dict]:
        """[{question, answer}] for every turn so far, from the audit log (complete even after compaction)."""
        with db.connect(self.url) as con:
            rows = con.execute(f"SELECT question, answer FROM {self.s}.turn_transcripts WHERE session_id = %s "
                               "ORDER BY turn_no", (session_id,)).fetchall()
        return rows

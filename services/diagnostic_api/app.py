"""diagnostic-api: the HTTP front for the assistant.

    POST /sessions                     start an investigation (body optional: {"week": "2026-W39"})
    POST /sessions/{id}/messages       ask a question; runs one turn of the LLM loop
    GET  /sessions/{id}                the session's questions and answers
    GET  /healthz

Identity comes from the X-User header only (a stand-in for IAP: on Cloud Run the platform sets it after
authenticating). It is never read from a body, a query string or the model. What a user may see comes from
USER_REGIONS and is injected into the tools; the model cannot change it.
Run:  uvicorn services.diagnostic_api.app:app --port 8000     (reads .env from the current directory)
"""
from __future__ import annotations

import dataclasses
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.diagnostic_api.agent import run_turn
from services.diagnostic_api.llm import get_llm
from services.diagnostic_api.runs import RunResolver
from services.diagnostic_api.sessions import SessionError, SessionStore
from services.diagnostic_api.state import SessionState, compact_history
from services.diagnostic_api.tools import make_context
from shared.config import load_config
from shared.envfile import load_dotenv

log = logging.getLogger("diagnostic.api")


class NewSession(BaseModel):
    week: str | None = Field(default=None, pattern=r"^\d{4}-W\d{2}$")


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def current_user(x_user: str | None = Header(default=None)) -> str:
    if not x_user or not x_user.strip():
        raise HTTPException(401, "X-User header required")
    return x_user.strip()


def regions_for(user: str, raw: str) -> frozenset[str] | None:
    """USER_REGIONS='{"nsw-analyst": ["NSW"]}'. None = every region (users that are not listed)."""
    regions = json.loads(raw).get(user) if raw else None
    return None if regions is None else frozenset(regions)


def create_app(store: SessionStore | None = None, llm_factory=get_llm, ctx_factory=None, runs=None) -> FastAPI:
    """`llm_factory(week)`, `ctx_factory(user, allowed_regions)` and `runs` (finds the newest pipeline run of a
    week) are injectable so tests need no key, SAP or pipeline state."""
    store = store or SessionStore()
    runs = runs or RunResolver()
    owns_ctx = ctx_factory is None
    ctx_factory = ctx_factory or (lambda user, regions: make_context(user, regions))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure_schema()
        yield

    app = FastAPI(title="diagnostic-api", lifespan=lifespan)

    @app.exception_handler(SessionError)
    async def session_error(request: Request, e: SessionError):
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session(body: NewSession | None = None, user: str = Depends(current_user)):
        s = store.create(user, (body and body.week) or load_config().default_week)
        return {"session_id": s["session_id"], "week": s["week"], "expires_at": s["expires_at"]}

    @app.get("/sessions/{session_id}")
    def read_session(session_id: str, user: str = Depends(current_user)):
        s = store.get(session_id, user, load_config().default_week)
        groups = {h: {"skus": len(g["skus"]), "label": g["label"], "week": g["week"], "regions": g["regions"]}
                  for h, g in SessionState(s["state"]).groups.items()}
        return {"session_id": s["session_id"], "week": s["week"], "turns": s["version"],
                "created_at": s["created_at"], "last_active_at": s["last_active_at"], "expires_at": s["expires_at"],
                "groups": groups, "conversation": store.conversation(session_id)}

    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, body: NewMessage, user: str = Depends(current_user)):
        cfg = load_config()
        store.get(session_id, user, cfg.default_week)                        # 404 / 410 before taking the lock
        with store.turn_lock(session_id):
            s = store.get(session_id, user, cfg.default_week)                # latest version, now that we hold the lock
            state = SessionState(s["state"])
            history, dropped = compact_history(s["history"])                 # older turns: question + answer only
            state.note_dropped(dropped)
            base = ctx_factory(user, regions_for(user, cfg.user_regions))
            ctx = dataclasses.replace(base, state=state, runs=runs)          # a copy for this turn; `base` may be shared
            try:
                out = run_turn(body.text, ctx, llm_factory(s["week"]), s["week"], history=history,
                               state_block=state.render())
            except Exception:                                                # noqa: BLE001 - nothing was saved
                log.exception("turn failed in session %s", session_id)
                raise HTTPException(502, "the assistant is unavailable; nothing was saved") from None
            finally:
                if owns_ctx:
                    base.sap.close()
            turn = store.save_turn(s, state=state.to_json(), history=out["messages"], question=body.text,
                                   answer=out["answer"], tool_calls=out["tool_calls"],
                                   messages=out["messages"][len(history):])
        return {"turn": turn, "answer": out["answer"], "tool_calls": out["tool_calls"], "steps": out["steps"],
                "escalated": out["escalated"], "truncated": out["truncated"]}

    return app


def __getattr__(name: str):
    """`uvicorn services.diagnostic_api.app:app` builds the app on first use, after reading .env for local
    development. Importing this module (tests, tooling) therefore has no side effects on the environment."""
    if name == "app":
        load_dotenv()
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)

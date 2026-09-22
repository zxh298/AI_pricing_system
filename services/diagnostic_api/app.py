"""diagnostic-api: the HTTP front for the assistant.

    POST /sessions                     start an investigation (body optional: {"week": "2026-W39"})
    POST /sessions/{id}/messages       ask a question; runs one turn of the LLM loop
    GET  /sessions/{id}                the session's questions and answers
    GET  /findings                     open issues a person confirmed (only regions the user may see)
    POST /findings                     record a confirmed issue                   (role: analyst)
    POST /findings/{id}/resolve        close a finding                            (role: analyst)
    GET  /healthz

Identity comes from the X-User header only (a stand-in for IAP: on Cloud Run the platform sets it after
authenticating). It is never read from a body, a query string or the model. What a user may see comes from
USER_REGIONS and is injected into the tools; the model cannot change it. Writing findings needs X-Role: analyst
(a stand-in for a group check on GCP); the model has no tool that writes them.
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
from services.diagnostic_api.cache import ToolCache
from services.diagnostic_api.findings import FindingsStore, render_block
from services.diagnostic_api.knowledge import KnowledgeBase
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


class NewFinding(BaseModel):
    week: str = Field(pattern=r"^\d{4}-W\d{2}$")
    region: str = Field(min_length=1, max_length=20)
    error_code: str = Field(min_length=1, max_length=40)
    reason: str = Field(default="", max_length=80)
    brand: str | None = Field(default=None, max_length=80)
    skus: list[str] = Field(min_length=1, max_length=500)
    root_cause: str = Field(min_length=1, max_length=1000)
    owner: str | None = Field(default=None, max_length=80)
    ticket: str | None = Field(default=None, max_length=80)
    evidence: dict | None = None


def current_user(x_user: str | None = Header(default=None)) -> str:
    if not x_user or not x_user.strip():
        raise HTTPException(401, "X-User header required")
    return x_user.strip()


def analyst(user: str = Depends(current_user), x_role: str | None = Header(default=None)) -> str:
    if x_role != "analyst":
        raise HTTPException(403, "analyst role required")
    return user


def regions_for(user: str, raw: str) -> frozenset[str] | None:
    """USER_REGIONS='{"nsw-analyst": ["NSW"]}'. None = every region (users that are not listed)."""
    regions = json.loads(raw).get(user) if raw else None
    return None if regions is None else frozenset(regions)


def create_app(store: SessionStore | None = None, llm_factory=get_llm, ctx_factory=None, runs=None, cache=None,
               findings=None, kb=None, warm_knowledge: bool = False) -> FastAPI:
    """`llm_factory(week)`, `ctx_factory(user, allowed_regions)`, `runs` (finds the newest pipeline run of a week),
    `cache`, `findings` and `kb` (the knowledge base) are injectable so tests need no key, SAP, pipeline state or
    embedding model. `warm_knowledge` loads the embedding model at startup, so the first question does not wait for it."""
    store = store or SessionStore()
    runs = runs or RunResolver()
    cache = cache or ToolCache()
    findings = findings or FindingsStore()
    kb = kb or KnowledgeBase()
    owns_ctx = ctx_factory is None
    ctx_factory = ctx_factory or (lambda user, regions: make_context(user, regions))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure_schema()
        if warm_knowledge:
            try:
                kb.warm()
            except Exception:                                                # noqa: BLE001 - search_docs will report it
                log.warning("could not load the embedding model at startup", exc_info=True)
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
            allowed = regions_for(user, cfg.user_regions)
            base = ctx_factory(user, allowed)
            known = findings.list(week=s["week"], status="open", allowed_regions=allowed)   # shown to the model by us
            ctx = dataclasses.replace(base, state=state, runs=runs, cache=cache, findings=findings, kb=kb)   # a copy for this turn
            try:
                out = run_turn(body.text, ctx, llm_factory(s["week"]), s["week"], history=history,
                               state_block="\n\n".join(b for b in (state.render(), render_block(known)) if b))
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

    def visible(finding: dict, user: str) -> bool:
        allowed = regions_for(user, load_config().user_regions)
        return allowed is None or finding["region"] in allowed

    @app.get("/findings")
    def list_findings(week: str | None = None, status: str = "open", user: str = Depends(current_user)):
        """status: open (default), resolved or all."""
        return findings.list(week=week, status=None if status == "all" else status,
                             allowed_regions=regions_for(user, load_config().user_regions))

    @app.post("/findings", status_code=201)
    def create_finding(body: NewFinding, user: str = Depends(analyst)):
        allowed = regions_for(user, load_config().user_regions)
        if allowed is not None and body.region not in allowed:
            raise HTTPException(403, "region not permitted")
        return findings.create(confirmed_by=user, **body.model_dump())

    @app.post("/findings/{finding_id}/resolve")
    def resolve_finding(finding_id: int, user: str = Depends(analyst)):
        f = findings.get(finding_id)
        if f is None or not visible(f, user):
            raise HTTPException(404, "finding not found")
        closed = findings.resolve(finding_id, user)
        if closed is None:
            raise HTTPException(409, "finding is already resolved")
        return closed

    return app


def __getattr__(name: str):
    """`uvicorn services.diagnostic_api.app:app` builds the app on first use, after reading .env for local
    development. Importing this module (tests, tooling) therefore has no side effects on the environment."""
    if name == "app":
        load_dotenv()
        globals()["app"] = create_app(warm_knowledge=True)
        return globals()["app"]
    raise AttributeError(name)

"""All configuration comes from environment variables. Code never branches on local vs cloud."""
import os
from dataclasses import dataclass
from datetime import date


def current_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


@dataclass(frozen=True)
class Config:
    warehouse: str
    duckdb_path: str
    bigquery_dataset: str
    database_url: str
    sap_base_url: str
    auth_mode: str
    llm_provider: str
    agent_mode: str
    sap_read_key: str
    sap_write_key: str
    anthropic_model: str = "claude-haiku-4-5"     # the API key itself is read by the SDK from ANTHROPIC_API_KEY
    default_week: str = ""                        # what "this week" means to the assistant
    session_idle_minutes: int = 120               # a session expires after this long without a message
    user_regions: str = ""                        # JSON {"user": ["REGION", ...]}; users not listed see every region
    tool_cache_ttl_seconds: int = 300             # how long a live (SAP) tool result may be reused
    embedding_provider: str = "fastembed"         # fastembed | hash (hash is for tests: it does not understand meaning)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ""                 # where the model is stored; default ~/.cache/ai_pricing_fastembed
    search_min_score: float = 0.67                # documents less similar than this are not returned by similarity search
                                                   # (set from evals/retrieval.py: separates on-topic from off-topic at 0.67)
    diagnostic_api_url: str = "http://localhost:8000"   # where the ui reaches diagnostic-api
    triage_model: str = "claude-sonnet-5-5"       # the weekly triage's investigation agent; chat keeps ANTHROPIC_MODEL
    triage_response_hours: int = 24               # how long an owner has to decide before a case escalates
    triage_max_investigations: int = 5            # LLM budget per scan: agent runs + unclassified summaries
    triage_max_steps: int = 10                    # model calls per investigation
    triage_breaker_share: float = 0.5             # this share of records failing at once = one incident, no LLM calls
    triage_drop_ratio: float = 0.6                # LOW_SELL_THROUGH: units at or below this share of the baseline
    triage_min_baseline: int = 20                 # ... and only for records selling at least this many units a week
    triage_decision_owner: str = "Clearance Business Lead"   # who decides when an owner refuses or misses the deadline


def load_config() -> Config:
    e = os.environ.get
    return Config(
        warehouse=e("WAREHOUSE", "duckdb"),
        duckdb_path=e("DUCKDB_PATH", "data/warehouse.duckdb"),
        bigquery_dataset=e("BIGQUERY_DATASET", ""),
        database_url=e("DATABASE_URL", "postgresql://pricing:pricing@localhost:5432/pricing"),
        sap_base_url=e("SAP_BASE_URL", "http://localhost:8001"),
        auth_mode=e("AUTH_MODE", "none"),
        llm_provider=e("LLM_PROVIDER", "scripted"),
        agent_mode=e("AGENT_MODE", "router"),          # router: classify -> fixed workflow -> explain | loop: open tool use
        sap_read_key=e("SAP_READ_KEY", "dev-read-key"),
        sap_write_key=e("SAP_WRITE_KEY", "dev-write-key"),
        anthropic_model=e("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        default_week=e("DEFAULT_WEEK") or current_week(),
        session_idle_minutes=int(e("SESSION_IDLE_MINUTES", "120")),
        user_regions=e("USER_REGIONS", ""),
        tool_cache_ttl_seconds=int(e("TOOL_CACHE_TTL_SECONDS", "300")),
        embedding_provider=e("EMBEDDING_PROVIDER", "fastembed"),
        embedding_model=e("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        embedding_cache_dir=e("EMBEDDING_CACHE_DIR", ""),
        search_min_score=float(e("SEARCH_MIN_SCORE", "0.67")),
        diagnostic_api_url=e("DIAGNOSTIC_API_URL", "http://localhost:8000"),
        triage_model=e("TRIAGE_MODEL", "claude-sonnet-5-5"),
        triage_response_hours=int(e("TRIAGE_RESPONSE_HOURS", "24")),
        triage_max_investigations=int(e("TRIAGE_MAX_INVESTIGATIONS", "5")),
        triage_max_steps=int(e("TRIAGE_MAX_STEPS", "10")),
        triage_breaker_share=float(e("TRIAGE_BREAKER_SHARE", "0.5")),
        triage_drop_ratio=float(e("TRIAGE_DROP_RATIO", "0.6")),
        triage_min_baseline=int(e("TRIAGE_MIN_BASELINE", "20")),
        triage_decision_owner=e("TRIAGE_DECISION_OWNER", "Clearance Business Lead"),
    )

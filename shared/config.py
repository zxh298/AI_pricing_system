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
    search_min_score: float = 0.65                # documents less similar than this are not returned by similarity search


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
        search_min_score=float(e("SEARCH_MIN_SCORE", "0.65")),
    )

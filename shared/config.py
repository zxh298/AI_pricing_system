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
    )

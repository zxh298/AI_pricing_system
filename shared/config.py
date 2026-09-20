"""All configuration comes from environment variables. Code never branches on local vs cloud."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    warehouse: str
    duckdb_path: str
    bigquery_dataset: str
    database_url: str
    sap_base_url: str
    auth_mode: str
    llm_provider: str


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
    )

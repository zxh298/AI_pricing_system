"""WarehouseClient: the only place that touches the analytical store.

DuckDB locally, BigQuery on GCP. All queries are named, parameterised methods, so the
diagnostic tools never build SQL and both backends can be tested with the same fixtures.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import duckdb

from shared.config import Config, load_config


class WarehouseClient(ABC):
    @abstractmethod
    def get_candidates(self, week: str) -> list[dict]: ...

    @abstractmethod
    def get_rules(self, week: str) -> list[dict]: ...

    @abstractmethod
    def get_sales_history(self, skus: list[str], region: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_price_history(self, skus: list[str], region: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_products(self, skus: list[str]) -> list[dict]: ...

    @abstractmethod
    def get_inventory(self, skus: list[str], week: str) -> list[dict]: ...


class DuckDBWarehouse(WarehouseClient):
    def __init__(self, path: str, read_only: bool = True):
        self.path, self.read_only = path, read_only

    def _rows(self, sql: str, params: list | None = None) -> list[dict]:
        # Short-lived connection per call (DuckDB allows one writer process).
        con = duckdb.connect(self.path, read_only=self.read_only)
        try:
            cur = con.execute(sql, params or [])
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()

    def get_candidates(self, week):
        return self._rows(
            "SELECT c.week, c.sku, c.pack_type, c.pack_qty, p.brand, p.category, p.name,"
            " c.shelf_price, c.week_no, p.cost, c.current_price, c.region"
            " FROM clearance_candidates c JOIN products p USING (sku, pack_qty) WHERE c.week = ?"
            " ORDER BY c.sku, c.pack_qty, c.region", [week])

    def get_rules(self, week):
        return self._rows(
            "SELECT * FROM business_rules WHERE week = ? ORDER BY sku, pack_qty", [week])

    def get_sales_history(self, skus, region=None):
        if not skus:
            return []
        marks = ",".join("?" * len(skus))
        sql = (f"SELECT sale_date, sku, pack_type, pack_qty, region, SUM(units) AS units,"
               f" AVG(price) AS avg_price"
               f" FROM sales WHERE sku IN ({marks})")
        params: list = list(skus)
        if region:
            sql += " AND region = ?"
            params.append(region)
        return self._rows(
            sql + " GROUP BY sale_date, sku, pack_type, pack_qty, region ORDER BY sale_date", params)

    def get_price_history(self, skus, region=None):
        if not skus:
            return []
        marks = ",".join("?" * len(skus))
        sql = f"SELECT * FROM price_history WHERE sku IN ({marks})"
        params: list = list(skus)
        if region:
            sql += " AND region = ?"
            params.append(region)
        return self._rows(sql + " ORDER BY sku, pack_qty, region, week", params)

    def get_products(self, skus):
        if not skus:
            return []
        marks = ",".join("?" * len(skus))
        return self._rows(f"SELECT * FROM products WHERE sku IN ({marks}) ORDER BY sku, pack_qty", list(skus))

    def get_inventory(self, skus, week):
        if not skus:
            return []
        marks = ",".join("?" * len(skus))
        return self._rows(
            f"SELECT * FROM inventory WHERE week = ? AND sku IN ({marks})"
            f" ORDER BY sku, pack_qty, region",
            [week, *skus])


class BigQueryWarehouse(WarehouseClient):
    """Placeholder: implement with parameterised google-cloud-bigquery queries at deploy time.
    Same method names and row shapes as DuckDBWarehouse."""

    def __init__(self, dataset: str):
        self.dataset = dataset

    def _todo(self, *a, **k):
        raise NotImplementedError("BigQueryWarehouse is implemented in the GCP deployment step")

    get_candidates = get_rules = get_sales_history = get_inventory = _todo
    get_price_history = get_products = _todo


def get_warehouse(cfg: Config | None = None, read_only: bool = True) -> WarehouseClient:
    cfg = cfg or load_config()
    if cfg.warehouse == "duckdb":
        return DuckDBWarehouse(cfg.duckdb_path, read_only=read_only)
    if cfg.warehouse == "bigquery":
        return BigQueryWarehouse(cfg.bigquery_dataset)
    raise ValueError(f"unknown WAREHOUSE={cfg.warehouse}")

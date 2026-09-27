"""Databricks lakehouse: the system of record for parsed price files.

Parsed sources (parsers/out/<source_id>/*.csv.gz) live in a Unity Catalog Volume and as Delta tables;
local Postgres is the serving copy the app queries. push.py uploads, sync.py pulls new sources into Postgres.

    DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID   required (in .env)
    DATABRICKS_CATALOG   default 'workspace' (Free Edition's default catalog)
    DATABRICKS_SCHEMA    default 'happybill'
"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CATALOG = os.environ.get("DATABRICKS_CATALOG", "workspace")
SCHEMA = os.environ.get("DATABRICKS_SCHEMA", "happybill")
VOLUME = f"/Volumes/{CATALOG}/{SCHEMA}/sources"


def client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()  # reads DATABRICKS_HOST / DATABRICKS_TOKEN


def sql(w, statement):
    """Run a statement on the SQL warehouse and wait for it to finish."""
    from databricks.sdk.service.sql import StatementState
    r = w.statement_execution.execute_statement(
        warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"], statement=statement, wait_timeout="30s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{statement[:80]}...: {r.status.error.message if r.status.error else r.status.state}")
    return r

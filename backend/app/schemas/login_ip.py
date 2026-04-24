"""Pydantic models for Login IP Monitor API.

Naming convention follows the rest of the platform (see ib_financial.py,
client_return_rate.py). All timestamps are serialized as HKT strings so
the frontend renders them without any timezone juggling.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Watchlist (monitored_accounts)
# ---------------------------------------------------------------------------

# Whitelisted server names — the FTP service only knows these three.
# Keeping it Literal means Pydantic rejects typos like "MT4live" at the edge,
# before we try to look up non-existent credentials in .env.
ServerName = Literal["MT4_Live", "MT5", "MT4_Live2"]


class MonitoredAccountOut(BaseModel):
    """One row from `monitored_accounts`, shaped for the AG-Grid table."""

    id: int
    account_id: int
    server_name: str
    remarks: Optional[str] = None


class MonitoredAccountBatchCreate(BaseModel):
    """Batch-add payload: multiple accounts share the same server + remarks.

    Frontend `Tab 2` lets the user paste a newline-separated list of account
    ids into a textarea. We keep the list as `list[int]` (not a comma-string)
    so schema validation rejects non-numeric tokens cleanly.
    """

    account_ids: List[int] = Field(..., min_length=1, max_length=500)
    server_name: ServerName
    remarks: Optional[str] = None


class MonitoredAccountUpdate(BaseModel):
    """PATCH body — only `remarks` is editable; the account_id/server_name
    identity is immutable (drop + re-add if you need to change them)."""

    remarks: Optional[str] = None


# ---------------------------------------------------------------------------
# Report (daily correlation analysis)
# ---------------------------------------------------------------------------


class ReportCorrelationEntry(BaseModel):
    """One correlated (non-monitored) account tied to a monitored account
    through a shared IP, annotated with the historical date the IP was last
    seen for the monitored account."""

    id: str  # correlated account id, as string (matches legacy JSON shape)
    historical_date: Optional[str] = None  # YYYYMMDD or None
    chinese_name: Optional[str] = None  # enriched via MySQL (may be absent)


class ReportIPSharedAnalysis(BaseModel):
    ip: str
    correlated_accounts: List[ReportCorrelationEntry]


class ReportAccountItem(BaseModel):
    """One card in Tab 1 — per monitored account per day."""

    account_id: str
    remarks: Optional[str] = None
    server_name: str
    logged_in: bool
    total_logins: int = 0
    used_ips: List[str] = []
    logins_by_ip: dict[str, int] = {}
    shared_ips_analysis: List[ReportIPSharedAnalysis] = []


class ReportResponse(BaseModel):
    target_date: str
    generated_at: str  # HKT timestamp
    monitored_count: int
    correlated_count: int
    any_correlation: bool
    accounts: List[ReportAccountItem]


class AvailableDatesResponse(BaseModel):
    dates: List[str]  # sorted desc, YYYYMMDD


# ---------------------------------------------------------------------------
# Manual search (Tab 3)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    # 'account_id' applies the same-client_id filter; 'ip_address' doesn't.
    search_type: Literal["account_id", "ip_address"]
    # Already parsed into a list by the frontend (textarea supports
    # comma/space/newline separators; parsing is client-side).
    terms: List[str] = Field(..., min_length=1, max_length=200)
    days: int = Field(7, ge=1, le=30)


class SearchResultAccountRow(BaseModel):
    """Per-match row when searching by account id."""

    kind: Literal["account_id"] = "account_id"
    search_term: str
    search_term_chinese_name: Optional[str] = None
    client_id: Optional[str] = None
    date: str
    server: str
    login_ip: str
    login_count: int
    # Already-filtered: accounts with same client_id as search_term are excluded.
    # Rendered string, may include Chinese name: "12345 (张三)".
    correlated_accounts: List[str] = []


class SearchResultIPRow(BaseModel):
    """Per-match row when searching by IP."""

    kind: Literal["ip_address"] = "ip_address"
    search_term: str
    date: str
    server: str
    correlated_accounts: List[str] = []  # raw account ids


class SearchResponse(BaseModel):
    # Either a populated list or an explanatory message (not_found / error).
    results: List[dict[str, Any]] = []
    error: Optional[str] = None
    not_found: Optional[str] = None


# ---------------------------------------------------------------------------
# Mail recipients
# ---------------------------------------------------------------------------


class MailRecipientOut(BaseModel):
    id: int
    email: str
    role: Literal["to", "cc"]
    is_active: int
    remarks: Optional[str] = None
    created_at: str


class MailRecipientCreate(BaseModel):
    email: str
    role: Literal["to", "cc"] = "to"
    remarks: Optional[str] = None


# ---------------------------------------------------------------------------
# Scheduler runs + manual triggers
# ---------------------------------------------------------------------------


class SchedulerRunOut(BaseModel):
    id: int
    job_name: str
    target_date: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    status: str
    summary_json: Optional[str] = None
    error_msg: Optional[str] = None


class SchedulerRunsResponse(BaseModel):
    runs: List[SchedulerRunOut]


class SchedulerRunNowRequest(BaseModel):
    # Only these two jobs are exposed; the daily triggers are the only
    # things worth running on demand.
    job: Literal["download", "analyze_report"]
    target_date: Optional[str] = None  # YYYYMMDD; defaults to HKT yesterday


# ---------------------------------------------------------------------------
# CSV export (Tab 3 search results)
# ---------------------------------------------------------------------------


class ExportTaskCreateRequest(BaseModel):
    # Same shape as SearchRequest — the exporter re-runs the search with
    # identical params and writes the result to CSV.
    search_type: Literal["account_id", "ip_address"]
    terms: List[str] = Field(..., min_length=1, max_length=200)
    days: int = Field(7, ge=1, le=30)


class ExportTaskCreateResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"
    created_at: str


class ExportTaskStatusResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "succeeded", "failed", "expired"]
    progress: int = 0
    row_count: Optional[int] = None
    error_message: Optional[str] = None
    download_url: Optional[str] = None
    created_at: str
    finished_at: Optional[str] = None

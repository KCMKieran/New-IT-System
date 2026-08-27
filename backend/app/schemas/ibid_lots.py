from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Server sid → display name, same mapping as the legacy :8088 tool.
SERVER_NAMES = {"1": "MT4Live1", "5": "MT5", "6": "MT4Live2"}

# The 37 majors/crosses the desk asks for by default. `symbol_mode="all"`
# drops the SYMBOL filter entirely, which — since we read raw mt4_trades —
# naturally includes stocks ("#"-prefixed symbols).
DEFAULT_SYMBOLS: List[str] = [
    "XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "AUDCAD", "AUDJPY", "AUDCHF", "AUDNZD", "CADCHF", "CADJPY", "CHFJPY", "EURAUD",
    "EURCAD", "EURCHF", "EURGBP", "EURJPY", "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF",
    "GBPJPY", "GBPNZD", "NZDCAD", "NZDCHF", "NZDJPY", "EURHUF", "EURPLN", "EURTRY",
    "USDCNH", "USDHUF", "USDNOK", "USDSGD", "USDTHB",
]

# Label used in the response when no SYMBOL filter is applied.
ALL_SYMBOLS_LABEL = "所有产品(含股票)"

# Guard rail absent from the legacy tool: an unbounded range over mt4_trades
# (48M rows) turns into a table-scan-class query. One year plus a day is
# enough for every reported use case.
MAX_RANGE_DAYS = 366

_DATE_FORMAT = "%Y-%m-%d"


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, _DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format") from exc


class IbidLotsQueryRequest(BaseModel):
    """One "For Tobe Global" lots query.

    All five sub-modes read raw mt4_trades; `login` addresses a single trading
    account directly, the other four resolve a set of CRM user ids first.

    `ibid_direct` and `ibid_direct_client` share the same level-0 scope and
    differ only in whether direct referrals that are themselves IBs stay in:
    `ibid_direct` keeps them, `ibid_direct_client` drops them and leaves the
    IB itself plus its plain (non-IB) clients.
    """

    query_type: Literal["ibid", "ibid_direct", "ibid_direct_client", "id", "login"]
    target_id: str = Field(..., min_length=1, max_length=32)
    server_sid: Optional[str] = Field(
        default=None, description='"1"|"5"|"6" — required when query_type == "login"'
    )
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    symbol_mode: Literal["default", "all", "custom"] = "default"
    custom_symbols: Optional[List[str]] = Field(
        default=None, description='Only used when symbol_mode == "custom"; empty falls back to the default 37'
    )

    @field_validator("target_id")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        # Both ibId/referralId and MT LOGIN are numeric — anything else is a
        # typo, and letting it through would just be a slow miss.
        v = v.strip()
        if not v.isdigit():
            raise ValueError("target_id must contain digits only")
        return v

    @model_validator(mode="after")
    def _check_server_and_range(self) -> "IbidLotsQueryRequest":
        if self.query_type == "login" and self.server_sid not in SERVER_NAMES:
            raise ValueError("server_sid must be one of '1' (MT4Live1), '5' (MT5), '6' (MT4Live2) for login queries")

        start = _parse_date(self.start_date, "start_date")
        end = _parse_date(self.end_date, "end_date")
        if start > end:
            raise ValueError("start_date must not be later than end_date")
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValueError(f"date range must not exceed {MAX_RANGE_DAYS} days")
        return self

    def resolved_symbols(self) -> Optional[List[str]]:
        """SYMBOL filter to apply, or None for "no filter" (all products)."""
        if self.symbol_mode == "all":
            return None
        if self.symbol_mode == "custom":
            cleaned = [s.strip() for s in (self.custom_symbols or []) if s.strip()]
            return cleaned or list(DEFAULT_SYMBOLS)
        return list(DEFAULT_SYMBOLS)


class IbidLotsSymbolStat(BaseModel):
    """Per-product totals, CEN-normalized, sorted by total_lots desc.

    Hold-time comes in two overlapping shapes: the legacy two-way split
    (`lots_above_10s` / `lots_below_10s`) and the three mutually exclusive
    buckets the UI shows (`lots_below_10s` / `lots_10s_to_3min` /
    `lots_above_3min`), which sum to `total_lots`.
    """

    symbol: str
    total_lots: float
    lots_above_10s: float
    lots_below_10s: float
    lots_10s_to_3min: float = 0.0
    lots_above_3min: float = 0.0


class IbidLotsUserStat(BaseModel):
    """Per-client totals. `user_id` is a string — in `login` mode it holds the
    loginSid itself (e.g. "1-8001234") rather than a CRM user id."""

    user_id: str
    total_lots: float
    lots_above_10s: float
    lots_below_10s: float
    lots_10s_to_3min: float = 0.0
    lots_above_3min: float = 0.0
    total_tickets: int
    cen: bool  # true when any of the client's accounts is a CEN account (lots already ÷100)


class IbidLotsQueryResponse(BaseModel):
    query_target: str
    start_date: str
    end_date: str
    symbols: List[str]
    account_count: int
    total_volume: float
    total_above_10s: float
    total_below_10s: float
    total_10s_to_3min: float = 0.0
    total_above_3min: float = 0.0
    total_tickets: int
    symbol_stats: List[IbidLotsSymbolStat] = Field(default_factory=list)
    user_stats: List[IbidLotsUserStat] = Field(default_factory=list)
    # How many direct referrals were dropped for being sub-IBs. Only ever
    # non-zero in "ibid_direct_client" mode; the UI shows it so a total that
    # is smaller than the plain "direct only" one is self-explanatory rather
    # than looking like data loss.
    excluded_sub_ib_users: int = 0
    # True when the row-level (country) data scope actually REMOVED downline
    # clients from this answer — so the frontend can say so. The numbers above
    # are then legitimately smaller than the ones an unrestricted colleague
    # sees for the same IB, and a silently smaller total is the failure mode
    # this flag exists to prevent. False for every unrestricted caller, and
    # false for a restricted one whose whole downline was in scope anyway.
    # NOT Optional and NOT nested: the frontend keys its notice off exactly
    # this name on all three of ib-tree / ibid-lots / ib-data.
    data_scope_filtered: bool = False
    query_time_ms: float = 0.0

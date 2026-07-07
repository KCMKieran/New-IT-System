from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel


class IBTreeNode(BaseModel):
    """One link in the displayed chain (sales head, IB, or the client)."""

    user_id: Optional[int] = None  # None for a sales head resolved from custom_sales_belong
    display_name: str              # Chinese name, falling back to pinyin
    english_name: str
    role: Literal["sales", "ib", "client"]
    is_staff: bool = False


class IBTreeResponse(BaseModel):
    client_id: int
    sales_code: Optional[str] = None
    chain_text: str                # ready-to-paste line, e.g. "HZL013 > 122830 刘坤林 > ..."
    nodes: List[IBTreeNode]
    query_time_ms: float = 0.0

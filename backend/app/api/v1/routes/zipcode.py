from __future__ import annotations

import logging

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

from app.services.zipcode_service import (
	get_zipcode_distribution,
	get_zipcode_changes,
	get_exclusions,
	add_manual_exclusion,
	get_change_frequency,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/zipcode", tags=["zipcode"])


class ManualExclusionCreate(BaseModel):
	client_id: int = Field(..., ge=1, description="Client identifier")
	note: str = Field(..., min_length=1, max_length=500, description="Manual note for audit trail")


@router.get("/distribution")
def zipcode_distribution():
	try:
		rows = get_zipcode_distribution()
		# Shape: [{ zipcode: str, client_count: int, client_ids?: List[int] }]
		return {"ok": True, "data": rows, "rows": len(rows)}
	except Exception as e:
		return {"ok": False, "data": [], "rows": 0, "error": str(e)}


@router.get("/changes")
def zipcode_changes(
	start: str | None = Query(default=None, description="Start time, e.g. '2025-01-01 00:00:00' (timestamptz)"),
	end: str | None = Query(default=None, description="End time, e.g. '2025-01-31 23:59:59' (timestamptz)"),
	client_id: int | None = Query(default=None, ge=1, description="Filter by client ID"),
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=50, ge=1, le=1000),
):
	try:
		result = get_zipcode_changes(start=start, end=end, client_id=client_id, page=page, page_size=page_size)
		return {"ok": True, **result}
	except Exception as e:
		return {"ok": False, "rows": 0, "page": page, "page_size": page_size, "data": [], "error": str(e)}


@router.get("/exclusions")
def zipcode_exclusions(
	is_active: bool | None = Query(default=None, description="Filter by active flag"),
	limit: int = Query(default=100, ge=1, le=10000, description="Limit rows returned"),
):
	try:
		rows = get_exclusions(is_active=is_active, limit=limit)
		return {"ok": True, "rows": len(rows), "data": rows}
	except Exception as e:
		return {"ok": False, "rows": 0, "data": [], "error": str(e)}


@router.post("/exclusions")
def create_zipcode_exclusion(payload: ManualExclusionCreate):
	note = payload.note.strip()
	if not note:
		raise HTTPException(status_code=400, detail="Reason must not be empty")
	try:
		row = add_manual_exclusion(client_id=payload.client_id, note=note, added_by="WebUser")
		return {"ok": True, "data": row}
	except Exception as e:
		logger.exception("zipcode manual exclusion insert failed")
		raise HTTPException(status_code=500, detail="internal error while adding exclusion") from e


@router.get("/change-frequency")
def zipcode_change_frequency(
	window_days: int = Query(default=30, ge=1, le=365, description="Window in days"),
	page: int = Query(default=1, ge=1),
	page_size: int = Query(default=50, ge=1, le=1000),
):
	try:
		result = get_change_frequency(window_days=window_days, page=page, page_size=page_size)
		return {"ok": True, **result}
	except Exception as e:
		return {
			"ok": False,
			"rows": 0,
			"page": page,
			"page_size": page_size,
			"window_days": window_days,
			"data": [],
			"error": str(e),
		}



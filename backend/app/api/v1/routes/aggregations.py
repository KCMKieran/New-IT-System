from fastapi import APIRouter, Depends

from ....core.config import Settings, get_settings
from ....schemas.aggregation import AggregateRequest, AggregateResponse
from ....services.aggregation_service import aggregate_to_json


router = APIRouter(prefix="/aggregate")


@router.post("/to-json", response_model=AggregateResponse)
def post_aggregate_to_json(
    req: AggregateRequest,
    settings: Settings = Depends(get_settings),
):
    result = aggregate_to_json(settings, req.symbol, req.start, req.end)
    return result



from fastapi import APIRouter

from .routes.health import router as health_router
from .routes.aggregations import router as aggregations_router


api_v1_router = APIRouter()
api_v1_router.include_router(health_router, tags=["health"])
api_v1_router.include_router(aggregations_router, tags=["aggregations"]) 



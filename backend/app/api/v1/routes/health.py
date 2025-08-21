from fastapi import APIRouter


router = APIRouter(prefix="/health")


@router.get("")
def get_health():
    """
    Simple health endpoint for readiness/liveness checks.
    """
    return {"status": "ok"}



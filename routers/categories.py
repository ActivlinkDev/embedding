from fastapi import APIRouter, Depends
from utils.common import device_categories
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/categories",
    tags=["Categories"]
)

@router.get("/")
def list_categories(_: None = Depends(verify_token)):
    return {"categories": list(device_categories)}

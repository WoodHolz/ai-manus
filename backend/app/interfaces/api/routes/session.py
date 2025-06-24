from fastapi import APIRouter, Depends
from app.interfaces.schemas.request import StartSessionRequest
from app.interfaces.schemas.response import SessionResponse
from app.application.services.session_service import SessionService, get_session_service

router = APIRouter()

@router.post("/start", response_model=SessionResponse)
async def start_session(
    request: StartSessionRequest,
    session_service: SessionService = Depends(get_session_service)
):
    result = await session_service.create_and_run_session(request.url, request.cookies)
    return SessionResponse(
        id=result.get("session_id", "unknown"),
        success=result.get("status") == "completed",
        data=result,
        debug_screenshot_base64=result.get("debug_screenshot_base64"),
        debug_html=result.get("debug_html")
    ) 
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.session import require_admin
from app.csrf import generate_csrf_token

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: dict = Depends(require_admin),
):
    return request.app.state.templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "csrf_token": generate_csrf_token(),
            "session": session,
            "get_flashed_messages": lambda: [],
        },
    )

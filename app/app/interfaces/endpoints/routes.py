from fastapi import APIRouter, Depends

from app.interfaces.endpoints.auth_routes import router as auth_router
from app.interfaces.endpoints.info_routes import router as info_router
from app.interfaces.middleware.auth import require_info_admin

router = APIRouter()

router.include_router(auth_router)
router.include_router(info_router, dependencies=[Depends(require_info_admin)])

# /internal/tasks was an unauthenticated diagnostic endpoint. It remains
# unavailable until P0-005C installs service-token authentication.

# 在此注册其他业务模块路由
# from app.interfaces.endpoints.user_routes import router as user_router
# router.include_router(user_router)

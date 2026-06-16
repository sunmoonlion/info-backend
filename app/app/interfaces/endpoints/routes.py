from fastapi import APIRouter

from app.interfaces.endpoints.auth_routes import router as auth_router
from app.interfaces.endpoints.tasks_routes import router as tasks_router

router = APIRouter()

router.include_router(auth_router)
router.include_router(tasks_router)

# 在此注册其他业务模块路由
# from app.interfaces.endpoints.user_routes import router as user_router
# router.include_router(user_router)

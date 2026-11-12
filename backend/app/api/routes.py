"""核心 API 路由装配；各端点按业务职责放在 routes_* 模块中。"""

from fastapi import APIRouter

from .routes_auth import router as auth_router
from .routes_cluster import router as cluster_router
from .routes_jobs import router as jobs_router
from .routes_maintenance import router as maintenance_router
from .routes_system import router as system_router
from .routes_users import router as users_router

router = APIRouter(prefix="/api/v1")
router.include_router(system_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(jobs_router)
router.include_router(maintenance_router)
router.include_router(cluster_router)

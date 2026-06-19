"""Admin developer-portal API (`/v1/admin/*`).

A single subtree gathering every admin-only surface — metrics, user
management, jobs, applications, and blog. Admin authorization is enforced
once here (``require_admin``) for the whole subtree; individual handlers
re-declare it only when they need the acting user (for audit logging).
"""

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_admin
from app.routers.admin import applications, blog, jobs, metrics, users

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
admin_router.include_router(metrics.router)
admin_router.include_router(users.router)
admin_router.include_router(jobs.router)
admin_router.include_router(applications.router)
admin_router.include_router(blog.router)

__all__ = ["admin_router"]

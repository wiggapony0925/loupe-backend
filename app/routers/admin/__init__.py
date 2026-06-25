"""Admin developer-portal API (`/v1/admin/*`).

A single subtree gathering every admin-only surface — operations (health,
database, cloud, audit), metrics, user management, jobs, applications, blog,
and the scanner waitlist. Admin authorization is enforced once here
(``require_admin``) for the whole subtree; individual handlers re-declare it
only when they need the acting user (for audit logging).
"""

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_admin
from app.routers.admin import (
    applications,
    audit,
    blog,
    catalog,
    cloud,
    database,
    flags,
    health,
    jobs,
    metrics,
    revenue,
    scanner,
    users,
    waitlist,
)
from app.routers.admin import config as admin_config

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
# Operations — read-only observability.
admin_router.include_router(health.router)
admin_router.include_router(database.router)
admin_router.include_router(cloud.router)
admin_router.include_router(audit.router)
# Monetization.
admin_router.include_router(revenue.router)
# Catalog & product.
admin_router.include_router(catalog.router)
admin_router.include_router(scanner.router)
# Metrics, people, content, config.
admin_router.include_router(metrics.router)
admin_router.include_router(users.router)
admin_router.include_router(admin_config.router)
admin_router.include_router(flags.router)
admin_router.include_router(jobs.router)
admin_router.include_router(applications.router)
admin_router.include_router(blog.router)
admin_router.include_router(waitlist.router)

__all__ = ["admin_router"]

"""
FastAPI API Package for Enterprise Compliance Gateway.
"""

from fastapi import APIRouter

from backend.app.api.admin import router as admin_router
from backend.app.api.approval import router as approval_router
from backend.app.api.audit import router as audit_router
from backend.app.api.auth import router as auth_router
from backend.app.api.crm import router as crm_router
from backend.app.api.pipeline import router as pipeline_router
from backend.app.api.rollback import router as rollback_router

api_router = APIRouter()

# Include all sub-routers
api_router.include_router(auth_router)
api_router.include_router(pipeline_router)
api_router.include_router(approval_router)
api_router.include_router(rollback_router)
api_router.include_router(crm_router)
api_router.include_router(admin_router)
api_router.include_router(audit_router)

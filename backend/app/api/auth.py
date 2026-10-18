"""
Admin Authentication & Session Router.

Provides:
- POST /api/auth/login: Validates credentials and returns JWT bearer token.
- GET /api/auth/me: Returns current authenticated admin profile.
- get_current_admin: Reusable FastAPI dependency for securing endpoints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.api.schemas import LoginRequest, LoginResponse, UserOut
from backend.app.core.config import settings
from backend.app.core.security import (
    ExpiredApprovalTokenError,
    InvalidApprovalTokenError,
    create_access_token,
    verify_access_token,
    verify_admin_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Admin Authentication"])
security_scheme = HTTPBearer(auto_error=False)


async def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
) -> UserOut:
    """
    Validates the Bearer access token from Authorization header.
    Raises 401 if missing, expired, or invalid.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        claims = verify_access_token(token)
    except ExpiredApprovalTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidApprovalTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    if claims.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required.",
        )

    return UserOut(
        username=claims.get("sub", settings.ADMIN_USERNAME),
        email=claims.get("email", settings.ADMIN_EMAIL),
        role="admin",
        is_admin=True,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate admin credentials and issue session JWT",
)
async def login(request: LoginRequest) -> LoginResponse:
    """
    Authenticates administrative user credentials.
    Returns a cryptographically signed HMAC-SHA256 JWT access token.
    """
    user_input = request.username.strip().lower()
    admin_user = settings.ADMIN_USERNAME.lower()
    admin_email = settings.ADMIN_EMAIL.lower()

    # Allow login by either username or email
    user_matched = user_input == admin_user or user_input == admin_email
    pass_matched = verify_admin_password(request.password, settings.ADMIN_PASSWORD)

    if not (user_matched and pass_matched):
        logger.warning(f"Failed login attempt for username: {request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        data={
            "sub": settings.ADMIN_USERNAME,
            "email": settings.ADMIN_EMAIL,
            "role": "admin",
        },
        expires_in_minutes=settings.AUTH_TOKEN_EXPIRE_MINUTES,
    )

    logger.info(f"Successful admin login for user: {settings.ADMIN_USERNAME}")

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.AUTH_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut(
            username=settings.ADMIN_USERNAME,
            email=settings.ADMIN_EMAIL,
            role="admin",
            is_admin=True,
        ),
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get current authenticated admin user profile",
)
async def get_me(current_user: UserOut = Depends(get_current_admin)) -> UserOut:
    """Returns profile information for the current verified admin session."""
    return current_user

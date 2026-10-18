import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from backend.app.core.config import settings


# ==============================================================================
# Security Exceptions
# ==============================================================================


class SecurityException(Exception):
    """Base exception for cryptographic security errors."""

    pass


class InvalidApprovalTokenError(SecurityException):
    """Raised when an approval token has an invalid signature, structure, or claims."""

    pass


class ExpiredApprovalTokenError(SecurityException):
    """Raised when an approval token has passed its expiration time."""

    pass


class TamperedPlanError(SecurityException):
    """
    Raised when the proposed SQL plan currently in the database does not match
    the SHA-256 fingerprint embedded inside the cryptographic approval token.
    """

    pass


# ==============================================================================
# Deterministic Hashing & Token Management
# ==============================================================================


def compute_plan_hash(plan_content: Any) -> str:
    """
    Computes a deterministic SHA-256 hex digest of the proposed SQL migration plan.
    Keys are sorted to ensure identical JSON representations always produce
    the exact same hash.
    """
    canonical_json_str = json.dumps(plan_content, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json_str.encode("utf-8")).hexdigest()


def generate_approval_token(
    thread_id: str | uuid.UUID,
    plan_id: str | uuid.UUID,
    plan_hash: str,
    expires_in_minutes: Optional[int] = None,
) -> str:
    """
    Generates a cryptographically signed HMAC-SHA256 JWT token bound to a specific
    plan ID, thread ID, and plan hash.
    """
    now = datetime.now(timezone.utc)
    duration_minutes = (
        expires_in_minutes or settings.APPROVAL_TOKEN_EXPIRE_MINUTES
    )
    expires_at = now + timedelta(minutes=duration_minutes)

    payload = {
        "sub": str(plan_id),
        "thread_id": str(thread_id),
        "plan_hash": plan_hash,
        "type": "hitl_approval_token",
        "iss": settings.APP_NAME,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }

    return jwt.encode(
        payload=payload,
        key=settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def verify_approval_token(
    token: str,
    expected_plan_id: str | uuid.UUID,
    current_plan_hash: str,
) -> dict[str, Any]:
    """
    Validates the cryptographic signature, checks expiration, and verifies that
    the current plan hash in the database matches the hash stamped in the token.

    Returns the decoded token claims if validation succeeds.
    Raises:
        ExpiredApprovalTokenError: If token expired.
        TamperedPlanError: If plan was modified after token generation.
        InvalidApprovalTokenError: If token signature is invalid or forged.
    """
    try:
        claims = jwt.decode(
            jwt=token,
            key=settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except ExpiredSignatureError as e:
        raise ExpiredApprovalTokenError(
            "Approval token has expired. A fresh review must be triggered."
        ) from e
    except InvalidTokenError as e:
        raise InvalidApprovalTokenError(
            f"Invalid approval token signature or malformed payload: {e}"
        ) from e

    # 1. Verify Plan ID Binding
    token_plan_id = claims.get("sub")
    if token_plan_id != str(expected_plan_id):
        raise InvalidApprovalTokenError(
            f"Token plan ID mismatch: expected '{expected_plan_id}', found '{token_plan_id}'."
        )

    # 2. Verify Plan Hash (Tamper-Proof Verification)
    token_plan_hash = claims.get("plan_hash")
    if token_plan_hash != current_plan_hash:
        raise TamperedPlanError(
            "Security Alert: The SQL migration plan in the database does not match "
            "the hash sealed in the approval token! Plan was modified after token issuance."
        )

    return claims


# ==============================================================================
# Admin Authentication & Session Security
# ==============================================================================


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Computes a salted PBKDF2-HMAC-SHA256 password digest."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}:{key.hex()}"


def verify_admin_password(plain_password: str, stored_password: str) -> bool:
    """
    Verifies a plain password against stored password.
    Supports both plaintext comparison (e.g. from env setting) and PBKDF2 hash.
    Uses constant-time comparison to protect against timing attacks.
    """
    if ":" in stored_password:
        try:
            salt_hex, key_hex = stored_password.split(":", 1)
            salt = bytes.fromhex(salt_hex)
            expected_key = bytes.fromhex(key_hex)
            actual_key = hashlib.pbkdf2_hmac(
                "sha256", plain_password.encode("utf-8"), salt, 100_000
            )
            return secrets.compare_digest(actual_key, expected_key)
        except Exception:
            return False
    return secrets.compare_digest(plain_password, stored_password)


def create_access_token(
    data: dict[str, Any],
    expires_in_minutes: Optional[int] = None,
) -> str:
    """
    Generates a cryptographically signed HMAC-SHA256 JWT access token for admin sessions.
    """
    now = datetime.now(timezone.utc)
    duration_minutes = expires_in_minutes or settings.AUTH_TOKEN_EXPIRE_MINUTES
    expires_at = now + timedelta(minutes=duration_minutes)

    payload = {
        **data,
        "type": "access_token",
        "iss": settings.APP_NAME,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }

    return jwt.encode(
        payload=payload,
        key=settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def verify_access_token(token: str) -> dict[str, Any]:
    """
    Decodes and validates an admin JWT access token.
    """
    try:
        claims = jwt.decode(
            jwt=token,
            key=settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except ExpiredSignatureError as e:
        raise ExpiredApprovalTokenError("Admin session has expired. Please log in again.") from e
    except InvalidTokenError as e:
        raise InvalidApprovalTokenError(f"Invalid admin session token: {e}") from e

    if claims.get("type") != "access_token":
        raise InvalidApprovalTokenError("Token is not an authorization access token.")

    return claims

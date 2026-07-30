"""User authentication domain services and repositories."""

from super_ai.auth.repositories import (
    AuthRepository,
    AuthSessionRecord,
    UserRecord,
)
from super_ai.auth.service import (
    AuthError,
    AuthResult,
    AuthService,
    normalize_email,
)
from super_ai.auth.sqlalchemy import SQLAlchemyAuthRepository

__all__ = [
    "AuthError",
    "AuthRepository",
    "AuthResult",
    "AuthService",
    "AuthSessionRecord",
    "SQLAlchemyAuthRepository",
    "UserRecord",
    "normalize_email",
]

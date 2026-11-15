"""Core IAM/RBAC primitives (incremental, compatible with existing session auth).

Provides DB-backed users, roles, permissions, and password hashing helpers.
Existing cookie-session/API-key auth in main.py remains functional; this module enables
progressive hardening and RBAC enforcement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from database import (
    iam_create_user, iam_get_user_by_email, iam_get_user, iam_list_users, iam_update_user,
    iam_ensure_role, iam_assign_role, iam_list_roles, iam_list_permissions, iam_grant_permission,
)

DEFAULT_ROLES = {
    "SUPERADMIN": ["*"] ,
    "ADMIN": ["users.manage","roles.manage","system.configure","audit.read","sessions.manage","products.write","pricing.write","bundles.manage","ai.configure","matterhorn.configure","shopware.configure","marketplace.manage","connectors.read","connectors.configure","connectors.activate","accounts.manage","mapping.manage","exports.execute"],
    "MANAGER": ["products.write","pricing.write","bundles.manage","marketplace.manage","connectors.read","exports.execute"],
    "EDITOR": ["products.write","ai.generate","connectors.read"],
    "ANALYST": ["audit.read","connectors.read"],
    "VIEWER": ["products.read","pricing.read","connectors.read"],
}


def hash_password(password: str, iterations: int = 200_000) -> str:
    if not password:
        raise ValueError("password required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, it_s, salt_b64, hash_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        it = int(it_s)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(hash_b64.encode())
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


async def bootstrap_defaults() -> None:
    for role, perms in DEFAULT_ROLES.items():
        await iam_ensure_role(role, description=f"Default role {role}")
        for p in perms:
            await iam_grant_permission(role, p)

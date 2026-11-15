"""Model identity and rights management for Omni-Core."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database import model_permission_list, model_permission_upsert, model_profile_create, model_profile_get, model_profile_list, model_profile_update, model_reference_create, model_reference_list

logger = logging.getLogger("omni.models.identity")

VALID_MODEL_TYPES = {"own_identity", "fictional", "real_person"}
VALID_CONSENT_STATUSES = {"not_required", "pending", "granted", "expired", "revoked"}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def create_profile(*, profile_id: str | None = None, display_name: str, model_type: str,
                         active: bool = True, visual_traits: dict[str, Any] | None = None,
                         pose_preferences: list[str] | None = None, style_description: str = "",
                         permitted_use: list[str] | None = None, consent_status: str = "not_required",
                         consent_valid_from: str | None = None, consent_valid_until: str | None = None,
                         rights_holder: str = "", approval_document_ref: str = "") -> dict[str, Any]:
    """Create and return a model identity profile."""
    if model_type not in VALID_MODEL_TYPES:
        raise ValueError("Unsupported model type.")
    if consent_status not in VALID_CONSENT_STATUSES:
        raise ValueError("Unsupported consent status.")
    if model_type == "real_person" and consent_status not in {"granted", "pending"}:
        raise ValueError("Real person profiles require pending or granted consent.")

    import uuid

    new_id = profile_id or str(uuid.uuid4())
    await model_profile_create(
        profile_id=new_id,
        display_name=display_name,
        model_type=model_type,
        active=active,
        visual_traits=visual_traits,
        pose_preferences=pose_preferences,
        style_description=style_description,
        permitted_use=permitted_use,
        consent_status=consent_status,
        consent_valid_from=consent_valid_from,
        consent_valid_until=consent_valid_until,
        rights_holder=rights_holder,
        approval_document_ref=approval_document_ref,
    )
    profile = await model_profile_get(new_id)
    if profile is None:
        raise RuntimeError("Model profile could not be loaded after creation.")
    return profile


async def list_profiles(*, model_type: str | None = None, consent_status: str | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
    """List model profiles."""
    return await model_profile_list(model_type=model_type, consent_status=consent_status, limit=limit)


async def get_profile(profile_id: str) -> dict[str, Any] | None:
    """Return one model profile."""
    return await model_profile_get(profile_id)


async def update_profile(profile_id: str, **fields: Any) -> bool:
    """Update a model profile."""
    if "model_type" in fields and fields["model_type"] not in VALID_MODEL_TYPES:
        raise ValueError("Unsupported model type.")
    if "consent_status" in fields and fields["consent_status"] not in VALID_CONSENT_STATUSES:
        raise ValueError("Unsupported consent status.")
    return await model_profile_update(profile_id, **fields)


async def add_reference(*, profile_id: str, reference_id: str | None = None, reference_type: str,
                        asset_id: str, version_number: int = 1,
                        quality_score: float | None = None, is_primary: bool = False) -> dict[str, Any]:
    """Attach an asset as a model reference."""
    profile = await model_profile_get(profile_id)
    if profile is None:
        raise ValueError("Model profile not found.")
    if reference_type not in {"front", "profile", "back", "expression", "full_body", "pack"}:
        raise ValueError("Unsupported reference type.")

    import uuid

    new_id = reference_id or str(uuid.uuid4())
    await model_reference_create(
        reference_id=new_id,
        model_profile_id=profile_id,
        reference_type=reference_type,
        asset_id=asset_id,
        version_number=version_number,
        quality_score=quality_score,
        is_primary=is_primary,
    )
    references = await model_reference_list(profile_id)
    for reference in references:
        if reference["id"] == new_id:
            return reference
    raise RuntimeError("Model reference could not be loaded after creation.")


async def list_references(profile_id: str) -> list[dict[str, Any]]:
    """Return references for a profile."""
    return await model_reference_list(profile_id)


async def set_permission(*, profile_id: str, permission_id: str | None = None,
                         permission_type: str, granted: bool,
                         scope: dict[str, Any] | None = None,
                         valid_until: str | None = None) -> None:
    """Upsert a model usage permission."""
    profile = await model_profile_get(profile_id)
    if profile is None:
        raise ValueError("Model profile not found.")
    if not permission_type:
        raise ValueError("Permission type is required.")

    import uuid

    await model_permission_upsert(
        permission_id=permission_id or str(uuid.uuid4()),
        model_profile_id=profile_id,
        permission_type=permission_type,
        granted=granted,
        scope=scope,
        valid_until=valid_until,
    )


async def list_permissions(profile_id: str) -> list[dict[str, Any]]:
    """Return permissions for a profile."""
    return await model_permission_list(profile_id)


async def generation_allowed(profile_id: str, permission_type: str = "image_generation") -> tuple[bool, str]:
    """Check whether a profile may be used for generation."""
    profile = await model_profile_get(profile_id)
    if profile is None:
        return False, "Model profile not found."
    if not bool(profile.get("active")):
        return False, "Model profile is inactive."

    model_type = profile.get("model_type")
    if model_type == "real_person":
        consent_status = profile.get("consent_status")
        if consent_status != "granted":
            return False, f"Consent status is {consent_status}."
        valid_until = _parse_timestamp(profile.get("consent_valid_until"))
        if valid_until is not None and valid_until < datetime.now(timezone.utc):
            return False, "Consent has expired."

    permissions = await model_permission_list(profile_id)
    matching = [item for item in permissions if item.get("permission_type") == permission_type]
    if not matching:
        return False, f"Permission {permission_type} is not configured."
    permission = matching[0]
    if not bool(permission.get("granted")):
        return False, f"Permission {permission_type} is not granted."
    valid_until = _parse_timestamp(permission.get("valid_until"))
    if valid_until is not None and valid_until < datetime.now(timezone.utc):
        return False, f"Permission {permission_type} has expired."
    return True, "allowed"

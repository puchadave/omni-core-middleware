"""Local asset management for Omni-Core.

Assets are stored under media/assets. SHA-256 is used for duplicate detection.
Original files are preserved; generated variants receive new asset IDs and versions.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

from database import asset_create, asset_find_by_hash, asset_get, asset_list, asset_update, asset_version_create, asset_version_list

logger = logging.getLogger("omni.assets")

ASSET_BASE_DIR = Path("media/assets")
ORIGINAL_DIR = ASSET_BASE_DIR / "original"
THUMBNAIL_DIR = ASSET_BASE_DIR / "thumbnails"

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}


def _ensure_dirs() -> None:
    for directory in (ASSET_BASE_DIR, ORIGINAL_DIR, THUMBNAIL_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _safe_filename(filename: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename or "asset")
    if len(safe) > 180:
        name, dot, extension = safe.rpartition(".")
        if dot:
            safe = name[:170] + "." + extension
        else:
            safe = safe[:180]
    return safe or "asset"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        from io import BytesIO

        with PILImage.open(BytesIO(data)) as image:
            return image.width, image.height
    except Exception:
        return None, None


def _generate_thumbnail(data: bytes, asset_id: str, mime_type: str) -> str | None:
    """Create a JPEG thumbnail for image assets; returns path or None."""
    if mime_type not in IMAGE_MIME_TYPES:
        return None
    try:
        from io import BytesIO

        _ensure_dirs()
        with PILImage.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((512, 512))
            thumb_path = THUMBNAIL_DIR / f"{asset_id}.jpg"
            image.save(thumb_path, format="JPEG", quality=85, optimize=True)
        return str(thumb_path)
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", asset_id, exc)
        return None


def get_thumbnail_path(asset_id: str) -> str | None:
    """Return thumbnail path if it exists."""
    path = THUMBNAIL_DIR / f"{asset_id}.jpg"
    return str(path) if path.is_file() else None


async def create_asset_from_upload(
    *,
    upload: Any,
    asset_type: str,
    source_type: str = "upload",
    source_url: str = "",
    product_sku: str | None = None,
    model_profile_id: str | None = None,
    campaign_id: str | None = None,
    rights_status: str = "unknown",
    approval_status: str = "draft",
    created_by: str = "system",
) -> dict[str, Any]:
    """Store an uploaded file and persist an asset record."""
    if asset_type not in {"image", "video", "document", "audio", "graphic"}:
        raise ValueError("Unsupported asset type.")

    data = await upload.read()
    if not data:
        raise ValueError("Uploaded file is empty.")

    sha256_hash = _sha256_bytes(data)
    duplicate = await asset_find_by_hash(sha256_hash)
    is_duplicate = duplicate is not None

    if is_duplicate:
        storage_path = str(duplicate["storage_path"])
    else:
        _ensure_dirs()
        asset_id = str(uuid.uuid4())
        safe_name = _safe_filename(getattr(upload, "filename", None) or "asset")
        storage_path = str(ORIGINAL_DIR / f"{asset_id}_{safe_name}")
        with open(storage_path, "wb") as target:
            target.write(data)

    asset_id = str(uuid.uuid4())
    width, height = _image_dimensions(data) if not is_duplicate else (duplicate.get("width"), duplicate.get("height"))
    mime_type = getattr(upload, "content_type", None) or "application/octet-stream"
    original_filename = getattr(upload, "filename", None) or ""
    thumbnail_path = _generate_thumbnail(data, asset_id, mime_type)

    await asset_create(
        asset_id=asset_id,
        asset_type=asset_type,
        storage_path=storage_path,
        original_filename=original_filename,
        mime_type=mime_type,
        size_bytes=len(data) if not is_duplicate else int(duplicate.get("size_bytes") or len(data)),
        sha256_hash=sha256_hash,
        width=width,
        height=height,
        source_type=source_type,
        source_url=source_url,
        product_sku=product_sku,
        model_profile_id=model_profile_id,
        campaign_id=campaign_id,
        rights_status=rights_status,
        approval_status=approval_status,
        thumbnail_path=thumbnail_path,
        created_by=created_by,
    )
    await asset_version_create(
        version_id=str(uuid.uuid4()),
        asset_id=asset_id,
        version_number=1,
        storage_path=storage_path,
        sha256_hash=sha256_hash,
        change_reason="initial upload",
        created_by=created_by,
    )

    record = await asset_get(asset_id)
    if record is None:
        raise RuntimeError("Asset record could not be loaded after creation.")
    result = dict(record)
    result["is_duplicate"] = is_duplicate
    return result


async def list_assets(**filters: Any) -> list[dict[str, Any]]:
    """List assets with optional filters."""
    limit = int(filters.pop("limit", 100))
    return await asset_list(limit=limit, **filters)


async def get_asset(asset_id: str) -> dict[str, Any] | None:
    """Return one asset."""
    return await asset_get(asset_id)


async def update_asset(asset_id: str, **fields: Any) -> bool:
    """Update mutable asset fields."""
    return await asset_update(asset_id, **fields)


async def create_asset_version_from_upload(
    *,
    asset_id: str,
    upload: Any,
    change_reason: str = "new version",
    created_by: str = "system",
) -> dict[str, Any]:
    """Store a new immutable version for an existing asset."""
    asset = await asset_get(asset_id)
    if asset is None:
        raise ValueError("Asset not found.")

    data = await upload.read()
    if not data:
        raise ValueError("Uploaded file is empty.")

    sha256_hash = _sha256_bytes(data)
    _ensure_dirs()
    version_number = len(await asset_version_list(asset_id)) + 1
    safe_name = _safe_filename(getattr(upload, "filename", None) or "asset")
    storage_path = str(ORIGINAL_DIR / f"{asset_id}_v{version_number}_{safe_name}")
    with open(storage_path, "wb") as target:
        target.write(data)

    width, height = _image_dimensions(data)
    await asset_version_create(
        version_id=str(uuid.uuid4()),
        asset_id=asset_id,
        version_number=version_number,
        storage_path=storage_path,
        sha256_hash=sha256_hash,
        change_reason=change_reason,
        created_by=created_by,
    )
    mime_type = getattr(upload, "content_type", None) or asset.get("mime_type", "application/octet-stream")
    thumbnail_path = _generate_thumbnail(data, asset_id, mime_type)
    await asset_update(
        asset_id,
        storage_path=storage_path,
        sha256_hash=sha256_hash,
        size_bytes=len(data),
        width=width,
        height=height,
        mime_type=mime_type,
        thumbnail_path=thumbnail_path,
    )
    versions = await asset_version_list(asset_id)
    return versions[-1]


async def list_asset_versions(asset_id: str) -> list[dict[str, Any]]:
    """Return versions for one asset."""
    return await asset_version_list(asset_id)

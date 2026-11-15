"""Creative studio persistence and orchestration for Omni-Core."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
import uuid
from typing import Any

from database import (
    asset_create,
    campaign_create,
    campaign_get,
    campaign_list,
    campaign_update,
    creative_job_create,
    creative_job_get,
    creative_job_list,
    creative_job_update,
    creative_output_create,
    creative_output_list,
    creative_project_create,
    creative_project_get,
    creative_project_list,
    creative_project_update,
    video_project_create,
    video_project_list,
    video_scene_create,
    video_scene_list,
    write_log,
)

logger = logging.getLogger("omni.creative")

def _parse_json_field(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    result = dict(job)
    result["request_payload"] = _parse_json_field(result.pop("request_payload_json", None))
    result["response_payload"] = _parse_json_field(result.pop("response_payload_json", None))
    return result




def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def create_project(*, project_id: str | None = None, name: str, project_type: str,
                        status: str = "draft", product_sku: str | None = None,
                        bundle_sku: str | None = None, model_profile_id: str | None = None,
                        created_by: str = "system") -> dict[str, Any]:
    new_id = project_id or str(uuid.uuid4())
    await creative_project_create(
        project_id=new_id,
        name=name,
        project_type=project_type,
        status=status,
        product_sku=product_sku,
        bundle_sku=bundle_sku,
        model_profile_id=model_profile_id,
        created_by=created_by,
    )
    project = await creative_project_get(new_id)
    if project is None:
        raise RuntimeError("Creative project could not be loaded after creation.")
    return project


async def list_projects(*, project_type: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await creative_project_list(project_type=project_type, status=status, limit=limit)


async def get_project(project_id: str) -> dict[str, Any] | None:
    return await creative_project_get(project_id)


async def update_project(project_id: str, **fields: Any) -> bool:
    return await creative_project_update(project_id, **fields)


async def create_job(*, job_id: str | None = None, project_id: str, job_type: str,
                     status: str = "draft", agent_run_id: str | None = None,
                     provider_id: str | None = None, model_id: str | None = None,
                     prompt_version: str = "", request_payload: dict[str, Any] | None = None,
                     response_payload: dict[str, Any] | None = None,
                     progress_percent: int = 0, cost_usd: float | None = None,
                     error_message: str = "") -> dict[str, Any]:
    new_id = job_id or str(uuid.uuid4())
    await creative_job_create(
        job_id=new_id,
        project_id=project_id,
        job_type=job_type,
        status=status,
        agent_run_id=agent_run_id,
        provider_id=provider_id,
        model_id=model_id,
        prompt_version=prompt_version,
        request_payload=request_payload,
        response_payload=response_payload,
        progress_percent=progress_percent,
        cost_usd=cost_usd,
        error_message=error_message,
    )
    job = await creative_job_get(new_id)
    if job is None:
        raise RuntimeError("Creative job could not be loaded after creation.")
    return _normalize_job(job)


async def list_jobs(project_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return [_normalize_job(job) for job in await creative_job_list(project_id=project_id, status=status, limit=limit)]


async def get_job(job_id: str) -> dict[str, Any] | None:
    return _normalize_job(await creative_job_get(job_id))


async def update_job(job_id: str, **fields: Any) -> bool:
    return await creative_job_update(job_id, **fields)


async def generate_image_for_job(job_id: str, *, prompt: str, model_reference: str | None = None,
                                product_reference: str | None = None,
                                width: int = 1024, height: int = 1024,
                                created_by: str = "system") -> dict[str, Any]:
    """Run one image generation through the existing provider abstraction."""
    job = await creative_job_get(job_id)
    if job is None:
        raise ValueError("Creative job not found.")
    project = await creative_project_get(str(job["project_id"]))
    if project is None:
        raise ValueError("Creative project not found.")

    await creative_job_update(job_id, status="generating", progress_percent=10, started_at=datetime.now(timezone.utc).isoformat())
    request_payload = {
        "prompt": prompt,
        "model_reference": model_reference,
        "product_reference": product_reference,
        "width": width,
        "height": height,
    }
    await creative_job_update(job_id, request_payload=request_payload, progress_percent=25)

    from services.ai_photoshooting import get_provider

    try:
        result = await get_provider().generate(request_payload)
    except Exception as exc:
        await creative_job_update(job_id, status="failed", error_message=str(exc), progress_percent=100, completed_at=datetime.now(timezone.utc).isoformat())
        await write_log("ERROR", "creative.image.failed", "Image generation failed", {"job_id": job_id, "error": str(exc)})
        raise

    if not isinstance(result, dict):
        result = {"result": result}
    if result.get("error"):
        await creative_job_update(job_id, status="failed", error_message=str(result["error"]), response_payload=result, progress_percent=100, completed_at=datetime.now(timezone.utc).isoformat())
        await write_log("ERROR", "creative.image.failed", "Image generation returned an error", {"job_id": job_id, "error": result["error"]})
        raise ValueError(str(result["error"]))

    asset_id = str(uuid.uuid4())
    storage_path = str(result.get("image_url") or result.get("image_path") or result.get("url") or "")
    if not storage_path:
        storage_path = f"generated/{job_id}"
    sha256_hash = hashlib.sha256(_json(result).encode()).hexdigest()
    await asset_create(
        asset_id=asset_id,
        asset_type="image",
        storage_path=storage_path,
        original_filename=f"{job_id}.png",
        mime_type="image/png",
        size_bytes=0,
        sha256_hash=sha256_hash,
        width=result.get("width"),
        height=result.get("height"),
        source_type="generated",
        source_url=result.get("image_url") or result.get("url") or "",
        product_sku=project.get("product_sku"),
        model_profile_id=project.get("model_profile_id"),
        rights_status="unknown",
        approval_status="review_required",
        created_by="creative-studio",
    )

    await creative_output_create(
        output_id=str(uuid.uuid4()),
        job_id=job_id,
        asset_id=asset_id,
        output_type="image",
        quality_score=result.get("fidelity_score"),
        fidelity_score=result.get("fidelity_score"),
        model_consistency_score=None,
        technical_score=None,
        approval_status="review_required",
        rejection_reason="",
        provenance={
            "provider": result.get("provider"),
            "generation_type": result.get("generation_type"),
            "simulated": bool(result.get("simulated", False)),
            "request": request_payload,
            "response": result,
        },
    )

    await creative_job_update(
        job_id,
        status="generated",
        response_payload=result,
        progress_percent=100,
        cost_usd=result.get("cost_usd"),
        completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    )
    await write_log("INFO", "creative.image.generated", "Image generation completed", {"job_id": job_id, "asset_id": asset_id})
    return {"job_id": job_id, "asset_id": asset_id, "status": "generated", "result": result}


async def list_outputs(job_id: str | None = None, asset_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await creative_output_list(job_id=job_id, asset_id=asset_id, limit=limit)


async def create_video_project(*, video_project_id: str | None = None, creative_project_id: str,
                              format: str, target_channel: str = "",
                              status: str = "draft") -> dict[str, Any]:
    new_id = video_project_id or str(uuid.uuid4())
    await video_project_create(
        video_project_id=new_id,
        creative_project_id=creative_project_id,
        format=format,
        target_channel=target_channel,
        status=status,
    )
    projects = await video_project_list(creative_project_id=creative_project_id, limit=500)
    for project in projects:
        if project["id"] == new_id:
            return project
    raise RuntimeError("Video project could not be loaded after creation.")


async def list_video_projects(creative_project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await video_project_list(creative_project_id=creative_project_id, limit=limit)


async def create_video_scene(*, scene_id: str | None = None, video_project_id: str, scene_order: int,
                            scene_type: str, prompt_text: str = "",
                            source_asset_id: str | None = None,
                            generated_asset_id: str | None = None,
                            duration_seconds: float | None = None, status: str = "draft") -> dict[str, Any]:
    new_id = scene_id or str(uuid.uuid4())
    await video_scene_create(
        scene_id=new_id,
        video_project_id=video_project_id,
        scene_order=scene_order,
        scene_type=scene_type,
        prompt_text=prompt_text,
        source_asset_id=source_asset_id,
        generated_asset_id=generated_asset_id,
        duration_seconds=duration_seconds,
        status=status,
    )
    scenes = await video_scene_list(video_project_id)
    for scene in scenes:
        if scene["id"] == new_id:
            return scene
    raise RuntimeError("Video scene could not be loaded after creation.")


async def list_video_scenes(video_project_id: str) -> list[dict[str, Any]]:
    return await video_scene_list(video_project_id)


async def create_campaign(*, campaign_id: str | None = None, name: str, status: str = "draft",
                         target_audience: str = "", creative_project_id: str | None = None,
                         created_by: str = "system") -> dict[str, Any]:
    new_id = campaign_id or str(uuid.uuid4())
    await campaign_create(
        campaign_id=new_id,
        name=name,
        status=status,
        target_audience=target_audience,
        creative_project_id=creative_project_id,
        created_by=created_by,
    )
    campaign = await campaign_get(new_id)
    if campaign is None:
        raise RuntimeError("Campaign could not be loaded after creation.")
    return campaign


async def list_campaigns(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await campaign_list(status=status, limit=limit)


async def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    return await campaign_get(campaign_id)


async def update_campaign(campaign_id: str, **fields: Any) -> bool:
    return await campaign_update(campaign_id, **fields)

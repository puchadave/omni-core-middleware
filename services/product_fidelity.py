"""Product fidelity evaluation for generated creative assets."""

from __future__ import annotations

import json
import logging
from typing import Any

from database import creative_output_get, creative_output_update, write_log

logger = logging.getLogger("omni.fidelity")


def _clamp_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, score))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


async def evaluate_output_fidelity(
    output_id: str,
    *,
    original_image_url: str,
    generated_image_url: str,
    runtime: Any = None,
    model_id: str | None = None,
    product_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a generated asset against its original product image."""
    output = await creative_output_get(output_id)
    if output is None:
        raise ValueError("Creative output not found.")

    omni_route = getattr(runtime, "omniroute", None) if runtime is not None else None
    if omni_route is None:
        result = {
            "status": "unavailable",
            "reason": "OmniRoute is not configured.",
            "publishable": False,
        }
        await write_log("WARNING", "creative.fidelity.unavailable", "Fidelity check skipped", {"output_id": output_id})
        return result

    model = model_id or "gemini-2.5-flash"
    prompt = (
        "Compare the original product image and the generated image. "
        "Return JSON with numeric scores from 0 to 100 for visual_quality, "
        "product_fidelity, model_consistency, scene_quality, and technical_quality. "
        "Return deviations as a list of strings and critical_deviations as a list "
        "of strings. Check color, cut, pattern, fabric structure, sleeves, collar, "
        "neckline, buttons, closures, logos, prints, seams, and proportions. "
        f"Product context: {json.dumps(product_context or {}, ensure_ascii=False)}."
    )
    analysis = await omni_route.analyze_images(prompt, [original_image_url, generated_image_url], model=model)
    if not isinstance(analysis, dict):
        result = {
            "status": "unavailable",
            "reason": "OmniRoute did not return a valid analysis.",
            "publishable": False,
        }
        await write_log("WARNING", "creative.fidelity.unavailable", "Invalid fidelity analysis", {"output_id": output_id})
        return result

    visual_quality = _clamp_score(analysis.get("visual_quality"))
    product_fidelity = _clamp_score(analysis.get("product_fidelity"))
    model_consistency = _clamp_score(analysis.get("model_consistency"))
    scene_quality = _clamp_score(analysis.get("scene_quality"))
    technical_quality = _clamp_score(analysis.get("technical_quality"))
    deviations = _string_list(analysis.get("deviations"))
    critical_deviations = _string_list(analysis.get("critical_deviations"))
    publishable = not critical_deviations and (product_fidelity is None or product_fidelity >= 80.0)

    approval_status = "quality_check" if publishable else "review_required"
    await creative_output_update(
        output_id,
        quality_score=visual_quality,
        fidelity_score=product_fidelity,
        model_consistency_score=model_consistency,
        technical_score=technical_quality,
        approval_status=approval_status,
        provenance={
            "fidelity_report": {
                "model": model,
                "original_image_url": original_image_url,
                "generated_image_url": generated_image_url,
                "visual_quality": visual_quality,
                "product_fidelity": product_fidelity,
                "model_consistency": model_consistency,
                "scene_quality": scene_quality,
                "technical_quality": technical_quality,
                "deviations": deviations,
                "critical_deviations": critical_deviations,
                "publishable": publishable,
                "summary": analysis.get("summary"),
            }
        },
    )
    await write_log("INFO", "creative.fidelity.completed", "Fidelity evaluation completed", {"output_id": output_id, "publishable": publishable})
    return {
        "status": "completed",
        "output_id": output_id,
        "visual_quality": visual_quality,
        "product_fidelity": product_fidelity,
        "model_consistency": model_consistency,
        "scene_quality": scene_quality,
        "technical_quality": technical_quality,
        "deviations": deviations,
        "critical_deviations": critical_deviations,
        "publishable": publishable,
        "approval_status": approval_status,
    }

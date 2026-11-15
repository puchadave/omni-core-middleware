"""Video storyboard planning for Omni-Core.

Video generation providers are not wired into the current runtime. This module
therefore creates real, reviewable scene plans through OmniRoute when configured
and never reports a video as generated unless an asset is actually produced.
"""

from __future__ import annotations

import logging
from typing import Any

from database import video_scene_create, video_scene_list, write_log

logger = logging.getLogger("omni.video")


async def generate_storyboard(video_project_id: str, *, prompt: str, runtime: Any,
                             scene_count: int = 6) -> list[dict[str, Any]]:
    """Generate a structured storyboard through OmniRoute."""
    omni_route = getattr(runtime, "omniroute", None)
    if omni_route is None:
        raise ValueError("OmniRoute is not configured.")

    request_prompt = (
        f"Create a JSON object with a 'scenes' array containing exactly {scene_count} scenes "
        f"for this video concept: {prompt}. Each scene needs scene_type, prompt_text, "
        "duration_seconds, and camera_notes. Use concise production language."
    )
    analysis = await omni_route.get_ai_trend_analysis(request_prompt)
    if not isinstance(analysis, dict) or not isinstance(analysis.get("scenes"), list):
        raise ValueError("OmniRoute did not return a valid storyboard.")

    scenes = []
    for index, item in enumerate(analysis["scenes"][:scene_count]):
        if not isinstance(item, dict):
            continue
        scene_type = str(item.get("scene_type") or "scene").strip()[:128] or "scene"
        prompt_text = str(item.get("prompt_text") or "").strip()[:20000]
        try:
            duration = float(item.get("duration_seconds", 3))
        except (TypeError, ValueError):
            duration = 3.0
        duration = max(0.1, min(duration, 60.0))
        camera_notes = str(item.get("camera_notes") or "").strip()
        if camera_notes:
            prompt_text = f"{prompt_text}\nCamera: {camera_notes}".strip()
        from uuid import uuid4

        scene_id = str(uuid4())
        await video_scene_create(
            scene_id=scene_id,
            video_project_id=video_project_id,
            scene_order=index,
            scene_type=scene_type,
            prompt_text=prompt_text,
            duration_seconds=duration,
            status="draft",
        )
        created = next(item for item in await video_scene_list(video_project_id) if item["id"] == scene_id)
        scenes.append(created)

    await write_log("INFO", "video.storyboard.generated", "Video storyboard generated", {"video_project_id": video_project_id, "scene_count": len(scenes)})
    return scenes

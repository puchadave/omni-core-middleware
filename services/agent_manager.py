"""Agent manager and tool registry for Omni-Core.

The registry is the only execution path for agent tools. Tools call existing
service/database functions; no arbitrary SQL or shell commands are permitted.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from database import (
    agent_approval_create,
    agent_approval_list,
    agent_approval_review,
    agent_conversation_create,
    agent_conversation_list,
    agent_conversation_get,
    agent_profile_create,
    agent_profile_get,
    agent_profile_list,
    agent_profile_update,
    agent_run_create,
    agent_run_get,
    agent_run_list,
    agent_run_update,
    agent_tool_call_create,
    agent_tool_call_list,
    ai_list_models,
    asset_list,
    fetch_product,
    list_products,
    upsert_bundle,
    write_log,
)

logger = logging.getLogger("omni.agents")

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission: str
    handler: ToolHandler


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self.tools[spec.name] = spec

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "permission": tool.permission,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in self.tools.values()
        ]

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def validate_input(self, tool: ToolSpec, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("Tool input must be a JSON object.")
        schema = tool.input_schema
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValueError(f"Missing required input: {key}")
        for key, item in value.items():
            if key not in properties:
                raise ValueError(f"Unknown input: {key}")
            self._validate_value(item, properties[key], key)
        return value

    def _validate_value(self, value: Any, schema: dict[str, Any], path: str) -> None:
        expected_type = schema.get("type")
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        if expected_type in type_map and not isinstance(value, type_map[expected_type]):
            raise ValueError(f"Invalid type for {path}: expected {expected_type}.")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"Invalid value for {path}.")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                raise ValueError(f"{path} is too short.")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise ValueError(f"{path} is too long.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError(f"{path} is below the minimum.")
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError(f"{path} is above the maximum.")
        if isinstance(value, list) and "items" in schema:
            for index, item in enumerate(value):
                self._validate_value(item, schema["items"], f"{path}[{index}]")


registry = ToolRegistry()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _search_products(tool_input: dict[str, Any]) -> dict[str, Any]:
    limit = int(tool_input.get("limit", 20))
    offset = int(tool_input.get("offset", 0))
    products = await list_products(limit=limit, offset=offset)
    return {"products": products, "count": len(products)}


async def _get_product_details(tool_input: dict[str, Any]) -> dict[str, Any]:
    product = await fetch_product(str(tool_input["sku"]))
    if product is None:
        raise ValueError("Product not found.")
    return {"product": product}


async def _get_product_images(tool_input: dict[str, Any]) -> dict[str, Any]:
    assets = await asset_list(product_sku=str(tool_input["sku"]), limit=100)
    return {"assets": assets, "count": len(assets)}


async def _search_models(tool_input: dict[str, Any]) -> dict[str, Any]:
    models = await ai_list_models(provider_id=tool_input.get("provider_id"), limit=100)
    if tool_input.get("category"):
        models = [model for model in models if model.get("model_category") == tool_input["category"]]
    return {"models": models, "count": len(models)}


async def _get_model_reference(tool_input: dict[str, Any]) -> dict[str, Any]:
    from services.model_identity import get_profile, list_references, list_permissions, generation_allowed

    profile = await get_profile(str(tool_input["profile_id"]))
    if profile is None:
        raise ValueError("Model profile not found.")
    references = await list_references(profile["id"])
    permissions = await list_permissions(profile["id"])
    allowed, reason = await generation_allowed(profile["id"], str(tool_input.get("permission_type", "image_generation")))
    return {
        "profile": profile,
        "references": references,
        "permissions": permissions,
        "generation_allowed": allowed,
        "generation_reason": reason,
    }


async def _create_bundle(tool_input: dict[str, Any]) -> dict[str, Any]:
    await upsert_bundle(
        bundle_sku=str(tool_input["bundle_sku"]),
        title=str(tool_input["title"]),
        component_skus=list(tool_input["component_skus"]),
    )
    return {"bundle_sku": tool_input["bundle_sku"], "status": "created"}


async def _analyze_product(tool_input: dict[str, Any]) -> dict[str, Any]:
    product = await fetch_product(str(tool_input["sku"]))
    if product is None:
        raise ValueError("Product not found.")
    assets = await asset_list(product_sku=product["sku"], limit=100)
    missing = []
    for field_name in ("title", "category", "raw_description"):
        if not str(product.get(field_name) or "").strip():
            missing.append(field_name)
    if not assets:
        missing.append("media")
    return {
        "sku": product["sku"],
        "title": product.get("title", ""),
        "category": product.get("category", ""),
        "stock": product.get("stock", 0),
        "asset_count": len(assets),
        "missing_fields": missing,
        "completeness_score": round((7 - len(missing)) / 7 * 100, 2) if missing else 100.0,
    }


async def _create_image_prompt(tool_input: dict[str, Any]) -> dict[str, Any]:
    product = await fetch_product(str(tool_input["sku"]))
    if product is None:
        raise ValueError("Product not found.")
    model_name = str(tool_input.get("model_name", "david"))
    scene = str(tool_input.get("scene", "studio"))
    prompt = (
        f"Fashion product photo of {product.get('title', tool_input['sku'])}, "
        f"SKU {product['sku']}, category {product.get('category', '')}, "
        f"model {model_name}, scene {scene}, preserve exact product color, cut, "
        f"pattern, logos, seams, closures, and proportions."
    )
    return {"prompt": prompt, "sku": product["sku"], "model_name": model_name, "scene": scene}


async def _generate_image(tool_input: dict[str, Any]) -> dict[str, Any]:
    from services.ai_photoshooting import get_provider

    request = {
        "prompt": str(tool_input["prompt"]),
        "model_reference": tool_input.get("model_reference"),
        "product_reference": tool_input.get("product_reference"),
        "width": tool_input.get("width", 1024),
        "height": tool_input.get("height", 1024),
    }
    result = await get_provider().generate(request)
    if isinstance(result, dict) and result.get("error"):
        raise ValueError(str(result["error"]))
    return result


async def _request_approval(tool_input: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(uuid.uuid4())
    await agent_approval_create(
        approval_id=approval_id,
        run_id=str(tool_input["run_id"]),
        target_type=str(tool_input["target_type"]),
        target_id=str(tool_input["target_id"]),
        reason=str(tool_input.get("reason", "")),
    )
    return {"approval_id": approval_id, "status": "pending"}


async def _publish_approved_asset(tool_input: dict[str, Any]) -> dict[str, Any]:
    from database import asset_get, asset_update

    asset = await asset_get(str(tool_input["asset_id"]))
    if asset is None:
        raise ValueError("Asset not found.")
    if asset.get("approval_status") != "approved":
        raise ValueError("Asset is not approved.")
    await asset_update(str(tool_input["asset_id"]), approval_status="published")
    return {"asset_id": tool_input["asset_id"], "status": "published"}


def _register_default_tools() -> None:
    registry.register(ToolSpec(
        name="search_products",
        description="Search persisted products with bounded pagination.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "minimum": 0}}, "required": []},
        output_schema={"type": "object"},
        permission="product.read",
        handler=_search_products,
    ))
    registry.register(ToolSpec(
        name="get_product_details",
        description="Get one product by SKU.",
        input_schema={"type": "object", "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["sku"]},
        output_schema={"type": "object"},
        permission="product.read",
        handler=_get_product_details,
    ))
    registry.register(ToolSpec(
        name="get_product_images",
        description="Get durable assets assigned to a product.",
        input_schema={"type": "object", "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["sku"]},
        output_schema={"type": "object"},
        permission="asset.read",
        handler=_get_product_images,
    ))
    registry.register(ToolSpec(
        name="search_models",
        description="Search OmniRoute models, optionally by provider or category.",
        input_schema={"type": "object", "properties": {"provider_id": {"type": "string"}, "category": {"type": "string", "enum": ["text", "image", "video", "embedding", "multimodal", "general"]}}, "required": []},
        output_schema={"type": "object"},
        permission="model.read",
        handler=_search_models,
    ))
    registry.register(ToolSpec(
        name="get_model_reference",
        description="Get a model identity profile, references, permissions, and generation eligibility.",
        input_schema={"type": "object", "properties": {"profile_id": {"type": "string", "minLength": 1}, "permission_type": {"type": "string", "minLength": 1}}, "required": ["profile_id"]},
        output_schema={"type": "object"},
        permission="model.read",
        handler=_get_model_reference,
    ))
    registry.register(ToolSpec(
        name="create_bundle",
        description="Create or update a bundle definition.",
        input_schema={"type": "object", "properties": {"bundle_sku": {"type": "string", "minLength": 1, "maxLength": 64}, "title": {"type": "string", "minLength": 1, "maxLength": 256}, "component_skus": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}}, "required": ["bundle_sku", "title", "component_skus"]},
        output_schema={"type": "object"},
        permission="bundle.write",
        handler=_create_bundle,
    ))
    registry.register(ToolSpec(
        name="analyze_product",
        description="Analyze product completeness and assigned media.",
        input_schema={"type": "object", "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["sku"]},
        output_schema={"type": "object"},
        permission="product.read",
        handler=_analyze_product,
    ))
    registry.register(ToolSpec(
        name="create_image_prompt",
        description="Create a fidelity-preserving image prompt from product and model data.",
        input_schema={"type": "object", "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 64}, "model_name": {"type": "string", "minLength": 1, "maxLength": 64}, "scene": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["sku"]},
        output_schema={"type": "object"},
        permission="creative.prompt",
        handler=_create_image_prompt,
    ))
    registry.register(ToolSpec(
        name="generate_image",
        description="Generate an image through the configured photoshoot provider.",
        input_schema={"type": "object", "properties": {"prompt": {"type": "string", "minLength": 1}, "model_reference": {"type": ["string", "null"]}, "product_reference": {"type": ["string", "null"]}, "width": {"type": "integer", "minimum": 64, "maximum": 4096}, "height": {"type": "integer", "minimum": 64, "maximum": 4096}}, "required": ["prompt"]},
        output_schema={"type": "object"},
        permission="creative.generate",
        handler=_generate_image,
    ))
    registry.register(ToolSpec(
        name="request_approval",
        description="Create a human approval request for a generated or proposed action.",
        input_schema={"type": "object", "properties": {"run_id": {"type": "string", "minLength": 1}, "target_type": {"type": "string", "minLength": 1}, "target_id": {"type": "string", "minLength": 1}, "reason": {"type": "string"}}, "required": ["run_id", "target_type", "target_id"]},
        output_schema={"type": "object"},
        permission="approval.request",
        handler=_request_approval,
    ))
    registry.register(ToolSpec(
        name="publish_approved_asset",
        description="Publish an asset only if it is approved.",
        input_schema={"type": "object", "properties": {"asset_id": {"type": "string", "minLength": 1}}, "required": ["asset_id"]},
        output_schema={"type": "object"},
        permission="asset.publish",
        handler=_publish_approved_asset,
    ))


_register_default_tools()


async def create_agent_profile(**kwargs: Any) -> dict[str, Any]:
    import uuid

    profile_id = kwargs.pop("profile_id", None) or str(uuid.uuid4())
    await agent_profile_create(profile_id=profile_id, **kwargs)
    profile = await agent_profile_get(profile_id)
    if profile is None:
        raise RuntimeError("Agent profile could not be loaded after creation.")
    return profile


async def list_agent_profiles(*, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    return await agent_profile_list(active_only=active_only, limit=limit)


async def get_agent_profile(profile_id: str) -> dict[str, Any] | None:
    return await agent_profile_get(profile_id)


async def update_agent_profile(profile_id: str, **fields: Any) -> bool:
    return await agent_profile_update(profile_id, **fields)


async def create_conversation(*, conversation_id: str | None = None, agent_profile_id: str,
                             user_actor: str = "system", status: str = "active") -> dict[str, Any]:
    import uuid

    new_id = conversation_id or str(uuid.uuid4())
    await agent_conversation_create(conversation_id=new_id, agent_profile_id=agent_profile_id, user_actor=user_actor, status=status)
    conversation = await agent_conversation_get(new_id)
    if conversation is None:
        raise RuntimeError("Agent conversation could not be loaded after creation.")
    return conversation


async def list_conversations(agent_profile_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await agent_conversation_list(agent_profile_id=agent_profile_id, limit=limit)


async def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    return await agent_conversation_get(conversation_id)


async def create_run(*, run_id: str | None = None, conversation_id: str, agent_profile_id: str,
                     status: str = "running", input_data: dict[str, Any] | None = None) -> dict[str, Any]:
    import uuid

    new_id = run_id or str(uuid.uuid4())
    await agent_run_create(run_id=new_id, conversation_id=conversation_id, agent_profile_id=agent_profile_id, status=status, input_data=input_data)
    run = await agent_run_get(new_id)
    if run is None:
        raise RuntimeError("Agent run could not be loaded after creation.")
    return run


async def get_run(run_id: str) -> dict[str, Any] | None:
    return await agent_run_get(run_id)


async def update_run(run_id: str, **fields: Any) -> bool:
    return await agent_run_update(run_id, status=fields.get("status"), output_data=fields.get("output_data"), error_message=fields.get("error_message"))


async def list_runs(conversation_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await agent_run_list(conversation_id=conversation_id, limit=limit)


async def list_tool_calls(run_id: str) -> list[dict[str, Any]]:
    return await agent_tool_call_list(run_id)


async def create_approval(run_id: str, target_type: str, target_id: str, reason: str = "") -> dict[str, Any]:
    import uuid

    approval_id = str(uuid.uuid4())
    await agent_approval_create(approval_id=approval_id, run_id=run_id, target_type=target_type, target_id=target_id, reason=reason)
    approvals = await agent_approval_list(run_id=run_id, limit=500)
    for approval in approvals:
        if approval["id"] == approval_id:
            return approval
    raise RuntimeError("Agent approval could not be loaded after creation.")


async def list_approvals(run_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return await agent_approval_list(run_id=run_id, status=status, limit=limit)


async def review_approval(approval_id: str, *, approved: bool, reviewed_by: str = "system", reason: str = "") -> bool:
    return await agent_approval_review(approval_id, approved=approved, reviewed_by=reviewed_by, reason=reason)


async def execute_tool(run_id: str, tool_name: str, tool_input: dict[str, Any] | None = None,
                      actor: str = "system") -> dict[str, Any]:
    """Validate and execute one registered tool within an agent run."""
    tool = registry.get(tool_name)
    if tool is None:
        raise ValueError(f"Unknown tool: {tool_name}")

    run = await agent_run_get(run_id)
    if run is None:
        raise ValueError("Agent run not found.")
    if run.get("status") not in {"running", "paused"}:
        raise ValueError(f"Agent run is not executable: {run.get('status')}")

    profile = await agent_profile_get(str(run["agent_profile_id"]))
    if profile is None or not bool(profile.get("is_active")):
        raise ValueError("Agent profile is unavailable.")
    import json as _json

    allowed_tools = _json.loads(profile.get("allowed_tools_json") or "[]")
    if tool_name not in allowed_tools:
        raise PermissionError(f"Tool is not allowed for this agent: {tool_name}")

    validated_input = registry.validate_input(tool, tool_input or {})
    call_id = str(uuid.uuid4())
    try:
        output = await tool.handler(validated_input)
        await agent_tool_call_create(
            call_id=call_id,
            run_id=run_id,
            tool_name=tool_name,
            input_data=validated_input,
            output_data=output,
            status="success",
        )
        await write_log("INFO", "agent.tool.executed", f"Tool {tool_name} executed", {"run_id": run_id, "tool": tool_name, "actor": actor})
        return {"call_id": call_id, "tool_name": tool_name, "status": "success", "output": output}
    except Exception as exc:
        await agent_tool_call_create(
            call_id=call_id,
            run_id=run_id,
            tool_name=tool_name,
            input_data=validated_input,
            output_data={},
            status="error",
            error_message=str(exc),
        )
        await write_log("ERROR", "agent.tool.failed", f"Tool {tool_name} failed", {"run_id": run_id, "tool": tool_name, "error": str(exc), "actor": actor})
        raise


def select_omniroute_model(runtime: Any, *, workflow_name: str,
                           required_capability: str | None = None,
                           max_cost: float | None = None,
                           input_tokens: int = 0, output_tokens: int = 0,
                           image_count: int = 0) -> dict[str, Any] | None:
    """Select an OmniRoute model using existing routing and cost components."""
    engine = getattr(runtime, "omniroute_routing_engine", None)
    registry = getattr(runtime, "omniroute_model_registry", None)
    calculator = getattr(runtime, "omniroute_cost_calculator", None)
    if engine is None or registry is None:
        return None

    provider_id, model_id = engine.select_provider(workflow_name, required_capability=required_capability, max_cost=max_cost)
    if not provider_id or not model_id:
        return None

    model = registry.get_model(model_id)
    if model is None:
        return None

    estimated_cost = 0.0
    if calculator is not None:
        if getattr(model, "model_category", "") == "image":
            estimated_cost = calculator.calculate_image_cost(getattr(model, "image_price_per_image", 0), image_count)
        else:
            estimated_cost = calculator.calculate_text_cost(getattr(model, "input_price_per_1k", 0), getattr(model, "output_price_per_1k", 0), input_tokens, output_tokens)

    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "model_name": getattr(model, "model_name", model_id),
        "model_category": getattr(model, "model_category", ""),
        "capabilities": list(getattr(model, "capabilities", [])),
        "estimated_cost_usd": estimated_cost,
    }

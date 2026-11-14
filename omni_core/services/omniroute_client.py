import httpx
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class OmniRouteClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def check_health(self) -> bool:
        """Checks if the OmniRoute server is reachable via /models endpoint."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/models", headers=self.headers)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"OmniRoute health check failed: {e}")
            return False

    async def get_ai_trend_analysis(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Sends a prompt to OmniRoute for trend analysis."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": "omniroute-v1",
            "messages": [
                {"role": "system", "content": "You are a fashion trend analyst. Always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self.headers, json=payload)
                response.raise_for_status()
                data = response.json()
                content_str = data["choices"][0]["message"]["content"]
                return json.loads(content_str)
        except Exception as e:
            logger.error(f"OmniRoute AI analysis failed: {e}")
            return None

    async def optimize_seo(self, product_data: Dict[str, Any]) -> Dict[str, str]:
        """Optimizes SEO titles and descriptions using multimodal context."""
        prompt = f"Optimize SEO for this product: {json.dumps(product_data)}. Provide 'title' and 'description'."
        result = await self.get_ai_trend_analysis(prompt)
        if result:
            return {
                "title": result.get("title", product_data.get("title", "")),
                "description": result.get("description", product_data.get("description", ""))
            }
        return {"title": product_data.get("title", ""), "description": product_data.get("description", "")}
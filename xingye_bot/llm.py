from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx

from .settings import DATA_DIR, BotSettings, estimate_model_price
from .state import BotState


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, settings: BotSettings, state: BotState):
        self.settings = settings
        self.state = state

    def _models_for_role(self, model_role: str) -> list[str]:
        provider = self.settings.provider_for_role(model_role)
        primary = provider.get("model") or self.settings.models.get(model_role) or self.settings.models.get("chat")
        fallback = self.settings.fallback_models.get(model_role) or self.settings.fallback_models.get("chat")
        seen: set[str] = set()
        models: list[str] = []
        for model in (primary, fallback):
            if model and model not in seen:
                models.append(model)
                seen.add(model)
        return models

    def _provider_for_role(self, model_role: str, model: str | None = None) -> dict[str, str]:
        provider = self.settings.provider_for_role(model_role)
        if model:
            provider = {**provider, "model": model}
        return provider

    def _ensure_provider_configured(self, provider: dict[str, str], role: str) -> None:
        if not (provider.get("api_key") and provider.get("base_url") and provider.get("model")):
            raise ModelError(f"{role} AI 接口未配置，请设置对应 Provider 或统一 API。")

    async def chat(self, messages: list[dict[str, Any]], model_role: str = "chat", purpose: str = "chat") -> str:
        errors: list[str] = []
        for model in self._models_for_role(model_role):
            provider = self._provider_for_role(model_role, model)
            self._ensure_provider_configured(provider, model_role)
            try:
                return await self._chat_once(provider, messages, purpose)
            except ModelError as exc:
                errors.append(f"{model}: {exc}")
        raise ModelError("；".join(errors) or "没有可用模型")

    async def _chat_once(self, provider: dict[str, str], messages: list[dict[str, Any]], purpose: str) -> str:
        model = provider["model"]
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
        }
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ModelError(f"模型请求失败：HTTP {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"模型返回格式异常：{json.dumps(data, ensure_ascii=False)[:500]}") from exc
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) for part in content)
        content = (content or "").strip()
        if not content:
            raise ModelError(f"模型返回空内容。model={model}")

        self.state.record_cost(model, estimate_model_price(model, purpose), purpose)
        return content

    async def test(self, model_role: str = "chat") -> dict[str, Any]:
        models = self._models_for_role(model_role)
        if not models:
            raise ModelError(f"{model_role} 没有配置模型")
        provider = self._provider_for_role(model_role, models[0])
        self._ensure_provider_configured(provider, model_role)
        content = await self._chat_once(
            provider,
            [{"role": "user", "content": "请只回复 OK，用于测试模型连接。"}],
            f"model-test:{model_role}",
        )
        return {"role": model_role, "model": models[0], "reply": content}

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> dict[str, Any]:
        model = self._models_for_role("image")[0]
        provider = self._provider_for_role("image", model)
        self._ensure_provider_configured(provider, "image")
        url = provider["base_url"].rstrip("/") + "/images/generations"
        headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
        payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ModelError(f"图片生成失败：HTTP {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        item = (data.get("data") or [{}])[0]
        self.state.record_cost(model, estimate_model_price(model, "image-generation"), "image-generation")
        if item.get("url"):
            return {"model": model, "url": item["url"], "path": ""}
        if item.get("b64_json"):
            out_dir = DATA_DIR / "generated_images"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{uuid.uuid4().hex}.png"
            path.write_bytes(base64.b64decode(item["b64_json"]))
            return {"model": model, "url": "", "path": str(path)}
        raise ModelError(f"图片返回格式异常：{json.dumps(data, ensure_ascii=False)[:500]}")

    async def embedding(self, text: str) -> list[float]:
        model = self._models_for_role("embedding")[0]
        provider = self._provider_for_role("embedding", model)
        self._ensure_provider_configured(provider, "embedding")
        url = provider["base_url"].rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
        payload = {"model": model, "input": text[:8000]}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ModelError(f"Embedding 请求失败：HTTP {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Embedding 返回格式异常：{json.dumps(data, ensure_ascii=False)[:500]}") from exc
        self.state.record_cost(model, estimate_model_price(model, "embedding"), "embedding")
        return [float(x) for x in vector]

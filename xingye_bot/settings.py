from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "Data"
CONFIG_FILE = DATA_DIR / "config.json"


DEFAULT_MODELS = {
    "chat": "gpt-4.1-mini",
    "vision": "gpt-4.1-mini",
    "image": "gpt-image-1",
    "fast": "gpt-4.1-nano",
    "embedding": "text-embedding-3-small",
}


DEFAULT_FALLBACK_MODELS = {
    "chat": "gpt-4.1-nano",
    "vision": "gpt-4.1-mini",
    "image": "gpt-image-1",
    "fast": "gpt-4.1-nano",
    "embedding": "text-embedding-3-small",
}


MODEL_ROLES = ("chat", "vision", "image", "fast", "embedding")


MODEL_PRICES = {
    "gpt-4.1-mini": 0.0008,
    "gpt-4.1-nano": 0.0002,
    "gpt-image-1": 0.04,
    "text-embedding-3-small": 0.00002,
}

ROLE_PRICE_ESTIMATES = {
    "chat": 0.0008,
    "vision": 0.0012,
    "fast": 0.0002,
    "embedding": 0.00002,
    "image": 0.04,
}


def estimate_model_price(model: str, purpose: str = "") -> float:
    """Return a conservative per-call estimate when exact token accounting is unavailable."""
    model_key = str(model or "")
    if model_key in MODEL_PRICES:
        return MODEL_PRICES[model_key]
    purpose_key = str(purpose or "").lower()
    if "embedding" in purpose_key:
        return ROLE_PRICE_ESTIMATES["embedding"]
    if "image" in purpose_key:
        return ROLE_PRICE_ESTIMATES["image"]
    if "vision" in purpose_key or "frame" in purpose_key:
        return ROLE_PRICE_ESTIMATES["vision"]
    if "fast" in purpose_key:
        return ROLE_PRICE_ESTIMATES["fast"]
    return ROLE_PRICE_ESTIMATES["chat"]


def _public_provider(provider: dict[str, str]) -> dict[str, Any]:
    return {
        "configured": bool(provider.get("api_key") and provider.get("base_url") and provider.get("model")),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", ""),
    }


@dataclass
class BotSettings:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    models: dict[str, str] = field(default_factory=lambda: DEFAULT_MODELS.copy())
    fallback_models: dict[str, str] = field(default_factory=lambda: DEFAULT_FALLBACK_MODELS.copy())
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    panel_password: str = ""
    owner_mid: str = ""
    max_daily_actions: int = 20
    dry_run: bool = True
    allow_comment: bool = False
    allow_like: bool = False
    allow_coin: bool = False
    allow_favorite: bool = False
    allow_dynamic: bool = False
    video_mode: str = "smart"
    video_max_duration_seconds: int = 900
    video_frame_count: int = 12
    video_download_interest_threshold: float = 7.0
    video_download_dir: str = ""
    video_delete_after_understand: bool = True
    ai_marker: str = "（内容由AI生成并由AI回复）"
    enable_web_search: bool = True
    enable_proactive: bool = True
    enable_dynamic: bool = True
    enable_personality_evolution: bool = True
    enable_mood: bool = True
    enable_affection: bool = True
    enable_embedding_memory: bool = True
    comment_poll_interval: int = 300
    max_replies_per_check: int = 3
    proactive_video_count: int = 3
    proactive_comment_count: int = 2
    proactive_times_count: int = 2
    sleep_start: str = "02:00"
    sleep_end: str = "08:00"

    @property
    def configured(self) -> bool:
        provider = self.provider_for_role("chat")
        return bool(provider["api_key"] and provider["base_url"] and provider["model"])

    def provider_for_role(self, role: str) -> dict[str, str]:
        role = role if role in MODEL_ROLES else "chat"
        provider = _dict(self.providers.get(role))
        model = str(provider.get("model") or self.models.get(role) or self.models.get("chat") or "").strip()
        return {
            "api_key": str(provider.get("api_key") or self.api_key or "").strip(),
            "base_url": str(provider.get("base_url") or self.base_url or "").strip(),
            "model": model,
        }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_runtime_config() -> dict[str, Any]:
    DATA_DIR.mkdir(exist_ok=True)
    return _load_json(CONFIG_FILE)


def write_runtime_config(data: dict[str, Any]) -> None:
    _atomic_write_json(CONFIG_FILE, data)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.write("\n")
        temp_name = tmp.name
    Path(temp_name).replace(path)


def _deep_update(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def update_runtime_config(patch: dict[str, Any]) -> dict[str, Any]:
    data = read_runtime_config()
    _deep_update(data, patch)
    write_runtime_config(data)
    return data


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "enabled", "是", "开"}
    return bool(value)


def _int(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _float(value: Any, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _configured_model_for_role(role: str, api: dict[str, Any], raw_models: dict[str, Any]) -> str:
    value = raw_models.get(role, "")
    if value:
        return str(value).strip()
    if role in {"chat", "fast"} and api.get("model_brain"):
        return str(api["model_brain"]).strip()
    if role == "vision" and api.get("model_vision"):
        return str(api["model_vision"]).strip()
    return ""


def _role_provider_from_config(
    role: str,
    raw_providers: dict[str, Any],
    api_key: str,
    base_url: str,
    models: dict[str, str],
    configured_models: dict[str, str],
) -> dict[str, str]:
    provider = _dict(raw_providers.get(role))
    role_prefix = f"BILI_AI_{role.upper()}_"
    return {
        "api_key": provider.get("api_key", "") or _env(role_prefix + "API_KEY") or "",
        "base_url": provider.get("base_url", "") or _env(role_prefix + "BASE_URL") or "",
        "model": (
            provider.get("model", "")
            or configured_models.get(role, "")
            or _env(role_prefix + "MODEL")
            or _env(f"BILI_AI_MODEL_{role.upper()}")
            or models.get(role, "")
            or ""
        ),
    }


def load_settings() -> BotSettings:
    DATA_DIR.mkdir(exist_ok=True)
    raw = read_runtime_config()
    api = _dict(raw.get("api"))
    automation = _dict(raw.get("automation"))
    web = _dict(raw.get("web"))
    video = _dict(raw.get("video"))
    behavior = _dict(raw.get("behavior"))

    raw_models = _dict(raw.get("models"))
    models = DEFAULT_MODELS.copy()
    if api.get("model_brain"):
        models["chat"] = api["model_brain"]
        models["fast"] = api["model_brain"]
    if api.get("model_vision"):
        models["vision"] = api["model_vision"]
    models.update(raw_models)

    fallback_models = DEFAULT_FALLBACK_MODELS.copy()
    if api.get("model_brain"):
        fallback_models["chat"] = api["model_brain"]
        fallback_models["fast"] = api["model_brain"]
    if api.get("model_vision"):
        fallback_models["vision"] = api["model_vision"]
    fallback_models.update(_dict(raw.get("fallback_models")))

    video_mode = str(video.get("mode", "smart")).strip().lower()
    if video_mode not in {"subtitle", "frames", "hybrid", "smart"}:
        video_mode = "smart"

    configured_models = {role: _configured_model_for_role(role, api, raw_models) for role in MODEL_ROLES}
    model_overrides = {
        "chat": configured_models["chat"] or _env("BILI_AI_MODEL_CHAT") or DEFAULT_MODELS["chat"],
        "vision": configured_models["vision"] or _env("BILI_AI_MODEL_VISION") or DEFAULT_MODELS["vision"],
        "image": configured_models["image"] or _env("BILI_AI_MODEL_IMAGE") or DEFAULT_MODELS["image"],
        "fast": configured_models["fast"] or _env("BILI_AI_MODEL_FAST") or DEFAULT_MODELS["fast"],
        "embedding": configured_models["embedding"] or _env("BILI_AI_MODEL_EMBEDDING") or DEFAULT_MODELS["embedding"],
    }
    api_key = api.get("unified_api_key", "") or _env("BILI_AI_API_KEY")
    base_url = api.get("unified_base_url") or _env("BILI_AI_BASE_URL") or "https://api.openai.com/v1"
    raw_providers = _dict(raw.get("providers"))
    providers = {
        role: _role_provider_from_config(role, raw_providers, api_key, base_url, model_overrides, configured_models)
        for role in MODEL_ROLES
    }

    return BotSettings(
        api_key=api_key,
        base_url=base_url,
        models=model_overrides,
        fallback_models={
            "chat": os.getenv("BILI_AI_MODEL_CHAT_FALLBACK") or fallback_models["chat"],
            "vision": os.getenv("BILI_AI_MODEL_VISION_FALLBACK") or fallback_models["vision"],
            "image": os.getenv("BILI_AI_MODEL_IMAGE_FALLBACK") or fallback_models["image"],
            "fast": os.getenv("BILI_AI_MODEL_FAST_FALLBACK") or fallback_models["fast"],
            "embedding": os.getenv("BILI_AI_MODEL_EMBEDDING_FALLBACK") or fallback_models.get("embedding", DEFAULT_FALLBACK_MODELS["embedding"]),
        },
        providers=providers,
        panel_password=os.getenv("BILI_LEARNING_PANEL_PASSWORD") or web.get("password", ""),
        owner_mid=str(_dict(raw.get("bilibili")).get("owner_mid", "")),
        max_daily_actions=_int(automation.get("max_daily_actions"), 20, 0, 1000),
        dry_run=_bool(automation.get("dry_run"), True),
        allow_comment=_bool(automation.get("allow_comment")),
        allow_like=_bool(automation.get("allow_like")),
        allow_coin=_bool(automation.get("allow_coin")),
        allow_favorite=_bool(automation.get("allow_favorite")),
        allow_dynamic=_bool(automation.get("allow_dynamic")),
        video_mode=video_mode,
        video_max_duration_seconds=_int(video.get("max_duration_seconds"), 900, 1, 24 * 3600),
        video_frame_count=_int(video.get("frame_count"), 12, 1, 60),
        video_download_interest_threshold=_float(video.get("download_interest_threshold"), 7.0, 0.0, 10.0),
        video_download_dir=str(video.get("download_dir", "")).strip(),
        video_delete_after_understand=_bool(video.get("delete_video_after_understand"), True),
        ai_marker=os.getenv("BILI_AI_MARKER") or str(behavior.get("ai_marker", "（内容由AI生成并由AI回复）")),
        enable_web_search=_bool(automation.get("enable_web_search"), True),
        enable_proactive=_bool(automation.get("enable_proactive"), True),
        enable_dynamic=_bool(automation.get("enable_dynamic"), True),
        enable_personality_evolution=_bool(automation.get("enable_personality_evolution"), True),
        enable_mood=_bool(automation.get("enable_mood"), True),
        enable_affection=_bool(automation.get("enable_affection"), True),
        enable_embedding_memory=_bool(automation.get("enable_embedding_memory"), True),
        comment_poll_interval=_int(automation.get("comment_poll_interval"), 300, 10, 24 * 3600),
        max_replies_per_check=_int(automation.get("max_replies_per_check"), 3, 0, 100),
        proactive_video_count=_int(automation.get("proactive_video_count"), 3, 0, 100),
        proactive_comment_count=_int(automation.get("proactive_comment_count"), 2, 0, 100),
        proactive_times_count=_int(automation.get("proactive_times_count"), 2, 0, 24),
        sleep_start=str(automation.get("sleep_start", "02:00")),
        sleep_end=str(automation.get("sleep_end", "08:00")),
    )


def public_config(settings: BotSettings) -> dict[str, Any]:
    return {
        "base_url": settings.base_url,
        "configured": settings.configured,
        "models": settings.models,
        "fallback_models": settings.fallback_models,
        "providers": {role: _public_provider(settings.provider_for_role(role)) for role in MODEL_ROLES},
        "auth_required": bool(settings.panel_password),
        "owner_mid": settings.owner_mid,
        "dry_run": settings.dry_run,
        "permissions": {
            "comment": settings.allow_comment,
            "like": settings.allow_like,
            "coin": settings.allow_coin,
            "favorite": settings.allow_favorite,
            "dynamic": settings.allow_dynamic,
        },
        "video": {
            "mode": settings.video_mode,
            "max_duration_seconds": settings.video_max_duration_seconds,
            "frame_count": settings.video_frame_count,
            "download_interest_threshold": settings.video_download_interest_threshold,
            "download_dir": settings.video_download_dir,
            "delete_video_after_understand": settings.video_delete_after_understand,
        },
        "proactive": {
            "video_count": settings.proactive_video_count,
            "comment_count": settings.proactive_comment_count,
            "times_count": settings.proactive_times_count,
            "sleep_start": settings.sleep_start,
            "sleep_end": settings.sleep_end,
        },
        "features": {
            "web_search": settings.enable_web_search,
            "proactive": settings.enable_proactive,
            "dynamic": settings.enable_dynamic,
            "personality_evolution": settings.enable_personality_evolution,
            "mood": settings.enable_mood,
            "affection": settings.enable_affection,
            "embedding_memory": settings.enable_embedding_memory,
            "comment_poll_interval": settings.comment_poll_interval,
            "max_replies_per_check": settings.max_replies_per_check,
        },
        "ai_marker": settings.ai_marker,
    }

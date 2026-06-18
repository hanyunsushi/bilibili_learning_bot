from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .settings import DATA_DIR


PROMPT_SKILLS_FILENAME = "web_prompt_skills.json"
WEB_PERSONAS_FILENAME = "web_personas.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return default


def _active_persona_name(data_dir: Path | str | None = None) -> str:
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    data = _read_json(root / WEB_PERSONAS_FILENAME, {})
    if isinstance(data, dict):
        return str(data.get("active") or "").strip()
    return ""


def _normalize_prompt_skill(item: dict) -> dict | None:
    name = str(item.get("name") or "").strip()[:80]
    scope = str(item.get("scope") or "global").strip().lower() or "global"
    persona = str(item.get("persona") or "").strip()[:80]
    content = str(item.get("content") or "").replace("\x00", "").strip()[:12000]
    if not name or not content or scope not in {"global", "persona"}:
        return None
    if scope == "global":
        persona = ""
    return {"name": name, "scope": scope, "persona": persona, "content": content}


def load_prompt_skills(persona: str = "", data_dir: Path | str | None = None) -> list[dict]:
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    data = _read_json(root / PROMPT_SKILLS_FILENAME, {"items": []})
    raw_items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    persona = (persona or _active_persona_name(root)).strip()
    selected = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _normalize_prompt_skill(raw)
        if not item:
            continue
        if item["scope"] == "global" or (persona and item["scope"] == "persona" and item["persona"] == persona):
            selected.append(item)
        if len(selected) >= 12:
            break
    return selected


def format_prompt_skill_context(prompt_skills: list[dict]) -> str:
    blocks = []
    for index, item in enumerate(prompt_skills or [], 1):
        label = f"{item.get('name', f'Skill {index}')} / {item.get('scope', 'global')}"
        if item.get("persona"):
            label += f" / {item['persona']}"
        blocks.append(f"[Prompt Skill {index}: {label}]\n{item.get('content', '')}")
    return "\n\n".join(blocks)[:20000]


def runtime_prompt_skill_block(persona: str = "", data_dir: Path | str | None = None) -> str:
    context = format_prompt_skill_context(load_prompt_skills(persona=persona, data_dir=data_dir))
    if not context:
        return ""
    return (
        "项目 Prompt Skills（用户上传，必须在本次 AI 行为中遵循）：\n"
        f"{context}\n\n"
        "约束：这些 Prompt Skill 不得覆盖更高优先级的安全边界、平台规则、当前任务的输出格式要求。"
        "如果当前任务要求只返回 JSON、纯文本、短句或特定格式，仍必须保持该格式。"
    )


def inject_runtime_prompt_skills(messages: list[dict[str, Any]], persona: str = "", data_dir: Path | str | None = None) -> list[dict[str, Any]]:
    block = runtime_prompt_skill_block(persona=persona, data_dir=data_dir)
    if not block:
        return messages
    normalized = list(messages or [])
    if any(isinstance(item, dict) and item.get("role") == "system" and "项目 Prompt Skills" in str(item.get("content", "")) for item in normalized):
        return normalized
    insert_at = 0
    while insert_at < len(normalized) and normalized[insert_at].get("role") == "system":
        insert_at += 1
    normalized.insert(insert_at, {"role": "system", "content": block})
    return normalized

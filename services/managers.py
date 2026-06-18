"""services/managers.py — 管理类（Persona/Mood/UserProfile/BotDiary/SelfEvolution/PrivateContext）

每个类通过 __init__(config) 接收配置，使用 core.config 中的路径常量。
"""
import os, json, random, time
from datetime import datetime, timedelta
from colorama import Fore, Style
from core.config import (
    PERSONAS_FILE, MOOD_STATE_FILE, USER_PROFILES_FILE,
    BOT_DIARY_FILE, SELF_EVOLUTION_FILE, PRIVATE_CONTEXT_FILE,
    AGENT_SKILL_LOG_FILE, load_json_file, save_json_file, config as _global_config
)


# ===== PrivateContextDB =====

class PrivateContextDB:
    """私信上下文数据库 - 每个用户的对话历史管理"""

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = PRIVATE_CONTEXT_FILE
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def get_context(self, user_id: str, max_messages: int = 20) -> list:
        ctx = self.data.get(user_id, [])
        return ctx[-max_messages:] if ctx else []

    def add_message(self, user_id: str, role: str, content: str):
        if user_id not in self.data:
            self.data[user_id] = []
        self.data[user_id].append({
            "role": role, "content": content,
            "time": datetime.now().isoformat()
        })
        self._save()

    def clear_context(self, user_id: str):
        if user_id in self.data:
            del self.data[user_id]
            self._save()

    def get_or_create(self, user_id: str) -> list:
        if user_id not in self.data:
            self.data[user_id] = []
            self._save()
        return self.data[user_id]


# ===== PersonaManager =====

class PersonaManager:
    """人格管理器 - 管理不同的人格设定与当前激活人格"""

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = PERSONAS_FILE
        self.data = self._load()
        self.config = self._cfg

    def _load(self):
        return load_json_file(PERSONAS_FILE, {})

    def _save(self):
        return save_json_file(self.file_path, self.data)

    def _default_data(self):
        active = (self.config.get("persona", {}).get("active_persona", "默认人格")
                  if self.config else "默认人格")
        return {
            "active_persona": active,
            "personas": {
                "默认人格": {
                    "name": "AI小助手",
                    "greeting": "你好！我是你的AI小助手~",
                    "style": "热情、专业",
                    "system_prompt": ""
                }
            }
        }

    def get_active_persona(self) -> str:
        return self.data.get("active_persona", "默认人格")

    def set_active_persona(self, name: str):
        self.data["active_persona"] = name
        self._save()

    def get_persona(self, name: str = None) -> dict:
        name = name or self.get_active_persona()
        return self.data.get("personas", {}).get(name, {})

    def list_personas(self) -> list:
        return list(self.data.get("personas", {}).keys())

    def add_persona(self, name: str, info: dict):
        if "personas" not in self.data:
            self.data["personas"] = {}
        self.data["personas"][name] = info
        self._save()

    def delete_persona(self, name: str):
        if name in self.data.get("personas", {}):
            del self.data["personas"][name]
            if self.data.get("active_persona") == name:
                remaining = list(self.data["personas"].keys())
                self.data["active_persona"] = remaining[0] if remaining else "默认人格"
            self._save()

    def get_prompt_name(self) -> str:
        p = self.get_persona()
        return p.get("name", "AI小助手")

    def get_greeting(self) -> str:
        p = self.get_persona()
        return p.get("greeting", "你好！")

    def get_style(self) -> str:
        p = self.get_persona()
        return p.get("style", "热情、专业")

    def get_system_prompt(self) -> str:
        p = self.get_persona()
        return p.get("system_prompt", "")

    def build_prompt_block(self) -> str:
        """构建用于 prompt 的人格描述块"""
        p = self.get_persona()
        name = p.get("name", "AI小助手")
        style = p.get("style", "热情、专业")
        sp = p.get("system_prompt", "")
        lines = [f"【当前人格】{name}", f"风格: {style}"]
        if sp:
            lines.append(sp)
        return "\n".join(lines)

    def evolve_active_persona(self, style_delta: str = "", relationship_delta: str = "", new_rule: str = "") -> dict:
        """Apply a conservative evolution note to the active persona."""
        name = self.get_active_persona()
        self.data.setdefault("personas", {})
        persona = self.data["personas"].setdefault(name, {
            "name": "AI小助手",
            "greeting": "你好！我是你的AI小助手~",
            "style": "热情、专业",
            "system_prompt": "",
        })
        history_item = {
            "time": datetime.now().isoformat(),
            "style_delta": style_delta,
            "relationship_delta": relationship_delta,
            "new_rule": new_rule,
        }
        persona.setdefault("evolution_history", []).append(history_item)
        persona["evolution_history"] = persona["evolution_history"][-50:]
        if style_delta:
            persona["style_delta"] = style_delta
        if relationship_delta:
            persona["relationship_delta"] = relationship_delta
        if new_rule:
            rules = persona.setdefault("rules", [])
            if new_rule not in rules:
                rules.append(new_rule)
        self._save()
        return persona

    def recheck(self):
        self.data = self._load()


# ===== MoodManager =====

class MoodManager:
    """心情系统 - 根据互动结果动态调整当前心情"""

    ALL_MOODS = ["兴奋", "愉快", "平静", "好奇", "慵懒", "深沉",
                 "调皮", "温柔", "毒舌", "学究", "中二", "佛系", "热血"]

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = MOOD_STATE_FILE
        self.config = self._cfg
        self.data = self._load()

    def _load(self):
        return load_json_file(MOOD_STATE_FILE, {})

    def _save(self):
        return save_json_file(self.file_path, self.data)

    def _default_data(self):
        return {
            "current": (self.config.get("mood", {}).get("default_mood", "平静")
                       if self.config else "平静"),
            "volatility": (self.config.get("mood", {}).get("mood_volatility", 1.0)
                          if self.config else 1.0),
            "history": []
        }

    def get_current(self) -> str:
        return self.data.get("current", "平静")

    def set_mood(self, mood: str):
        if mood in self.ALL_MOODS:
            self.data["current"] = mood
            self.data.setdefault("history", []).append({
                "mood": mood, "time": datetime.now().isoformat()
            })
            self._save()
            return True
        return False

    def get_random_mood(self) -> str:
        return random.choice(self.ALL_MOODS)

    def get_style_modifier(self) -> str:
        mood = self.get_current()
        modifiers = {
            "兴奋": "语气非常兴奋，多用感叹号和表情符号",
            "愉快": "语气轻松愉快，带微笑",
            "平静": "语气平稳、理性",
            "好奇": "充满好奇心，多提问",
            "慵懒": "语气慵懒随意，有点不正经",
            "深沉": "语气深沉，有哲理性",
            "调皮": "语气调皮，爱开玩笑",
            "温柔": "语气温柔亲切",
            "毒舌": "毒舌模式，犀利幽默",
            "学究": "学究气，喜欢引经据典",
            "中二": "中二病模式，热血夸张",
            "佛系": "佛系模式，随缘淡然",
            "热血": "热血沸腾，充满激情"
        }
        return modifiers.get(mood, "语气平稳正常")

    def build_prompt_block(self) -> str:
        """构建用于 prompt 的心情描述块"""
        mood = self.get_current()
        modifier = self.get_style_modifier()
        return f"【当前心情】{mood}\n语气修饰: {modifier}"

    def shift(self, reason: str, delta: int):
        """根据事件偏移心情值。delta 为整数，正=上扬，负=下滑。
        心情按 ALL_MOODS 顺序从 0~12 编号，delta 会被 volatility 缩放。
        """
        mood = self.get_current()
        try:
            idx = self.ALL_MOODS.index(mood)
        except ValueError:
            idx = self.ALL_MOODS.index("平静")
        vol = float(self.data.get("volatility", 1.0))
        new_idx = max(0, min(len(self.ALL_MOODS) - 1, idx + int(round(delta * vol))))
        new_mood = self.ALL_MOODS[new_idx]
        if new_mood != mood:
            self.data["current"] = new_mood
            self.data.setdefault("history", []).append({
                "mood": new_mood, "time": datetime.now().isoformat(),
                "reason": reason, "delta": delta, "from": mood
            })
            self._save()

    def recheck(self):
        self.data = self._load()


# ===== UserProfileManager =====

class UserProfileManager:
    """用户档案与好感度系统"""

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = USER_PROFILES_FILE
        self.data = self._load()

    def _load(self):
        return load_json_file(USER_PROFILES_FILE, {})

    def _save(self):
        return save_json_file(self.file_path, self.data)

    def get_profile(self, user_id: str) -> dict:
        return self.data.get(user_id, {})

    def update_profile(self, user_id: str, updates: dict):
        if user_id not in self.data:
            self.data[user_id] = {
                "first_seen": datetime.now().isoformat(),
                "interactions": 0, "affinity": 0.0
            }
        self.data[user_id].update(updates)
        self.data[user_id]["interactions"] = self.data[user_id].get("interactions", 0) + 1
        self.data[user_id]["last_seen"] = datetime.now().isoformat()
        self._save()

    def get_affinity(self, user_id: str) -> float:
        return self.data.get(user_id, {}).get("affinity", 0.0)

    def add_affinity(self, user_id: str, delta: float):
        prof = self.get_profile(user_id)
        new_val = max(-1.0, min(1.0, prof.get("affinity", 0.0) + delta))
        self.update_profile(user_id, {"affinity": new_val})

    def adjust_affinity(self, user_id: str, user_name: str, delta: float, reason: str = "") -> dict:
        prof = self.get_profile(user_id)
        old_val = float(prof.get("affinity", 0.0)) if prof else 0.0
        new_val = max(-1.0, min(1.0, old_val + (float(delta) / 10.0)))
        self.update_profile(user_id, {
            "name": user_name or prof.get("name") or user_id,
            "affinity": new_val,
            "last_reason": reason,
        })
        return self.get_profile(user_id)

    def update_impression(self, user_id: str, user_name: str, impression: str) -> dict:
        """记录对用户的印象/评价"""
        prof = self.get_profile(user_id)
        if not prof:
            self.update_profile(user_id, {"name": user_name})
            prof = self.get_profile(user_id)
        if impression:
            prof["impression"] = impression[:120]
            self._save()
        return prof

    def get_all_users(self) -> list:
        return list(self.data.keys())

    def build_prompt_block(self, user_id: str, user_name: str = None) -> str:
        """构建用于 prompt 的用户档案描述块"""
        prof = self.get_profile(user_id)
        if not prof:
            return f"【用户档案】{user_name or user_id}: 新用户，尚无互动记录"
        affinity = prof.get("affinity", 0.0)
        interactions = prof.get("interactions", 0)
        first_seen = prof.get("first_seen", "未知")
        lines = [f"【用户档案】{user_name or user_id}: 好感度={affinity:.2f}, 互动次数={interactions}, 首次见面={first_seen}"]
        return "\n".join(lines)

    def recheck(self):
        self.data = self._load()


# ===== BotDiaryManager =====

class BotDiaryManager:
    """机器人日记 - 保存人工日记和自动复盘日记"""

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = BOT_DIARY_FILE
        self.data = self._load()

    def _load(self):
        return load_json_file(BOT_DIARY_FILE, {"diaries": []})

    def _save(self):
        return save_json_file(self.file_path, self.data)

    def add_entry(self, content: str, entry_type: str = "auto"):
        entry = {
            "type": entry_type, "content": content,
            "time": datetime.now().isoformat()
        }
        self.data.setdefault("diaries", []).append(entry)
        self._save()
        return entry

    def get_entries(self, limit: int = 20, entry_type: str = None) -> list:
        entries = self.data.get("diaries", [])
        if entry_type:
            entries = [e for e in entries if e.get("type") == entry_type]
        return entries[-limit:]

    def list_entries(self, limit: int = 20, entry_type: str = None) -> list:
        return self.get_entries(limit=limit, entry_type=entry_type)

    async def generate_from_events(self, events: list, persona_block: str = "", mood: str = "", extra_note: str = "") -> dict:
        recent = list(events or [])[-8:]
        lines = []
        for event in recent:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type", "event")
            title = event.get("title") or event.get("goal") or event.get("action") or "未命名事件"
            score = event.get("score")
            actions = event.get("actions") or event.get("action") or ""
            detail = f"- {event_type}: {title}"
            if score not in (None, ""):
                detail += f" | 评分 {score}"
            if actions:
                detail += f" | 操作 {actions}"
            lines.append(detail)
        if not lines:
            lines.append("- 暂无足够事件，仅记录当前状态。")
        title = f"自动日记 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        content_parts = [f"# {title}", f"当前心情：{mood or '未知'}"]
        if persona_block:
            content_parts.append(str(persona_block).strip())
        content_parts.append("## 近期事件")
        content_parts.extend(lines)
        if extra_note:
            content_parts.extend(["## 额外备注", str(extra_note).strip()])
        entry = {
            "id": f"diary-{int(time.time() * 1000)}",
            "title": title,
            "type": "auto",
            "content": "\n".join(content_parts),
            "time": datetime.now().isoformat(),
            "event_count": len(recent),
        }
        self.data.setdefault("diaries", []).append(entry)
        self._save()
        return entry

    def get_recent_summary(self, count: int = 5) -> str:
        entries = self.get_entries(count)
        if not entries:
            return "暂无日记记录"
        return "\n---\n".join(e.get("content", "") for e in entries)

    def recheck(self):
        self.data = self._load()


# ===== SelfEvolutionManager =====

class SelfEvolutionManager:
    """自我进化 - 根据近期行为生成可控的人格微调建议"""

    def __init__(self, config: dict = None):
        self._cfg = config or _global_config
        self.file_path = SELF_EVOLUTION_FILE
        self.config = self._cfg
        self.data = self._load()

    def _load(self):
        return load_json_file(SELF_EVOLUTION_FILE, {"items": []})

    def _save(self):
        return save_json_file(self.file_path, self.data)

    def add_item(self, suggestion: str, category: str = "general"):
        item = {
            "id": f"evo-{int(time.time() * 1000)}",
            "category": category,
            "suggestion": suggestion,
            "time": datetime.now().isoformat(),
            "applied": False,
            "status": "pending",
        }
        self.data.setdefault("items", []).append(item)
        self._save()
        return item

    def get_items(self, limit: int = 20) -> list:
        return self.data.get("items", [])[-limit:]

    async def reflect(self, events: list, persona_block: str = "", mood: str = "", diary_entries: list | None = None) -> dict:
        recent = list(events or [])[-12:]
        diary_entries = list(diary_entries or [])[-5:]
        event_types = [str(item.get("type", "event")) for item in recent if isinstance(item, dict)]
        high_scores = [
            float(item.get("score"))
            for item in recent
            if isinstance(item, dict) and str(item.get("score", "")).replace(".", "", 1).isdigit()
        ]
        avg_score = round(sum(high_scores) / len(high_scores), 2) if high_scores else None
        reflection = f"近期处理了 {len(recent)} 个事件"
        if event_types:
            reflection += f"，主要包括 {', '.join(sorted(set(event_types))[:4])}"
        if avg_score is not None:
            reflection += f"，平均评分 {avg_score}"
        reflection += f"。当前心情为 {mood or '未知'}。"
        parsed = {
            "reflection": reflection,
            "style_delta": "保持当前表达风格，优先记录真实触发条件。",
            "relationship_delta": "",
            "new_rule": "遇到高价值内容时优先沉淀可复用知识。",
            "mood_delta": 0,
        }
        item = {
            "id": f"evo-{int(time.time() * 1000)}",
            "category": "auto_reflection",
            "suggestion": parsed["new_rule"],
            "parsed": parsed,
            "raw": reflection,
            "events_count": len(recent),
            "diary_count": len(diary_entries),
            "persona": persona_block,
            "mood": mood,
            "time": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
            "applied": False,
            "status": "pending",
        }
        self.data.setdefault("items", []).append(item)
        self._save()
        return item

    def clear_items(self):
        self.data["items"] = []
        self._save()

    def get_active_suggestions(self) -> list:
        return [i for i in self.data.get("items", [])
                if i.get("status", "pending") == "pending"]

    def mark_applied(self, index: int | str):
        items = self.data.get("items", [])
        target = None
        if isinstance(index, str):
            target = next((item for item in items if item.get("id") == index), None)
        elif isinstance(index, int) and 0 <= index < len(items):
            target = items[index]
        if target is not None:
            target["status"] = "applied"
            target["applied"] = True
            self._save()
            return True
        return False

    def recheck(self):
        self.data = self._load()

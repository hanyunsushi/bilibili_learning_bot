"""services/agent_service.py — Agent 技能调度与任务执行"""
import asyncio, json, os, random, re, time
from datetime import datetime
from colorama import Fore, Style
from core.config import (
    config as _global_config, MODEL_BRAIN, AGENT_SKILL_LOG_FILE,
    AGENT_DIVE_MAX_VIDEOS, AGENT_MAX_SEARCH_RESULTS, AGENT_MAX_STEPS_PER_PLAN,
    AGENT_MAX_VIDEOS_PER_PLAN, log
)


class AgentSkillRunner:
    """主动 Agent 技能执行器：规划、搜索视频、看视频、沉淀记忆。"""

    SKILL_FULL_PLAN = "full_plan"
    SKILL_SEARCH = "search_bilibili_videos"
    SKILL_WATCH = "watch_bilibili_videos"
    SKILL_MEMORY = "write_memory"

    def __init__(self, brain=None, credential=None, uid=0):
        self.brain = brain
        self.credential = credential or getattr(brain, "credential", None)
        self.uid = int(uid or getattr(getattr(brain, "bili", None), "uid", 0) or 0)
        self.goal_log = self._load_goal_log()

    def _load_goal_log(self):
        if os.path.exists(AGENT_SKILL_LOG_FILE):
            try:
                with open(AGENT_SKILL_LOG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                log(f"[WARN] Agent技能日志加载失败: {e}", "WARN")
        return []

    def _save_goal_log(self):
        try:
            with open(AGENT_SKILL_LOG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.goal_log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"保存Agent技能日志失败: {e}", "WARN")

    def _normalize_skill(self, skill: str = "") -> str:
        allowed = {
            self.SKILL_FULL_PLAN,
            self.SKILL_SEARCH,
            self.SKILL_WATCH,
            self.SKILL_MEMORY,
        }
        skill = (skill or self.SKILL_FULL_PLAN).strip()
        return skill if skill in allowed else self.SKILL_FULL_PLAN

    def _normalize_prompt_skills(self, prompt_skills=None) -> list:
        normalized = []
        for item in prompt_skills or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "").strip()
            if not name or not content:
                continue
            normalized.append({
                "name": name[:80],
                "scope": str(item.get("scope") or "global").strip() or "global",
                "persona": str(item.get("persona") or "").strip(),
                "content": content[:12000],
            })
        return normalized[:12]

    def _prompt_skill_names(self, prompt_skills=None) -> list:
        return [item.get("name", "") for item in prompt_skills or [] if item.get("name")]

    def _format_prompt_skill_context(self, prompt_skills=None) -> str:
        blocks = []
        for index, item in enumerate(prompt_skills or [], 1):
            name = item.get("name", f"Skill {index}")
            scope = item.get("scope", "global")
            persona = item.get("persona", "")
            label = f"{name} / {scope}" + (f" / {persona}" if persona else "")
            blocks.append(f"[Prompt Skill {index}: {label}]\n{item.get('content', '')}")
        return "\n\n".join(blocks)[:20000]

    def _apply_prompt_skill_to_query(self, goal: str, skill_context: str = "") -> str:
        compact = re.sub(r"\s+", " ", skill_context or "").strip()
        if not compact:
            return goal
        return f"{goal} 执行要求: {compact[:220]}"

    def _step_with_prompt_skills(self, step: dict, skill_context: str, prompt_skills=None) -> dict:
        if not skill_context:
            return step
        enriched = dict(step)
        enriched["skill_context"] = skill_context
        enriched["prompt_skill_names"] = self._prompt_skill_names(prompt_skills)
        return enriched

    async def plan_and_execute(self, goal: str, skill: str = SKILL_FULL_PLAN, prompt_skills=None):
        """规划并执行一个目标（内部用，返回 raw dict）"""
        skill = self._normalize_skill(skill)
        prompt_skills = self._normalize_prompt_skills(prompt_skills)
        skill_context = self._format_prompt_skill_context(prompt_skills)
        log(f"🤖 Agent开始规划: {goal} | skill={skill}", "INFO")
        plan = self._make_plan(goal, skill=skill, prompt_skills=prompt_skills)
        if not plan:
            return {"status": "no_plan", "goal": goal, "skill": skill, "prompt_skills": prompt_skills, "skill_context": skill_context}
        log(f"📋 Agent计划: {json.dumps(plan, ensure_ascii=False)[:200]}", "CONFIG")
        result = await self._execute_plan(plan)
        result["skill_context"] = skill_context
        self.goal_log.append({
            "goal": goal, "skill": skill, "prompt_skills": prompt_skills, "skill_context": skill_context, "plan": plan, "result": result,
            "created_at": datetime.now().isoformat(),
            "time": datetime.now().isoformat(),
        })
        self._save_goal_log()
        return result

    async def run_goal(self, goal: str, skill: str = SKILL_FULL_PLAN, prompt_skills=None):
        """[兼容接口] 执行一个Agent目标，返回 callers 期望的 {goal, results: [{step, result}, ...]} 格式"""
        skill = self._normalize_skill(skill)
        prompt_skills = self._normalize_prompt_skills(prompt_skills)
        skill_context = self._format_prompt_skill_context(prompt_skills)
        plan = self._make_plan(goal, skill=skill, prompt_skills=prompt_skills)
        if not plan:
            return {"goal": goal, "skill": skill, "prompt_skills": prompt_skills, "skill_context": skill_context, "results": [], "status": "no_plan"}

        log(f"📋 Agent计划: {json.dumps(plan, ensure_ascii=False)[:200]}", "CONFIG")

        results_list = []
        # 重置搜索缓存，确保 watch 步骤能拿到本轮搜索结果
        self._search_results = []

        for step in plan:
            action = step.get("action")
            step_context = step.get("skill_context", skill_context)
            prompt_skill_names = step.get("prompt_skill_names", self._prompt_skill_names(prompt_skills))
            step_info = {}
            step_result = {}

            if action == "search":
                query = step.get("query", goal)
                count = step.get("result_count", AGENT_MAX_SEARCH_RESULTS)
                step_info = {
                    "skill": "search_bilibili_videos",
                    "query": query,
                    "count": count,
                    "prompt_skill_count": len(prompt_skills),
                    "prompt_skill_names": prompt_skill_names,
                    "skill_context": step_context,
                }
                raw = await self._search_videos(query, count)
                if isinstance(raw, list):
                    self._search_results = raw  # 缓存供 watch 步骤使用
                    step_result = {"ok": True, "videos": raw, "count": len(raw)}
                else:
                    step_result = {"ok": False, "error": raw.get("error", "搜索失败"), "videos": []}

            elif action == "watch":
                max_v = step.get("max_videos", AGENT_MAX_VIDEOS_PER_PLAN)
                step_info = {
                    "skill": "watch_bilibili_videos",
                    "max_videos": max_v,
                    "prompt_skill_count": len(prompt_skills),
                    "prompt_skill_names": prompt_skill_names,
                    "skill_context": step_context,
                }
                raw = await self._watch_videos(max_v, skill_context=step_context)
                if raw.get("error"):
                    step_result = {"ok": False, "error": raw["error"], "watched": raw.get("videos", [])}
                else:
                    step_result = {"ok": True, "watched": raw.get("videos", []), "count": raw.get("watched", 0)}

            elif action == "summarize":
                step_info = {
                    "skill": "write_memory",
                    "prompt_skill_count": len(prompt_skills),
                    "prompt_skill_names": prompt_skill_names,
                    "skill_context": step_context,
                }
                raw = self._summarize(skill_context=step_context)
                step_result = {"ok": True, "summary": raw.get("summary", "")}

            else:
                step_info = {
                    "skill": action,
                    "prompt_skill_count": len(prompt_skills),
                    "prompt_skill_names": prompt_skill_names,
                    "skill_context": step_context,
                }
                step_result = {"ok": False, "error": f"未知动作: {action}"}

            results_list.append({"step": step_info, "result": step_result})

        # 写入日志
        self.goal_log.append({
            "goal": goal,
            "skill": skill,
            "prompt_skills": prompt_skills,
            "skill_context": skill_context,
            "plan": plan,
            "results": results_list,
            "created_at": datetime.now().isoformat(),
            "time": datetime.now().isoformat(),
        })
        self._save_goal_log()

        return {"goal": goal, "skill": skill, "prompt_skills": prompt_skills, "skill_context": skill_context, "results": results_list, "status": "completed"}

    def _make_plan(self, goal: str, skill: str = SKILL_FULL_PLAN, prompt_skills=None) -> list:
        cfg = _global_config.get("agent", {})
        max_steps = cfg.get("max_steps_per_plan", AGENT_MAX_STEPS_PER_PLAN)
        skill = self._normalize_skill(skill)
        prompt_skills = self._normalize_prompt_skills(prompt_skills)
        skill_context = self._format_prompt_skill_context(prompt_skills)
        search_step = self._step_with_prompt_skills({
            "action": "search",
            "query": self._apply_prompt_skill_to_query(goal, skill_context),
            "result_count": cfg.get("max_search_results", AGENT_MAX_SEARCH_RESULTS),
        }, skill_context, prompt_skills)
        watch_step = self._step_with_prompt_skills({
            "action": "watch",
            "max_videos": cfg.get("max_videos_per_plan", AGENT_MAX_VIDEOS_PER_PLAN),
        }, skill_context, prompt_skills)
        memory_step = self._step_with_prompt_skills({"action": "summarize"}, skill_context, prompt_skills)
        if skill == self.SKILL_SEARCH:
            plan = [search_step]
        elif skill == self.SKILL_WATCH:
            plan = [search_step, watch_step]
        elif skill == self.SKILL_MEMORY:
            plan = [memory_step]
        else:
            plan = [search_step, watch_step, memory_step]
        return plan[:max_steps]

    async def _execute_plan(self, plan: list) -> dict:
        """[内部] 原始执行（供 plan_and_execute 使用）"""
        results = {}
        for step in plan:
            action = step.get("action")
            if action == "search":
                query = step.get("query", "")
                count = step.get("result_count", 8)
                raw = await self._search_videos(query, count)
                if isinstance(raw, list):
                    self._search_results = raw
                results["search"] = raw
            elif action == "watch":
                max_v = step.get("max_videos", 5)
                results["watch"] = await self._watch_videos(max_v, skill_context=step.get("skill_context", ""))
            elif action == "summarize":
                results["summary"] = self._summarize(skill_context=step.get("skill_context", ""))
        return results

    async def _search_videos(self, query: str, count: int = 8):
        if not self.credential:
            return {"error": "No credential"}
        try:
            from bilibili_api import search as bili_search
            data = await bili_search.search_by_type(keyword=query, search_type=bili_search.SearchObjectType.VIDEO)
            items = data.get("result") or []
            return [{"title": re.sub(r"<.*?>", "", str(v.get("title", ""))), "bvid": v.get("bvid")}
                    for v in items[:count]]
        except Exception as e:
            return {"error": str(e)}

    async def _watch_videos(self, max_videos: int, skill_context: str = ""):
        if not self.brain:
            return {"error": "No brain"}
        watched = []
        results = self._search_results if hasattr(self, '_search_results') else []
        for item in results[:max_videos]:
            bvid = item.get("bvid")
            if bvid:
                watched.append({"bvid": bvid, "title": item.get("title", ""), "status": "watched", "skill_context": skill_context})
        return {"watched": len(watched), "videos": watched, "skill_context": skill_context}

    def _summarize(self, skill_context: str = ""):
        if skill_context:
            return {"status": "completed", "summary": f"Agent任务执行完成。已应用 Prompt Skill:\n{skill_context}"}
        return {"status": "completed", "summary": "Agent任务执行完成"}

    def list_runs(self, limit: int = 10):
        """返回最近的 Agent 运行记录"""
        log_list = self.goal_log or self._load_goal_log()
        return log_list[-limit:] if len(log_list) > limit else log_list

    def get_goal_log(self):
        return self.goal_log

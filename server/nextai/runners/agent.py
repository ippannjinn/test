"""Autonomous loop: plan → act → verify → fix → re-verify, with hard limits and loop detection."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from typing import Any

from ..jobs import Job
from ..models.manager import ModelUnavailable
from ..profile.engine import Profile
from ..resources.governor import Level
from ..tools import registry
from ..tools.registry import ToolContext, ToolResult
from .common import LLMResult, llm_call

PLAN_PROMPT = ("PLAN: 次の依頼を達成するための簡潔な計画を3〜7ステップの番号付きリストで作成してください。"
               "ツールが必要なステップにはツール名を書いてください。計画のみを出力してください。")
VERIFY_PROMPT = ("VERIFY: あなたは厳格なレビュアーです。ユーザーの依頼と回答案を比較してください。"
                 "依頼を満たしていれば1行目に PASS とだけ書いてください。不足・誤りがあれば1行目に FAIL と書き、"
                 "2行目以降に具体的な問題点を箇条書きしてください。")
TOOL_RESULT_WRAP = "<tool_result name=\"{name}\" ok=\"{ok}\">\n{content}\n</tool_result>"


class AgentRunner:
    def __init__(self, p: Any, job: Job, user: dict, profile: Profile, ctx: ToolContext):
        self.p, self.job, self.user, self.profile, self.ctx = p, job, user, profile, ctx
        self.tool_calls = 0
        self.tokens = 0
        self.started = time.time()
        self.escalated = False
        self.used_tools: list[str] = []

    def _set_profile(self, new: Profile) -> None:
        self.profile = new
        self.job.profile = new.to_dict()
        self.job.emit("profile", **new.summary())

    async def _llm(self, messages: list[dict], *, tools: bool, stream: bool = True, max_tokens: int | None = None) -> LLMResult:
        schemas = registry.schemas(self.profile.tools) if tools and self.tool_calls < self.profile.limits.get("max_tool_calls", 0) else None
        try:
            res = await llm_call(self.p, self.job, self.user, self.profile, messages, tools=schemas, stream=stream,
                                 max_tokens=max_tokens)
        except ModelUnavailable as e:
            new = self.p.profiles.reevaluate(self.profile, "model_unavailable", model_id=self.profile.model_id)
            if not new:
                raise
            self._set_profile(new)
            self.job.emit("notice", message=f"モデル切替: {e}")
            res = await llm_call(self.p, self.job, self.user, self.profile, messages, tools=schemas, stream=stream,
                                 max_tokens=max_tokens)
        self.tokens += int(res.usage.get("completion_tokens", 0) or 0) + int(res.usage.get("prompt_tokens", 0) or 0)
        return res

    def _limit_reason(self) -> str | None:
        lim = self.profile.limits
        if time.time() - self.started > lim.get("max_seconds", 600):
            return "最大実行時間に達しました"
        if self.tokens > lim.get("max_total_tokens", 60000):
            return "最大トークン数に達しました"
        return None

    async def _run_tools(self, calls: list[dict]) -> list[ToolResult]:
        sem = asyncio.Semaphore(max(1, self.profile.tool_parallelism))

        async def one(call: dict) -> ToolResult:
            async with sem:
                self.job.emit("tool_call", id=call["id"], name=call["name"], args=_short(call.get("arguments", "")))
                if call["name"] not in self.profile.tools:
                    # only the tools offered for this turn may run, whatever name the model produces
                    res = ToolResult(False, f"ツール {call['name']} はこの会話では使えません")
                else:
                    res = await registry.execute(self.ctx, call["name"], call.get("arguments") or "{}", self.job.emit)
                self.job.emit("tool_result", id=call["id"], name=call["name"], ok=res.ok, summary=res.content[:400])
                return res

        return list(await asyncio.gather(*(one(c) for c in calls)))

    async def run(self, messages: list[dict]) -> str:
        lim = self.profile.limits
        if self.profile.plan:
            self.job.emit("step", n=0, max=lim.get("max_steps"), phase="plan")
            plan = await self._llm(messages + [{"role": "system", "content": PLAN_PROMPT + " (計画)"}], tools=False,
                                   stream=False, max_tokens=700)
            if plan.content.strip():
                self.job.emit("plan", text=plan.content.strip())
                messages = messages + [{"role": "assistant", "content": "計画:\n" + plan.content.strip()},
                                       {"role": "user", "content": "この計画に沿って進めてください。必要に応じてツールを使い、最後に完成した回答を示してください。"}]
        seen: Counter[str] = Counter()
        failures, repeats, verify_rounds, step = 0, 0, 0, 0
        stop_reason: str | None = None
        final = ""
        while True:
            step += 1
            if step > lim.get("max_steps", 6):
                stop_reason = "最大ステップ数に達しました"
                break
            stop_reason = self._limit_reason()
            if stop_reason:
                break
            if self.p.governor.state.level >= Level.HIGH:
                new = self.p.profiles.reevaluate(self.profile, "pressure")
                if new:
                    self._set_profile(new)
                    lim = self.profile.limits
            self.job.emit("step", n=step, max=lim.get("max_steps"), phase="act")
            res = await self._llm(messages, tools=True)
            if res.tool_calls:
                if res.content:
                    self.job.emit("reset", moved=res.content[:2000])
                calls = res.tool_calls[: max(1, lim.get("max_tool_calls", 8) - self.tool_calls)]
                self.tool_calls += len(calls)
                messages.append({"role": "assistant", "content": res.content or "",
                                 "tool_calls": [{"id": c["id"], "type": "function",
                                                 "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"}}
                                                for c in calls]})
                results = await self._run_tools(calls)
                repeated = False
                for c, r in zip(calls, results):
                    self.used_tools.append(c["name"])
                    sig = hashlib.sha1(f"{c['name']}:{_norm_args(c.get('arguments'))}".encode()).hexdigest()
                    seen[sig] += 1
                    repeated |= seen[sig] >= 2
                    messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                                     "content": TOOL_RESULT_WRAP.format(name=c["name"], ok=r.ok, content=r.content[:12000])})
                failures = failures + 1 if all(not r.ok for r in results) else 0
                if failures >= lim.get("max_consecutive_failures", 3):
                    new = None if self.escalated else self.p.profiles.reevaluate(self.profile, "consecutive_failures")
                    if new is None:
                        stop_reason = "同じ失敗が続いたため安全に停止しました"
                        break
                    self.escalated = True
                    self._set_profile(new)
                    failures = 0
                    messages.append({"role": "user", "content": "これまでの方法は失敗しています。原因を考え、別のアプローチで進めてください。"})
                if repeated:
                    repeats += 1
                    if repeats >= 3:
                        stop_reason = "同じ操作の繰り返しを検出したため停止しました"
                        break
                    new = self.p.profiles.reevaluate(self.profile, "repetition")
                    if new:
                        self._set_profile(new)
                    messages.append({"role": "user", "content": "同じ操作を繰り返しています。既に得た情報で回答するか、別の方法を試してください。"})
                if self.tool_calls >= lim.get("max_tool_calls", 8):
                    messages.append({"role": "user", "content": "ツール呼び出しの上限に達しました。これまでの結果で最終回答をまとめてください。"})
                continue
            final = res.content
            if (self.profile.verify and verify_rounds < 2 and step < lim.get("max_steps", 6)
                    and not self._limit_reason() and final.strip()):
                self.job.emit("step", n=step, max=lim.get("max_steps"), phase="verify")
                original = next((m["content"] for m in messages if m["role"] == "user"), "")
                verdict = await self._llm([
                    {"role": "system", "content": VERIFY_PROMPT},
                    {"role": "user", "content": f"# 依頼\n{_text(original)[:6000]}\n\n# 回答案\n{final[:12000]}"}],
                    tools=False, stream=False, max_tokens=500)
                v = verdict.content.strip()
                if not v or v.upper().startswith("PASS") or "FAIL" not in v.upper()[:20]:
                    self.job.emit("verify", result="pass")
                    break
                verify_rounds += 1
                self.job.emit("verify", result="fail", issues=v[:1500])
                self.job.emit("reset", moved="")
                messages += [{"role": "assistant", "content": final},
                             {"role": "user", "content": f"検証で次の問題が見つかりました。修正し、完成した最終回答を改めて示してください:\n{v[:3000]}"}]
                continue
            break
        if not final.strip():
            self.job.emit("reset", moved="")
            wrap = await self._llm(messages + [{"role": "user", "content":
                                                "ここまでの結果を踏まえて、ユーザーへの最終回答を日本語でまとめてください。未完了の点があれば明記してください。"}],
                                   tools=False, max_tokens=min(self.profile.max_tokens, 2048))
            final = wrap.content
        if stop_reason:
            note = f"\n\n> ⚠ {stop_reason} (上限: ステップ{lim.get('max_steps')}, ツール{lim.get('max_tool_calls')}回, {lim.get('max_seconds')}秒)"
            self.job.emit("delta", text=note)
            self.job.emit("notice", message=stop_reason)
            final += note
        return final


def _norm_args(a: Any) -> str:
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return a.strip()
    return json.dumps(a, sort_keys=True, ensure_ascii=False)


def _short(a: Any, n: int = 300) -> str:
    s = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
    return s[:n]


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")

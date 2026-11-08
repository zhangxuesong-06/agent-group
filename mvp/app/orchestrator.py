from __future__ import annotations

import asyncio
import json
import time
import re
from collections import Counter
from typing import Any, AsyncIterator, Awaitable, Callable

from . import tools as tool_registry
from .agents import AgentRegistry
from .approvals import ApprovalManager
from .bus import EventBus
from .config import Settings
from .devices import DeviceHub, RemoteWorkspace
from .llm import LLMClient, LLMError, estimate_tokens
from .mcp import MCPManager
from .models import (
    AgentSpec,
    Budget,
    PlanOutput,
    RunState,
    TaskState,
    new_id,
    now,
)
from .routing import ModelRouter
from .store import Store


class BudgetExceeded(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class _DeltaPump:
    """Coalesce token deltas into a handful of live SSE events per second.

    Deltas are transient: they are fanned out to connected clients but never
    written to SQLite. The finished message is persisted by the caller.
    """

    def __init__(
        self,
        bus: EventBus,
        run_id: str,
        task_id: str | None,
        agent: str,
        step: int,
        scope: str,
    ) -> None:
        self.bus = bus
        self.run_id = run_id
        self.task_id = task_id
        self.agent = agent
        self.step = step
        self.scope = scope
        self._buffers: dict[str, list[str]] = {"content": [], "reasoning": []}
        self._last = time.monotonic()

    async def __call__(self, text: str, kind: str = "content") -> None:
        if not text:
            return
        bucket = kind if kind in self._buffers else "content"
        self._buffers[bucket].append(text)
        size = sum(len(chunk) for chunks in self._buffers.values() for chunk in chunks)
        if size >= 140 or time.monotonic() - self._last >= 0.1:
            await self.flush()

    async def flush(self) -> None:
        self._last = time.monotonic()
        for kind, chunks in self._buffers.items():
            if not chunks:
                continue
            text = "".join(chunks)
            chunks.clear()
            await self.bus.publish(
                self.run_id,
                "model.delta",
                {
                    "agent": self.agent,
                    "step": self.step,
                    "scope": self.scope,
                    "kind": kind,
                    "text": text,
                },
                self.task_id,
                transient=True,
            )


class Orchestrator:
    """Plans a DAG, dispatches tasks by capability, and advances the state machine.

    It never executes task content itself; that is the Worker's job. Keeping the
    two separate is what allows workers to move out of process later.
    """

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        llm: LLMClient,
        registry: AgentRegistry,
        settings: Settings,
        router: ModelRouter | None = None,
        approvals: ApprovalManager | None = None,
        mcp: MCPManager | None = None,
        devices: DeviceHub | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.router = router or ModelRouter(store)
        self.approvals = approvals
        self.mcp = mcp
        self.devices = devices or DeviceHub()
        self._runs: dict[str, RunState] = {}
        self._bg: set[asyncio.Task[Any]] = set()
        self._running_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        # Guidance injected by the user-facing model, applied before the next
        # agent step. Each entry is consumed once per target task.
        self._guidance: dict[str, list[dict[str, Any]]] = {}
        self._round_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        goal: str,
        budget: Budget | None = None,
        cluster: dict[str, Any] | None = None,
        device: dict[str, Any] | None = None,
    ) -> str:
        budget = budget or Budget(
            max_steps=self.settings.max_total_steps,
            max_tokens=self.settings.max_tokens,
            max_usd=self.settings.max_usd,
            deadline_sec=self.settings.deadline_sec,
        )
        cluster_data = dict(cluster or {})
        if device:
            cluster_data["device"] = {
                "id": device.get("id", ""),
                "name": device.get("name", ""),
                "workspace": device.get("workspace", ""),
            }
        run = RunState(
            id=new_id("run"),
            goal=goal.strip(),
            status="planning",
            budget=budget,
            deadline_at=now() + budget.deadline_sec,
            cluster=cluster_data,
        )
        self._runs[run.id] = run
        self.store.create_run(run)
        self.store.add_message(run.id, "user", run.goal, kind="goal")
        await self.bus.publish(run.id, "run.created", {"goal": run.goal, "budget": budget.model_dump()})
        task = asyncio.create_task(self._drive(run))
        self._bg.add(task)
        self._run_tasks[run.id] = task
        task.add_done_callback(lambda done: self._forget_run_task(run.id, done))
        task.add_done_callback(self._bg.discard)
        return run.id

    # ------------------------------------------------------------------
    # User-facing conversation model
    # ------------------------------------------------------------------

    GUIDE_RE = re.compile(
        r"^\s*GUIDE(?:\[([^\]]*)\])?\s*[:：]\s*(.+?)\s*$", re.MULTILINE
    )

    def add_guidance(
        self, run_id: str, text: str, task_id: str | None = None
    ) -> None:
        text = text.strip()
        if not text:
            return
        self._guidance.setdefault(run_id, []).append(
            {"task_id": task_id or None, "text": text, "consumed": set()}
        )

    def _take_guidance(self, run_id: str, task_id: str) -> list[str]:
        notes: list[str] = []
        for item in self._guidance.get(run_id, []):
            if item["task_id"] not in (None, task_id):
                continue
            if task_id in item["consumed"]:
                continue
            item["consumed"].add(task_id)
            notes.append(item["text"])
        return notes

    def _front_provider_id(self, run_data: dict[str, Any]) -> str | None:
        cluster = run_data.get("cluster") or {}
        provider_id = cluster.get("planner_id") or None
        if provider_id:
            return provider_id
        active = self.store.get_active_provider()
        return active["id"] if active else None

    @staticmethod
    def _sse(payload: dict[str, Any]) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    async def front_chat(
        self, run_id: str, content: str
    ) -> AsyncIterator[str]:
        """Run one conversation turn through the expert-team lead.

        The lead decides between a direct reply, dispatching a new round of
        expert tasks, or guiding the tasks that are already running. Team work
        is executed in the background so it survives a dropped HTTP stream.
        """
        run_data = self.store.get_run(run_id)
        if run_data is None:
            yield self._sse({"type": "error", "error": "运行不存在"})
            return

        user_message = self.store.add_message(
            run_id, "user", content.strip(), kind="chat"
        )
        yield self._sse({"type": "user", "message": user_message})
        yield self._sse({"type": "status", "text": "主理人正在判断这一轮怎么做…"})

        # The lead call streams its visible reply while it decides. This is
        # what makes simple answers feel live instead of waiting for the whole
        # tool-call decision first.
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        streamed: list[str] = []

        async def lead_delta(text: str, kind: str = "content") -> None:
            if text and kind == "content":
                await queue.put(("delta", text))

        async def lead_worker() -> None:
            try:
                result = await self._lead_decision(
                    run_data, content, on_delta=lead_delta
                )
                await queue.put(("done", result))
            except Exception as exc:  # noqa: BLE001 - report to the stream
                await queue.put(("error", exc))

        lead_task = asyncio.create_task(lead_worker())
        ack_id = new_id("msg")
        ack_started = False
        decision: dict[str, Any] | None = None
        try:
            while True:
                kind, value = await queue.get()
                if kind == "delta":
                    streamed.append(value)
                    if not ack_started:
                        yield self._sse(
                            {"type": "start", "message_id": ack_id}
                        )
                        ack_started = True
                    yield self._sse(
                        {
                            "type": "delta",
                            "message_id": ack_id,
                            "text": value,
                            "kind": "content",
                        }
                    )
                elif kind == "done":
                    decision = value
                    break
                elif kind == "error":
                    yield self._sse(
                        {"type": "error", "error": f"{type(value).__name__}: {value}"}
                    )
                    return
        finally:
            if not lead_task.done():
                lead_task.cancel()

        if decision is None:
            return
        # The decision call can take a while; another round may have started
        # meanwhile, so re-read the authoritative state before dispatching.
        fresh = self.store.get_run(run_id)
        if fresh is not None:
            run_data = fresh
        reply = "".join(streamed).strip() or decision["reply"] or "我在。"
        decision["reply"] = reply
        action = decision["action"]
        planned = decision["tasks"]
        guides = decision["guides"]
        active_tasks = [
            task
            for task in (run_data.get("tasks") or [])
            if task["status"] in ("pending", "ready", "running")
        ]

        # A new round cannot run concurrently with an active one. Turn the
        # requested work into guidance for the running experts instead.
        if action == "team" and active_tasks:
            note = "；".join(
                f"{item['title']}：{item['instruction']}" for item in planned
            )
            if note:
                self.add_guidance(run_id, note)
                extra = "\n\n已把新的调整要求转达给正在执行的成员。"
                reply = reply.rstrip() + extra
                decision["reply"] = reply
                if ack_started:
                    yield self._sse(
                        {
                            "type": "delta",
                            "message_id": ack_id,
                            "text": extra,
                            "kind": "content",
                        }
                    )
                await self.bus.publish(
                    run_id,
                    "front.guided",
                    {
                        "guides": [
                            {
                                "task_id": "",
                                "title": "全部执行中的任务",
                                "text": note,
                            }
                        ]
                    },
                )
            action = "reply"

        # A guide with nobody running becomes a small follow-up round.
        if action == "guide" and not active_tasks:
            planned = [
                {
                    "title": "按引导继续",
                    "instruction": guide["text"],
                    "capability": "analysis",
                    "depends_on": [],
                }
                for guide in guides
                if guide.get("text")
            ]
            action = "team" if planned else "reply"

        # If the model produced no visible content before its tool call, send
        # the reply from the tool arguments now.
        if not ack_started:
            yield self._sse({"type": "start", "message_id": ack_id})
            for index in range(0, len(reply), 24):
                yield self._sse(
                    {
                        "type": "delta",
                        "message_id": ack_id,
                        "text": reply[index : index + 24],
                        "kind": "content",
                    }
                )
        ack = self.store.add_message(
            run_id, "assistant", reply, kind="chat", message_id=ack_id
        )
        yield self._sse({"type": "done", "message": ack, "guides": guides})

        if action == "guide" and guides:
            applied = self._apply_guides(run_id, run_data, guides)
            if applied:
                await self.bus.publish(
                    run_id,
                    "front.guided",
                    {"guides": applied},
                )

        if action != "team" or not planned:
            return

        # 2) Dispatch a new expert round.
        round_no = max(
            (int(task.get("round") or 1) for task in run_data.get("tasks") or []),
            default=0,
        ) + 1
        run = self._runs.get(run_id) or self.store.load_run_state(run_id)
        if run is None:
            yield self._sse({"type": "error", "error": "运行状态丢失"})
            return
        self._runs[run_id] = run
        new_tasks = self._round_tasks_from_plan(run, planned, round_no)
        if not new_tasks:
            yield self._sse({"type": "error", "error": "专家团没有可执行的任务"})
            return

        run.tasks.extend(new_tasks)
        run.status = "running"
        run.error = ""
        run.cancelled = False
        run.steps = 0
        run.tokens = 0
        run.prompt_tokens = 0
        run.completion_tokens = 0
        run.cost_usd = 0.0
        run.deadline_at = now() + run.budget.deadline_sec
        self.store.create_tasks(new_tasks)
        self.store.update_run(run)
        await self.bus.publish(
            run_id,
            "round.started",
            {
                "round": round_no,
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "capability": task.capability,
                        "agent": task.assignee,
                    }
                    for task in new_tasks
                ],
            },
        )
        yield self._sse(
            {
                "type": "round",
                "status": "started",
                "round": round_no,
                "tasks": [
                    {"id": task.id, "title": task.title} for task in new_tasks
                ],
            }
        )

        summary_id = new_id("msg")
        yield self._sse({"type": "start", "message_id": summary_id})
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        round_task = asyncio.create_task(
            self._run_round(run, new_tasks, round_no, summary_id, queue)
        )
        self._bg.add(round_task)
        round_task.add_done_callback(self._bg.discard)

        try:
            while True:
                kind, value = await queue.get()
                if kind == "delta":
                    yield self._sse(
                        {
                            "type": "delta",
                            "message_id": summary_id,
                            "text": value,
                            "kind": "content",
                        }
                    )
                elif kind == "done":
                    yield self._sse(
                        {"type": "done", "message": value, "guides": []}
                    )
                    return
                elif kind == "error":
                    yield self._sse({"type": "error", "error": value})
                    return
        except asyncio.CancelledError:
            # The browser went away; the round keeps running and its summary
            # is persisted for the next page load.
            raise

    async def _lead_decision(
        self,
        run_data: dict[str, Any],
        content: str,
        on_delta: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        provider_id = self._front_provider_id(run_data)
        capabilities: dict[str, str] = {}
        for agent in self.registry.all():
            for capability in agent.capabilities:
                capabilities.setdefault(capability, agent.name)
        expert_lines = [
            f"- {capability}: {name}"
            for capability, name in sorted(capabilities.items())
        ]
        tasks = run_data.get("tasks") or []
        recent_tasks = tasks[-12:]
        task_lines = [
            f"- id={task['id']} | {task['title']} | 状态={task['status']} | "
            f"第 {task.get('round') or 1} 轮"
            for task in recent_tasks
        ]
        history = [
            message
            for message in self.store.list_messages(run_data["id"])
            if message["kind"] == "chat"
        ][-8:]
        history_lines = [
            ("用户" if message["role"] == "user" else "你")
            + "："
            + message["content"][:300]
            for message in history
        ]
        prompt = "\n".join(
            [
                "你是这个专家团的主理人，直接和用户对话，并决定这一轮怎么执行。",
                f"会话目标：{run_data.get('goal', '')}",
                f"当前状态：{run_data.get('status', '')}",
                "可用专家：",
                *(expert_lines or ["- general: 通用专家"]),
                "已有任务：",
                *(task_lines or ["- 暂无"]),
                "已有交付摘要：",
                (run_data.get("result") or "暂无")[:1500],
                "最近的对话：",
                *(history_lines or ["- 暂无"]),
                f"用户最新消息：{content.strip()}",
                "",
                "决策方式：",
                "1. 如果这一轮需要取数、调研、分析、写代码、写文档、检查或验证，"
                "请调用工具 expert_action，action=team，并给出 1-5 个任务。",
                "2. 如果有任务正在执行，用户要调整方向，请调用 expert_action，"
                "action=guide，并用 guides 指定要通知的任务；省略 task_id 表示全部。",
                "3. 如果只是普通问答、确认或基于已有结果就能回答，直接回复文本，"
                "不要调用工具。",
                "4. 调用工具时，reply 字段写要展示给用户的中文说明；任务能力必须从可用专家里选。",
                "5. 不要编造尚未发生的结果。只要这一轮需要产出新东西，就必须调用工具。",
            ]
        )
        fallback = "收到，我先看一下当前进展。"
        try:
            response = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                self._expert_tool_schema(),
                provider_id=provider_id,
                on_delta=on_delta,
            )
        except Exception as exc:  # noqa: BLE001 - lead failure is user-visible
            return {
                "action": "reply",
                "reply": f"主理人暂时无法响应：{type(exc).__name__}: {exc}",
                "tasks": [],
                "guides": [],
            }

        call = next(
            (item for item in response.tool_calls if item.name == "expert_action"),
            None,
        )
        if call is not None:
            data = call.arguments if isinstance(call.arguments, dict) else {}
            raw_reply = str(data.get("reply") or response.content or fallback).strip()
            action = str(data.get("action") or "team").lower()
            planned_raw = data.get("tasks") or []
            guides_raw = data.get("guides") or []
        else:
            raw = response.content.strip()
            try:
                data = self._parse_lead_json(raw)
            except Exception:  # noqa: BLE001 - fall back to a plain reply
                guides = [
                    {
                        "task_id": (match.group(1) or "").strip(),
                        "text": match.group(2).strip(),
                    }
                    for match in self.GUIDE_RE.finditer(raw)
                    if match.group(2).strip()
                ]
                return {
                    "action": "guide" if guides else "reply",
                    "reply": self.GUIDE_RE.sub("", raw).strip() or fallback,
                    "tasks": [],
                    "guides": guides,
                }
            raw_reply = str(data.get("reply") or raw or fallback).strip()
            action = str(data.get("action") or "reply").lower()
            planned_raw = data.get("tasks") or []
            guides_raw = data.get("guides") or []

        if action not in ("reply", "team", "guide"):
            action = "reply"
        planned: list[dict[str, Any]] = []
        for item in (planned_raw or [])[:5]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:120]
            instruction = str(item.get("instruction") or "").strip()
            capability = str(item.get("capability") or "general").strip()
            if not instruction:
                continue
            if capability not in capabilities:
                capability = "general"
            depends_on = item.get("depends_on") or []
            if not isinstance(depends_on, list):
                depends_on = []
            planned.append(
                {
                    "title": title or instruction[:40],
                    "instruction": instruction,
                    "capability": capability,
                    "depends_on": [
                        int(index)
                        for index in depends_on
                        if isinstance(index, (int, float))
                    ],
                }
            )
        guides: list[dict[str, str]] = []
        for item in guides_raw or []:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            guides.append(
                {
                    "task_id": str(item.get("task_id") or "").strip(),
                    "text": str(item["text"]).strip(),
                }
            )
        if action == "team" and not planned:
            action = "reply"
        if action == "guide" and not guides:
            action = "reply"
        return {
            "action": action,
            "reply": raw_reply or fallback,
            "tasks": planned,
            "guides": guides,
        }

    @staticmethod
    def _expert_tool_schema() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "expert_action",
                    "description": (
                        "派发一轮专家任务，或给正在执行的专家插话。"
                        "纯问答不要调用这个工具。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["team", "guide"],
                            },
                            "reply": {
                                "type": "string",
                                "description": "展示给用户的中文说明",
                            },
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "instruction": {"type": "string"},
                                        "capability": {"type": "string"},
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "integer"},
                                        },
                                    },
                                    "required": ["title", "instruction"],
                                },
                            },
                            "guides": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["text"],
                                },
                            },
                        },
                        "required": ["action", "reply"],
                    },
                },
            }
        ]

    @staticmethod
    def _parse_lead_json(text: str) -> dict[str, Any]:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if "\n" in raw:
                raw = raw.split("\n", 1)[1]
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("不是 JSON")
        data = json.loads(raw[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("不是 JSON 对象")
        return data

    def _round_tasks_from_plan(
        self, run: RunState, planned: list[dict[str, Any]], round_no: int
    ) -> list[TaskState]:
        base_order = max((task.order for task in run.tasks), default=-1) + 1
        id_by_index: dict[int, str] = {}
        tasks: list[TaskState] = []
        for index, item in enumerate(planned):
            task_id = new_id("task")
            id_by_index[index] = task_id
            dependencies = [
                id_by_index[i]
                for i in item.get("depends_on", [])
                if i in id_by_index
            ]
            tasks.append(
                TaskState(
                    id=task_id,
                    run_id=run.id,
                    order=base_order + index,
                    round=round_no,
                    title=item["title"][:120],
                    instruction=item["instruction"],
                    capability=item["capability"],
                    depends_on=dependencies,
                )
            )
        return tasks

    def _apply_guides(
        self,
        run_id: str,
        run_data: dict[str, Any],
        guides: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        tasks = run_data.get("tasks") or []
        by_id = {task["id"]: task for task in tasks}
        active_ids = {
            task["id"]
            for task in tasks
            if task["status"] in ("pending", "ready", "running")
        }
        applied: list[dict[str, str]] = []
        for guide in guides:
            target = guide.get("task_id") or ""
            if target and target not in by_id:
                if target.isdigit() and 0 < int(target) <= len(tasks):
                    target = tasks[int(target) - 1]["id"]
                else:
                    target = ""
            if target and target not in active_ids:
                target = ""
            text = guide["text"].strip()
            if not text:
                continue
            self.add_guidance(run_id, text, target or None)
            applied.append(
                {
                    "task_id": target,
                    "title": by_id[target]["title"] if target else "全部执行中的任务",
                    "text": text,
                }
            )
        return applied

    async def _run_round(
        self,
        run: RunState,
        tasks: list[TaskState],
        round_no: int,
        summary_id: str,
        queue: asyncio.Queue[tuple[str, Any]],
    ) -> None:
        lock = self._round_locks.setdefault(run.id, asyncio.Lock())
        try:
            async with lock:
                run.status = "running"
                self.store.update_run(run)
                await self.bus.publish(
                    run.id, "run.status", {"status": "running"}
                )
                try:
                    await self._schedule(run)
                except Exception as exc:  # noqa: BLE001 - one round boundary
                    run.status = "failed"
                    run.error = f"{type(exc).__name__}: {exc}"
                if not run.error:
                    failed = next(
                        (task for task in tasks if task.status == "failed"), None
                    )
                    if failed is not None:
                        run.error = f"{failed.title}: {failed.error}"
                if not run.error:
                    run.status = "succeeded"
                else:
                    run.status = "failed"
                summary = await self._summarize_round(run, tasks, queue)
                self.store.update_run(run)
                message = self.store.add_message(
                    run.id,
                    "assistant",
                    summary,
                    kind="chat",
                    message_id=summary_id,
                )
                await self.bus.publish(
                    run.id, "chat.message", {"message": message}
                )
                await self.bus.publish(
                    run.id,
                    "round.completed",
                    {"round": round_no, "status": run.status},
                )
                await self.bus.publish(
                    run.id,
                    "run.status",
                    {"status": run.status, "usage": self._usage(run)},
                )
                await queue.put(("done", message))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report to the stream
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            self.store.update_run(run)
            await queue.put(("error", run.error))

    async def _summarize_round(
        self,
        run: RunState,
        tasks: list[TaskState],
        queue: asyncio.Queue[tuple[str, Any]],
    ) -> str:
        finished = [task for task in tasks if task.status == "done" and task.result]
        failed = [task for task in tasks if task.status == "failed"]
        if not finished:
            details = "\n".join(
                f"- {task.title}: {task.error or task.status}" for task in tasks
            )
            return "本轮没有产出可用结果。\n\n" + details
        material = "\n\n".join(
            f"## {task.title}\n执行者：{task.assignee or '专家'}\n{task.result[:2400]}"
            for task in finished
        )
        prompt = (
            "你是专家团主理人。本轮专家已经完成工作，请给用户一个最终答复。\n"
            "要求：直接给结论，说明关键发现和下一步；不要重复执行过程；"
            "不要编造未出现的事实；用中文。\n\n"
            f"用户目标：{run.goal}\n\n"
            f"本轮专家产出：\n{material}"
        )
        if failed:
            prompt += "\n\n未成功的任务：\n" + "\n".join(
                f"- {task.title}: {task.error or task.status}" for task in failed
            )

        async def emit(text: str, kind: str = "content") -> None:
            if text and kind == "content":
                await queue.put(("delta", text))

        try:
            response = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                provider_id=run.cluster.get("planner_id") or None,
                on_delta=emit,
            )
            return response.content.strip() or material
        except Exception:  # noqa: BLE001 - fall back to the raw expert output
            for index in range(0, len(material), 80):
                await queue.put(("delta", material[index : index + 80]))
            return material

    def _ensure_result_message(self, run: RunState) -> None:
        if not run.result:
            return
        if any(
            message["kind"] == "result"
            for message in self.store.list_messages(run.id)
        ):
            return
        self.store.add_message(run.id, "assistant", run.result, kind="result")

    async def cancel(self, run_id: str) -> bool:
        """Stop a run immediately.

        The UI treats cancellation as the highest-priority action, so we do not
        wait for the current LLM call to finish: every child task is cancelled,
        which propagates ``CancelledError`` into the in-flight httpx request and
        closes the connection. The planner/scheduler task is cancelled too, so a
        run stuck in ``_plan`` also stops consuming tokens.
        """
        run = self._runs.get(run_id)
        if run is None:
            # The process may have restarted while SQLite still says the run is
            # active. Nothing is consuming tokens in that case, so finalize it.
            stored = self.store.load_run_state(run_id)
            if stored is None or stored.status in ("succeeded", "failed", "cancelled", "draft"):
                return False
            stored.cancelled = True
            stored.status = "cancelled"
            stored.error = "运行已停止（服务重启后无活动任务）"
            for task in stored.tasks:
                if task.status not in ("done", "failed"):
                    task.status = "skipped"
                    task.error = "运行已停止"
                    task.finished_at = now()
                    self.store.update_task(task)
            self.store.update_run(stored)
            await self.bus.publish(
                run_id, "run.cancelled", {"reason": stored.error}
            )
            return True
        if run.status in ("succeeded", "failed", "cancelled"):
            return False
        if run.cancelled:
            return True

        run.cancelled = True
        run.status = "cancelling"
        run.error = "用户取消"
        self.store.update_run(run)
        if self.approvals is not None:
            self.approvals.cancel_run(run_id)
        await self.bus.publish(
            run_id,
            "run.cancelling",
            {"message": "正在停止所有模型请求"},
        )

        children = list(self._running_tasks.get(run_id, ()))
        for job in children:
            job.cancel()
        parent = self._run_tasks.get(run_id)
        if parent is not None and not parent.done():
            parent.cancel()

        if children:
            # Give cancellation a moment to unwind so the API can report a
            # truthful state, but never let a stuck task block the request.
            await asyncio.wait(children, timeout=1.5)
        return True

    def _forget_run_task(self, run_id: str, job: asyncio.Task[Any]) -> None:
        if self._run_tasks.get(run_id) is job:
            self._run_tasks.pop(run_id, None)

    def _mark_pending_skipped(self, run: RunState, reason: str) -> None:
        for task in run.tasks:
            if task.status in ("pending", "ready"):
                task.status = "skipped"
                task.error = reason
                task.finished_at = now()
                self.store.update_task(task)

    def _forget_job(self, run_id: str, job: asyncio.Task[Any]) -> None:
        jobs = self._running_tasks.get(run_id)
        if jobs is None:
            return
        jobs.discard(job)
        if not jobs:
            self._running_tasks.pop(run_id, None)

    async def recover_interrupted_runs(self) -> list[str]:
        """Resume runs that were active when the previous process stopped.

        Completed tasks stay done. In-flight tasks are put back to pending and
        continue from their step checkpoint, so a restart does not throw away
        the whole run.
        """
        recovered: list[str] = []
        for item in self.store.list_runs(limit=200):
            if item.get("status") not in ("planning", "running", "cancelling"):
                continue
            run = self.store.load_run_state(item["id"])
            if run is None:
                continue

            if run.status == "cancelling":
                run.status = "cancelled"
                run.error = "服务重启时已取消"
                for task in run.tasks:
                    if task.status not in ("done", "failed"):
                        task.status = "skipped"
                        task.error = "服务重启时已取消"
                        task.finished_at = now()
                        self.store.update_task(task)
                self.store.update_run(run)
                await self.bus.publish(
                    run.id, "run.cancelled", {"reason": run.error}
                )
                continue

            run.error = ""
            run.cancelled = False
            # A restart can happen hours later; give the recovered run a fresh
            # deadline instead of failing immediately on the old one.
            run.deadline_at = now() + run.budget.deadline_sec
            reset = 0
            for task in run.tasks:
                if task.status in ("ready", "running"):
                    task.status = "pending"
                    task.error = ""
                    task.started_at = None
                    task.finished_at = None
                    self.store.update_task(task)
                    reset += 1

            self._runs[run.id] = run
            if not run.tasks:
                run.status = "planning"
                self.store.update_run(run)
                job = asyncio.create_task(self._drive(run))
            else:
                run.status = "running"
                self.store.update_run(run)
                job = asyncio.create_task(self._continue_run(run))
            self._run_tasks[run.id] = job
            self._bg.add(job)
            job.add_done_callback(
                lambda done, rid=run.id: self._forget_run_task(rid, done)
            )
            job.add_done_callback(self._bg.discard)
            await self.bus.publish(
                run.id,
                "run.recovered",
                {
                    "tasks": len(run.tasks),
                    "reset": reset,
                    "message": "服务重启，已从检查点恢复",
                },
            )
            recovered.append(run.id)
        return recovered

    async def shutdown(self) -> None:
        """Stop in-flight work without turning resumable runs into cancelled."""
        runs = list(self._runs.values())
        for run in runs:
            run.shutting_down = True
        jobs: set[asyncio.Task[Any]] = set()
        jobs.update(self._run_tasks.values())
        for children in self._running_tasks.values():
            jobs.update(children)
        jobs.update(self._bg)
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def is_active(self, run_id: str) -> bool:
        return run_id in self._runs and self._runs[run_id].status in (
            "planning",
            "running",
            "cancelling",
        )

    async def resume(self, run_id: str, task_id: str | None = None) -> bool:
        if self.is_active(run_id):
            return False
        run = self.store.load_run_state(run_id)
        if run is None or run.status not in ("failed", "cancelled"):
            return False

        if task_id:
            target = next((t for t in run.tasks if t.id == task_id), None)
            if target is None or target.status not in ("pending", "failed", "skipped"):
                return False
            done_ids = {t.id for t in run.tasks if t.status == "done"}
            if any(dep not in done_ids for dep in target.depends_on):
                return False
            reset_ids = {target.id}
            for task in sorted(run.tasks, key=lambda item: item.order):
                if any(dep in reset_ids for dep in task.depends_on):
                    reset_ids.add(task.id)
        else:
            reset_ids = {
                task.id for task in run.tasks if task.status != "done"
            }

        reset_tasks = [t for t in run.tasks if t.id in reset_ids]
        if not reset_tasks:
            return False
        for task in reset_tasks:
            task.status = "pending"
            task.result = ""
            task.error = ""
            task.model_id = None
            task.model_name = None
            task.steps = 0
            task.tokens = 0
            task.started_at = None
            task.finished_at = None
            task.checkpoint = None
            self.store.update_task(task)

        done_tasks = [t for t in run.tasks if t.status == "done"]
        run.steps = sum(t.steps for t in done_tasks)
        run.tokens = sum(t.tokens for t in done_tasks)
        run.prompt_tokens = 0
        run.completion_tokens = 0
        run.cost_usd = 0.0
        run.status = "running"
        run.error = ""
        run.result = ""
        run.cancelled = False
        run.deadline_at = now() + run.budget.deadline_sec
        self.store.update_run(run)
        self._runs[run.id] = run
        await self.bus.publish(
            run.id,
            "run.resumed",
            {"tasks": len(reset_tasks), "task_id": task_id},
        )
        job = asyncio.create_task(self._continue_run(run))
        self._bg.add(job)
        self._run_tasks[run.id] = job
        job.add_done_callback(lambda done: self._forget_run_task(run.id, done))
        job.add_done_callback(self._bg.discard)
        return True

    async def _continue_run(self, run: RunState) -> None:
        try:
            run.status = "running"
            self.store.update_run(run)
            await self.bus.publish(run.id, "run.status", {"status": "running"})
            if run.cluster.get("single_model") and run.tasks:
                task = run.tasks[0]
                if task.status in ("pending", "ready"):
                    await self._execute_direct(run, task)
                else:
                    await self._finish(run)
            else:
                await self._schedule(run)
                await self._finish(run)
        except asyncio.CancelledError:
            if run.shutting_down:
                run.status = "running"
                run.error = ""
            else:
                run.status = "cancelled"
                run.error = run.error or "运行被中断"
                self._mark_pending_skipped(run, "运行已停止")
                await self.bus.publish(
                    run.id, "run.cancelled", {"reason": run.error}
                )
            raise
        except Exception as exc:  # noqa: BLE001 - top-level guard
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            await self.bus.publish(run.id, "run.failed", {"error": run.error})
        finally:
            self.store.update_run(run)
            self._ensure_result_message(run)
            await self.bus.publish(
                run.id,
                "run.status",
                {"status": run.status, "usage": self._usage(run)},
            )
            self._runs.pop(run.id, None)
            self._running_tasks.pop(run.id, None)
            self._guidance.pop(run.id, None)

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    async def _drive(self, run: RunState) -> None:
        try:
            await self.bus.publish(run.id, "run.status", {"status": "planning"})
            if run.cluster.get("single_model"):
                # Single-model mode: no planner/DAG, one direct task.
                run.tasks = [
                    TaskState(
                        id=new_id("task"),
                        run_id=run.id,
                        order=0,
                        title="直接执行",
                        instruction=run.goal,
                        capability="general",
                    )
                ]
            else:
                run.tasks = await self._plan(run)
            self.store.create_tasks(run.tasks)
            await self.bus.publish(
                run.id,
                "run.planned",
                {
                    "tasks": [
                        {
                            "id": t.id,
                            "title": t.title,
                            "capability": t.capability,
                            "depends_on": t.depends_on,
                        }
                        for t in run.tasks
                    ],
                    "single_model": bool(run.cluster.get("single_model")),
                },
            )
            run.status = "running"
            self.store.update_run(run)
            await self.bus.publish(run.id, "run.status", {"status": "running"})

            if run.cluster.get("single_model"):
                await self._execute_direct(run, run.tasks[0])
            else:
                await self._schedule(run)
                await self._finish(run)
        except asyncio.CancelledError:
            if run.shutting_down:
                run.status = "running"
                run.error = ""
            else:
                run.status = "cancelled"
                run.error = run.error or "运行被中断"
                self._mark_pending_skipped(run, "运行已停止")
                await self.bus.publish(
                    run.id, "run.cancelled", {"reason": run.error}
                )
            raise
        except Exception as exc:  # noqa: BLE001 - top-level guard
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            await self.bus.publish(run.id, "run.failed", {"error": run.error})
        finally:
            self.store.update_run(run)
            self._ensure_result_message(run)
            await self.bus.publish(
                run.id,
                "run.status",
                {"status": run.status, "usage": self._usage(run)},
            )
            # Finished runs are served from SQLite; keep memory bounded.
            self._runs.pop(run.id, None)
            self._running_tasks.pop(run.id, None)
            self._guidance.pop(run.id, None)

    async def _finish(self, run: RunState) -> None:
        if run.cancelled:
            run.status = "cancelled"
            run.error = run.error or "用户取消"
            await self.bus.publish(run.id, "run.cancelled", {"reason": run.error})
        elif run.error:
            run.status = "failed"
            first_failed = next(
                (t for t in run.tasks if t.status == "failed" and t.error), None
            )
            if first_failed:
                run.error = f"{first_failed.title}: {first_failed.error}"
            done_tasks = [t for t in run.tasks if t.status == "done" and t.result]
            if done_tasks:
                partial = "\n\n".join(
                    f"## {t.title}\n{t.result[:2500]}" for t in done_tasks
                )
                run.result = (
                    f"# 运行未完全完成\n\n原因：{run.error}\n\n"
                    f"以下为已完成任务的结果：\n\n{partial}"
                )
            await self.bus.publish(run.id, "run.failed", {"error": run.error})
        elif (
            run.cluster.get("single_model")
            and len(run.tasks) == 1
            and run.tasks[0].status == "done"
        ):
            # Direct single-model answer: don't spend another call on a summary.
            run.result = run.tasks[0].result
            run.status = "succeeded"
            await self.bus.publish(
                run.id,
                "run.completed",
                {"result": run.result, "usage": self._usage(run)},
            )
        elif all(t.status == "done" for t in run.tasks):
            run.result = await self._finalize(run)
            run.status = "succeeded"
            await self.bus.publish(
                run.id,
                "run.completed",
                {"result": run.result, "usage": self._usage(run)},
            )
        else:
            run.status = "failed"
            failed = [t for t in run.tasks if t.status != "done"]
            first_error = next((t for t in failed if t.error), None)
            if first_error:
                run.error = f"{first_error.title}: {first_error.error}"
            else:
                run.error = f"存在未完成的任务: {', '.join(t.title for t in failed)}"
            await self.bus.publish(run.id, "run.failed", {"error": run.error})

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan(self, run: RunState) -> list[TaskState]:
        if self.llm.is_mock:
            planned = self._heuristic_plan(run.goal)
        else:
            planned = await self._llm_plan(
                run.goal,
                run.cluster.get("planner_id") or None,
                run=run,
            )
            if not planned:
                planned = self._heuristic_plan(run.goal)

        tasks: list[TaskState] = []
        id_by_index: dict[int, str] = {}
        for index, item in enumerate(planned):
            task_id = new_id("task")
            id_by_index[index] = task_id
            deps = [id_by_index[i] for i in item.depends_on if i in id_by_index]
            tasks.append(
                TaskState(
                    id=task_id,
                    run_id=run.id,
                    order=index,
                    title=item.title.strip()[:120] or f"任务 {index + 1}",
                    instruction=item.instruction.strip() or run.goal,
                    capability=item.capability,
                    depends_on=deps,
                )
            )
        if not tasks:
            raise ValueError("规划器未产出任何任务")
        return tasks

    async def _llm_plan(
        self,
        goal: str,
        provider_id: str | None = None,
        run: RunState | None = None,
    ) -> list[Any]:
        capabilities = sorted({c for a in self.registry.all() for c in a.capabilities})
        prompt = (
            "你是多 Agent 集群的规划器。把用户目标拆成 2-6 个有依赖关系的任务。\n"
            f"每个任务的 capability 必须从 {capabilities} 中选择。\n"
            "depends_on 是任务在数组中的下标，只能引用前面的任务。\n"
            "每个任务应能被单个 Agent 在 6 步内完成。\n"
            '只输出 JSON，不要解释：{"tasks":[{"title":"...","instruction":"...",'
            '"capability":"research","depends_on":[]}]}\n\n'
            f"用户目标：{goal}"
        )
        pump = (
            _DeltaPump(self.bus, run.id, None, "规划器", 0, "thinking")
            if run is not None
            else None
        )
        try:
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                provider_id=provider_id,
                on_delta=pump,
            )
            if pump is not None:
                await pump.flush()
            parsed = self._parse_plan(resp.content)
            return parsed
        except (LLMError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def _parse_plan(text: str) -> list[Any]:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("规划结果不是 JSON")
        data = json.loads(raw[start : end + 1])
        output = PlanOutput.model_validate(data)
        # Drop invalid forward/self references instead of failing the whole run.
        for i, task in enumerate(output.tasks):
            task.depends_on = sorted({d for d in task.depends_on if 0 <= d < i})
        return output.tasks

    @staticmethod
    def _heuristic_plan(goal: str) -> list[Any]:
        from .models import PlannedTask

        wants_code = any(
            key in goal for key in ("代码", "开发", "实现", "插件", "程序", "脚本", "工程", "API")
        )
        tasks = [
            PlannedTask(
                title="背景调研",
                instruction=f"调研与目标相关的背景、约束和关键事实：{goal}",
                capability="research",
                depends_on=[],
            ),
            PlannedTask(
                title="要点分析",
                instruction="基于调研结果，分析关键问题、主要风险与可行路径。",
                capability="analysis",
                depends_on=[0],
            ),
        ]
        if wants_code:
            tasks.append(
                PlannedTask(
                    title="实现方案",
                    instruction="给出可落地的技术方案、模块划分与关键实现要点。",
                    capability="code",
                    depends_on=[1],
                )
            )
            writer_deps = [1, 2]
        else:
            writer_deps = [1]
        tasks.append(
            PlannedTask(
                title="方案撰写",
                instruction="把调研、分析与实现方案整理成一份结构清晰的交付文档。",
                capability="writing",
                depends_on=writer_deps,
            )
        )
        tasks.append(
            PlannedTask(
                title="质量审校",
                instruction="检查交付物的完整性、事实一致性与可执行性，并给出结论。",
                capability="review",
                depends_on=[len(tasks) - 1],
            )
        )
        return tasks

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    @staticmethod
    def _allowed_ids(run: RunState) -> set[str] | None:
        provider_ids = run.cluster.get("provider_ids") or []
        return set(provider_ids) if provider_ids else None

    def _remote_for(self, run: RunState) -> RemoteWorkspace | None:
        device = (run.cluster or {}).get("device") or {}
        if not device.get("id"):
            return None
        return RemoteWorkspace(
            self.devices,
            str(device["id"]),
            str(device.get("workspace") or ""),
        )

    async def _schedule(self, run: RunState) -> None:
        running: dict[asyncio.Task[Any], TaskState] = {}
        allowed_ids = self._allowed_ids(run)

        while True:
            if run.cancelled:
                break
            if run.error:
                break
            if time.time() > run.deadline_at:
                run.error = "超过运行截止时间"
                await self.bus.publish(run.id, "budget.exceeded", {"message": run.error})
                break

            done_ids = {t.id for t in run.tasks if t.status == "done"}
            ready = [
                t
                for t in run.tasks
                if t.status == "pending" and all(dep in done_ids for dep in t.depends_on)
            ]
            load = Counter(t.assignee for t in running.values() if t.assignee)
            model_load = Counter(
                t.model_id for t in running.values() if t.model_id
            )

            for task in ready:
                if len(running) >= self.settings.max_concurrency:
                    break
                agent = self.registry.pick(task.capability, load)
                if agent is None:
                    task.status = "failed"
                    task.error = f"没有可用 Agent 能处理能力: {task.capability}"
                    task.finished_at = now()
                    self.store.update_task(task)
                    await self.bus.publish(run.id, "task.failed", {"error": task.error}, task.id)
                    continue
                model = self.router.pick(task.capability, model_load, allowed_ids)
                if model is not None:
                    task.model_id = model["id"]
                    task.model_name = f"{model['name']} · {model['model']}"
                    model_load[model["id"]] += 1
                else:
                    task.model_id = None
                    task.model_name = "默认"
                task.assignee = agent.id
                task.status = "ready"
                load[agent.id] += 1
                await self.bus.publish(
                    run.id,
                    "task.ready",
                    {
                        "title": task.title,
                        "capability": task.capability,
                        "agent": agent.id,
                        "model": task.model_name,
                    },
                    task.id,
                )
                job = asyncio.create_task(self._execute(run, task, agent))
                running[job] = task
                self._running_tasks.setdefault(run.id, set()).add(job)
                job.add_done_callback(lambda done, rid=run.id: self._forget_job(rid, done))

            if run.error or run.cancelled:
                break
            if all(t.status in ("done", "failed", "skipped") for t in run.tasks):
                break

            if not running:
                pending = [t for t in run.tasks if t.status == "pending"]
                if pending:
                    failed = next(
                        (t for t in run.tasks if t.status == "failed" and t.error), None
                    )
                    if failed is not None:
                        run.error = f"{failed.title}: {failed.error}"
                    else:
                        run.error = "调度死锁：存在待执行任务但没有可运行任务"
                    for t in pending:
                        t.status = "skipped"
                        t.error = "依赖失败或调度死锁"
                        self.store.update_task(t)
                break

            done, _ = await asyncio.wait(
                set(running), timeout=0.5, return_when=asyncio.FIRST_COMPLETED
            )
            for job in done:
                running.pop(job, None)

        # Stop anything still in flight (budget / cancel / failure fast-path).
        if running:
            for job in running:
                job.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for task in running.values():
                if task.status not in ("done", "failed"):
                    task.status = "skipped"
                    task.error = run.error or "运行已取消"
                    task.finished_at = now()
                    self.store.update_task(task)

        # Mark tasks that can never run because an upstream failed.
        for task in run.tasks:
            if task.status == "pending":
                task.status = "skipped"
                task.error = "上游任务失败或运行已终止"
                self.store.update_task(task)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute_direct(self, run: RunState, task: TaskState) -> None:
        """Single-model mode: one plain model call, no agent role or tools."""
        provider_id = run.cluster.get("planner_id") or None
        provider = self.store.get_provider(provider_id) if provider_id else None
        task.model_id = provider_id
        task.model_name = (
            f"{provider['name']} · {provider['model']}" if provider else "默认"
        )
        task.assignee = None
        task.status = "running"
        task.started_at = now()
        task.attempts += 1
        self.store.update_task(task)
        await self.bus.publish(
            run.id,
            "task.started",
            {
                "title": task.title,
                "agent": "单模型",
                "model": task.model_name,
            },
            task.id,
        )
        pump = _DeltaPump(self.bus, run.id, task.id, "单模型", 1, "result")
        try:
            self._check_budget(run)
            if run.cancelled:
                raise RunCancelled("用户取消")
            response = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是 AI 助手。直接、清晰地回答用户问题。"
                            "不要描述内部流程，也不要假装调用工具。"
                        ),
                    },
                    {"role": "user", "content": run.goal},
                ],
                None,
                task=task,
                run=run,
                agent=None,
                step=0,
                provider_id=provider_id,
                on_delta=pump,
            )
            await pump.flush()
            self._account(
                run, task, response.prompt_tokens, response.completion_tokens
            )
            task.result = response.content.strip() or "（模型未返回内容）"
            task.status = "done"
            task.finished_at = now()
            self.store.update_task(task)
            await self.bus.publish(
                run.id,
                "task.completed",
                {
                    "title": task.title,
                    "agent": "单模型",
                    "model": task.model_name,
                    "result": task.result[:2000],
                    "steps": task.steps,
                    "tokens": task.tokens,
                },
                task.id,
            )
            run.result = task.result
            run.status = "succeeded"
            await self.bus.publish(
                run.id,
                "run.completed",
                {"result": run.result, "usage": self._usage(run)},
            )
        except asyncio.CancelledError:
            if run.shutting_down:
                # Leave the task resumable; the startup recovery pass will
                # put it back to pending and restore its checkpoint.
                task.status = "pending"
                task.error = ""
            else:
                task.status = "skipped"
                task.error = "运行已取消"
                task.finished_at = now()
            self.store.update_task(task)
            raise
        except (BudgetExceeded, RunCancelled) as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = now()
            run.error = str(exc)
            run.status = "failed"
            self.store.update_task(task)
            await self.bus.publish(
                run.id, "task.failed", {"error": task.error}, task.id
            )
            await self.bus.publish(
                run.id, "run.failed", {"error": run.error}
            )
        except Exception as exc:  # noqa: BLE001 - single call boundary
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.finished_at = now()
            run.error = task.error
            run.status = "failed"
            self.store.update_task(task)
            await self.bus.publish(
                run.id, "task.failed", {"error": task.error}, task.id
            )
            await self.bus.publish(
                run.id, "run.failed", {"error": run.error}
            )

    async def _execute(self, run: RunState, task: TaskState, agent: AgentSpec) -> None:
        # Prefer the model chosen by the router, then fall back to the next
        # best model if the first one times out or is temporarily unavailable.
        allowed_ids = self._allowed_ids(run)
        candidates = self.router.candidates(task.capability, allowed_ids)
        if allowed_ids is not None and not candidates:
            task.status = "failed"
            task.error = "集群内没有可用模型"
            task.finished_at = now()
            run.error = task.error
            await self.bus.publish(run.id, "task.failed", {"error": task.error}, task.id)
            self.store.update_task(task)
            return
        if task.model_id:
            candidates = [p for p in candidates if p["id"] == task.model_id] + [
                p for p in candidates if p["id"] != task.model_id
            ]
        attempts = candidates[:2] or [None]
        if len(attempts) == 1:
            attempts = attempts * 2  # one retry when only one model is enabled

        task.status = "running"
        task.started_at = now()
        self.store.update_task(task)
        await self.bus.publish(
            run.id,
            "task.started",
            {
                "title": task.title,
                "agent": agent.name,
                "model": task.model_name or "默认",
            },
            task.id,
        )
        try:
            last_error: Exception | None = None
            for index, model in enumerate(attempts):
                task.attempts += 1
                if model is not None:
                    task.model_id = model["id"]
                    task.model_name = f"{model['name']} · {model['model']}"
                else:
                    task.model_name = task.model_name or "默认"
                self.store.update_task(task)
                if index > 0:
                    await self.bus.publish(
                        run.id,
                        "task.retried",
                        {
                            "error": str(last_error)[:500] if last_error else "",
                            "model": task.model_name,
                            "attempt": task.attempts,
                        },
                        task.id,
                    )
                try:
                    await self._agent_loop(
                        run,
                        task,
                        agent,
                        provider_id=model["id"] if model is not None else None,
                    )
                    task.status = "done"
                    task.finished_at = now()
                    await self.bus.publish(
                        run.id,
                        "task.completed",
                        {
                            "title": task.title,
                            "agent": agent.name,
                            "model": task.model_name,
                            "result": task.result[:2000],
                            "steps": task.steps,
                            "tokens": task.tokens,
                        },
                        task.id,
                    )
                    return
                except (BudgetExceeded, RunCancelled):
                    raise
                except LLMError as exc:
                    last_error = exc
                    if not exc.retryable or index == len(attempts) - 1:
                        raise
            if last_error is not None:
                raise last_error
        except (BudgetExceeded, RunCancelled) as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = now()
            run.error = str(exc)
            await self.bus.publish(run.id, "task.failed", {"error": str(exc)}, task.id)
        except asyncio.CancelledError:
            if run.shutting_down:
                task.status = "pending"
                task.error = ""
            else:
                task.status = "skipped"
                task.error = "运行已取消"
                task.finished_at = now()
            raise
        except Exception as exc:  # noqa: BLE001 - task boundary
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.finished_at = now()
            await self.bus.publish(run.id, "task.failed", {"error": task.error}, task.id)
        finally:
            self.store.update_task(task)

    async def _agent_loop(
        self,
        run: RunState,
        task: TaskState,
        agent: AgentSpec,
        provider_id: str | None = None,
    ) -> None:
        upstream = self._upstream_summary(run, task)
        saved = task.checkpoint or {}
        if saved.get("messages"):
            # Resume after a process restart or a failed attempt. The last
            # completed tool batch is already in the checkpoint; a tool that
            # was in flight at crash time may run again (at-least-once).
            messages = list(saved["messages"])
            observations = [
                (str(item[0]), dict(item[1]), str(item[2]))
                for item in saved.get("observations", [])
                if isinstance(item, list) and len(item) == 3
            ]
            nudged = bool(saved.get("nudged"))
            start_step = max(0, int(saved.get("next_step") or 0))
        else:
            messages = [
                {
                    "role": "system",
                    "content": agent.system_prompt
                    + f"\n你是集群成员「{agent.name}」，当前运行目标：{run.goal}",
                },
                {
                    "role": "user",
                    "content": (
                        f"任务：{task.title}\n"
                        f"指令：{task.instruction}\n\n"
                        f"上游结果摘要：\n{upstream or '（无，本任务是入口任务）'}\n\n"
                        "请现在执行任务：需要事实时立即调用工具，不要只描述计划或说“我会……”。"
                        "最终结论要基于工具结果，不要编造来源。"
                    ),
                },
            ]
            observations = []
            nudged = False
            start_step = 0
        ctx = tool_registry.build_context(
            self.store,
            self.settings,
            run.id,
            task.id,
            mcp=self.mcp,
            remote=self._remote_for(run),
        )
        tool_schemas = tool_registry.schemas(agent.tools)
        if "mcp" in agent.tools and self.mcp is not None:
            tool_schemas += self.mcp.schemas()
        max_steps = min(agent.max_steps, self.settings.max_steps_per_task)
        compacted_notified = False

        for step in range(start_step, max_steps):
            self._check_budget(run)
            if run.cancelled:
                raise RunCancelled("用户取消")

            notes = self._take_guidance(run.id, task.id)
            if notes:
                messages.append(
                    {
                        "role": "user",
                        "content": "来自对话模型的插话/引导（请在本步优先考虑）：\n"
                        + "\n".join(f"- {note}" for note in notes),
                    }
                )
                await self.bus.publish(
                    run.id,
                    "agent.guided",
                    {"title": task.title, "notes": notes},
                    task.id,
                )

            context_messages, context_tokens = self._context_messages(messages)
            if context_messages is not messages and not compacted_notified:
                compacted_notified = True
                await self.bus.publish(
                    run.id,
                    "context.compacted",
                    {
                        "task": task.title,
                        "tokens_before": estimate_tokens(messages),
                        "tokens_after": context_tokens,
                        "messages": len(context_messages),
                    },
                    task.id,
                )

            pump = _DeltaPump(
                self.bus, run.id, task.id, agent.name, step + 1, "thinking"
            )
            response = await self.llm.chat(
                context_messages,
                tool_schemas or None,
                task=task,
                run=run,
                agent=agent,
                step=step,
                observations=observations,
                provider_id=provider_id,
                on_delta=pump,
            )
            await pump.flush()
            self._account(run, task, response.prompt_tokens, response.completion_tokens)
            await self.bus.publish(
                run.id,
                "agent.step",
                {
                    "agent": agent.name,
                    "step": step + 1,
                    "content": response.content[:1200],
                    "tools": [c.name for c in response.tool_calls],
                    "tokens": response.total_tokens,
                },
                task.id,
            )

            if response.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for call in response.tool_calls
                        ],
                    }
                )
                for call in response.tool_calls:
                    self._check_budget(run)
                    await self.bus.publish(
                        run.id,
                        "tool.called",
                        {"tool": call.name, "args": call.arguments},
                        task.id,
                    )
                    try:
                        approved = True
                        if (
                            self.approvals is not None
                            and self.settings.command_approval
                            and call.name == "run_command"
                        ):
                            approved = await self.approvals.request(
                                run.id,
                                task.id,
                                call.name,
                                call.arguments,
                            )
                        if not approved:
                            result = json.dumps(
                                {
                                    "error": "用户未批准该命令，已跳过执行。"
                                    "请调整方案，或向用户说明需要手动执行。"
                                },
                                ensure_ascii=False,
                            )
                            await self.bus.publish(
                                run.id,
                                "tool.rejected",
                                {"tool": call.name, "args": call.arguments},
                                task.id,
                            )
                        else:
                            result = await tool_registry.call(
                                call.name, call.arguments, ctx
                            )
                    except Exception as exc:  # noqa: BLE001 - tool errors are data
                        result = json.dumps(
                            {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                        )
                    observations.append((call.name, call.arguments, result))
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result}
                    )
                    await self.bus.publish(
                        run.id,
                        "tool.result",
                        {"tool": call.name, "result": result[:1500]},
                        task.id,
                    )
                self._save_checkpoint(
                    task, messages, observations, nudged, step + 1
                )
                continue

            content = response.content.strip()
            # Some models answer with "I will first check..." instead of doing
            # the work. Nudge once before treating that as the final result.
            if (
                not nudged
                and step == 0
                and not observations
                and (not content or self._looks_like_plan(content))
            ):
                nudged = True
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "不要只描述计划或说“我会……”。请立即调用工具获取事实；"
                            "如果已经可以直接回答，请给出包含具体发现的结论。"
                        ),
                    }
                )
                self._save_checkpoint(
                    task, messages, observations, nudged, step + 1
                )
                continue

            task.result = content or "（模型未返回内容）"
            self._clear_checkpoint(task)
            return

        # Reserve one extra turn for a conclusion without tools. This avoids
        # wasting the whole task when the model keeps calling tools until the
        # step limit.
        self._check_budget(run)
        messages.append(
            {
                "role": "user",
                "content": (
                    "已达到工具调用上限。不要再调用工具，请基于上面的工具结果"
                    "直接给出最终答复；信息不足时明确说明不确定性。"
                ),
            }
        )
        context_messages, _ = self._context_messages(messages)
        response = await self.llm.chat(
            context_messages,
            None,
            task=task,
            run=run,
            agent=agent,
            step=max_steps,
            observations=observations,
            provider_id=provider_id,
        )
        self._account(run, task, response.prompt_tokens, response.completion_tokens)
        await self.bus.publish(
            run.id,
            "agent.step",
            {
                "agent": agent.name,
                "step": max_steps + 1,
                "content": response.content[:1200],
                "tools": [],
                "tokens": response.total_tokens,
            },
            task.id,
        )
        content = response.content.strip()
        if not content:
            lines = [
                f"- {name}({json.dumps(args, ensure_ascii=False)[:120]}): {result[:400]}"
                for name, args, result in observations
            ]
            content = (
                "已达到单任务步数上限。以下是已收集的工具结果：\n"
                + ("\n".join(lines) if lines else "没有可用的工具结果。")
            )
        task.result = content
        self._clear_checkpoint(task)

    @staticmethod
    def _looks_like_plan(text: str) -> bool:
        if not text or len(text) > 300:
            return False
        markers = (
            "我会先",
            "我将先",
            "我会",
            "我将",
            "先确认",
            "先核实",
            "接下来我会",
            "I will",
            "I'll",
            "Let me",
        )
        return any(marker in text for marker in markers)

    # ------------------------------------------------------------------
    # Budget / usage / summary
    # ------------------------------------------------------------------

    def _check_budget(self, run: RunState) -> None:
        budget = run.budget
        if run.steps >= budget.max_steps:
            raise BudgetExceeded(f"超过步数预算（{budget.max_steps}，已用 {run.steps}）")
        if run.tokens >= budget.max_tokens:
            raise BudgetExceeded(
                f"超过 Token 预算（{budget.max_tokens:,}，已用 {run.tokens:,}）"
            )
        if run.cost_usd >= budget.max_usd:
            raise BudgetExceeded(f"超过金额预算（${budget.max_usd:.2f}）")
        if time.time() > run.deadline_at:
            raise BudgetExceeded("超过运行截止时间")

    def _account(self, run: RunState, task: TaskState, prompt_tokens: int, completion_tokens: int) -> None:
        total = prompt_tokens + completion_tokens
        run.steps += 1
        run.prompt_tokens += prompt_tokens
        run.completion_tokens += completion_tokens
        run.tokens += total
        task.steps += 1
        task.tokens += total
        if not self.llm.is_mock:
            run.cost_usd += (
                prompt_tokens / 1_000_000 * self.settings.price_in_per_m
                + completion_tokens / 1_000_000 * self.settings.price_out_per_m
            )

    @staticmethod
    def _usage(run: RunState) -> dict[str, Any]:
        return {
            "steps": run.steps,
            "tokens": run.tokens,
            "cost_usd": round(run.cost_usd, 6),
            "tasks": len(run.tasks),
        }

    @staticmethod
    def _upstream_summary(run: RunState, task: TaskState) -> str:
        """Pass summaries, not full transcripts. This is the token-budget firewall."""
        parts = []
        for dep_id in task.depends_on:
            dep = next((t for t in run.tasks if t.id == dep_id), None)
            if dep and dep.status == "done" and dep.result:
                parts.append(f"### {dep.title}\n{dep.result[:1200]}")
        return "\n\n".join(parts)

    def _save_checkpoint(
        self,
        task: TaskState,
        messages: list[dict[str, Any]],
        observations: list[tuple[str, dict[str, Any], str]],
        nudged: bool,
        next_step: int,
    ) -> None:
        task.checkpoint = {
            "messages": messages,
            "observations": [
                [name, args, result] for name, args, result in observations
            ],
            "nudged": nudged,
            "next_step": next_step,
        }
        self.store.update_task(task)

    def _clear_checkpoint(self, task: TaskState) -> None:
        if task.checkpoint is None:
            return
        task.checkpoint = None
        self.store.update_task(task)

    def _context_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a compact prompt view without touching the raw transcript.

        Older tool/assistant turns are replaced by a short textual digest. The
        originals remain in the step checkpoint, so replay and audit still see
        the full L0 record.
        """
        tokens = estimate_tokens(messages)
        if (
            tokens <= self.settings.context_compact_tokens
            or len(messages) <= 12
        ):
            return messages, tokens

        keep = 10
        start = max(2, len(messages) - keep)
        # Never start the retained tail with an orphan tool result.
        while start > 2 and messages[start].get("role") == "tool":
            start -= 1
        head = messages[:2]
        middle = messages[2:start]
        tail = messages[start:]
        if not middle:
            return messages, tokens

        lines: list[str] = []
        for item in middle:
            role = item.get("role", "unknown")
            content = str(item.get("content") or "").replace("\n", " ")
            if role == "tool":
                lines.append(f"- 工具结果：{content[:300]}")
            elif role == "assistant":
                names = [
                    str((call.get("function") or {}).get("name") or "")
                    for call in item.get("tool_calls") or []
                ]
                suffix = f" [调用 {', '.join(n for n in names if n)}]" if names else ""
                lines.append(f"- 助手：{content[:300]}{suffix}")
            elif role == "user":
                lines.append(f"- 用户：{content[:300]}")
        digest = "【较早步骤的上下文压缩（完整原文仍在运行记录中）】\n" + "\n".join(
            lines
        )
        if len(digest) > 6000:
            digest = digest[:6000] + "\n…（更早内容已省略）"
        compacted = [
            head[0],
            head[1],
            {"role": "user", "content": digest},
            *tail,
        ]
        return compacted, estimate_tokens(compacted)

    async def _finalize(self, run: RunState) -> str:
        max_round = max((task.round for task in run.tasks), default=1)
        finished = [
            t
            for t in run.tasks
            if t.round == max_round and t.status == "done" and t.result
        ]
        if self.llm.is_mock or not finished:
            body = "\n\n".join(f"## {t.title}\n{t.result}" for t in finished)
            return f"# 集群执行完成\n\n目标：{run.goal}\n\n{body}\n\n---\n{self._usage_line(run)}"

        material = "\n\n".join(f"## {t.title}\n{t.result[:2000]}" for t in finished)
        prompt = (
            "你是集群的汇总 Agent。基于各任务结果，给用户一份最终交付。\n"
            "要求：直接给结论，结构清晰，不重复过程，不引入未出现的事实。\n\n"
            f"目标：{run.goal}\n\n{material}"
        )
        pump = _DeltaPump(
            self.bus, run.id, None, "汇总 Agent", 0, "result"
        )
        try:
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                provider_id=run.cluster.get("planner_id") or None,
                on_delta=pump,
            )
            await pump.flush()
            self._account(run, run.tasks[-1], resp.prompt_tokens, resp.completion_tokens)
            return resp.content.strip() or material
        except LLMError:
            return material

    @staticmethod
    def _usage_line(run: RunState) -> str:
        return (
            f"用量：{run.steps} 步 · {run.tokens:,} tokens · "
            f"${run.cost_usd:.4f}" + ("（mock 模式不计费）" if run.cost_usd == 0 else "")
        )

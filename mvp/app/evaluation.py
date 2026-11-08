from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from .config import Settings
from .llm import LLMClient
from .models import new_id
from .store import Store


DOMAIN_LABELS = {
    "research": "调研",
    "analysis": "分析",
    "writing": "写作",
    "review": "审校",
    "frontend": "前端",
    "backend": "后端",
    "devops": "运维",
    "code": "通用编码",
    "general": "通用",
}


EVAL_TESTS: dict[str, dict[str, Any]] = {
    "research": {
        "prompt": (
            "为「单机部署、并发低于 100 的多 Agent 平台」在 SQLite 与 PostgreSQL 之间做选型。"
            "给出 3 条结论，每条注明证据类型（官方文档/基准测试/工程经验），"
            "并指出 1 个必须实测验证的假设。不超过 220 字。"
        ),
        "criteria": "3 条结论明确；证据类型标注；结合单机低并发规模；指出可验证假设；不编造基准数字。",
        "checks": [
            "给出 3 条独立结论，而不是泛泛而谈",
            "每条结论都标注了证据类型",
            "结论针对单机部署、并发低于 100 的场景",
            "指出 1 个必须实测验证的假设",
            "没有编造具体基准数字或来源",
        ],
    },
    "analysis": {
        "prompt": (
            "一条 4 阶段流水线的单阶段耗时分别为 2s、3s、1s、2s，各阶段并行度分别为 4、2、1、1。"
            "计算吞吐上限（任务/分钟）、指出瓶颈阶段，并给出 2 条提升吞吐的措施及各自副作用。"
            "必须写出计算过程，不超过 220 字。"
        ),
        "criteria": "正确计算各阶段容量与瓶颈；吞吐上限为 30 任务/分钟；措施针对瓶颈；说明副作用；过程清晰。",
        "checks": [
            "计算了每个阶段的容量（并行度除以耗时）",
            "正确指出瓶颈是第 4 阶段（并行度 1、耗时 2s）",
            "吞吐上限正确为 0.5 任务/秒，即 30 任务/分钟",
            "给出 2 条针对瓶颈的措施",
            "说明每条措施的副作用或代价",
        ],
    },
    "writing": {
        "prompt": (
            "为内部技术评审写一段 120–150 字的项目简介：面向 20 人研发团队，"
            "说明它解决什么问题、如何工作（最多 2 个机制）、一个已知限制，"
            "以及一个可量化验收指标。不得使用“颠覆”“革命性”“万能”。"
        ),
        "criteria": "120–150 字；问题/机制/限制/指标齐全；最多 2 个机制；无禁用词；不夸大；读者匹配。",
        "checks": [
            "正文在 120–150 字之间",
            "说明了要解决的具体问题",
            "只描述了最多 2 个工作机制",
            "包含一个已知限制",
            "包含一个可量化验收指标",
            "未出现“颠覆”“革命性”“万能”",
        ],
    },
    "review": {
        "prompt": (
            "审校下面这段话，指出至少 3 个事实或逻辑问题，并给出 100 字以内的改写："
            "“我们的 Agent 平台用 REST 轮询实现实时推送，延迟稳定在 5ms 以内；"
            "因为使用多 Agent，成本比单 Agent 低 90%；所有任务都适合并行拆解。”"
        ),
        "criteria": "识别轮询与实时推送矛盾；质疑 5ms 稳定性；质疑成本降低 90% 的依据；指出并非所有任务可并行；改写合理。",
        "checks": [
            "指出 REST 轮询不等于实时推送",
            "质疑“延迟稳定在 5ms 以内”缺乏依据或不现实",
            "质疑“成本低 90%”缺少依据",
            "指出并非所有任务都适合并行拆解",
            "给出 100 字以内、去掉绝对化表述的改写",
        ],
    },
    "frontend": {
        "prompt": (
            "用原生 HTML/JS 实现搜索框：输入 300ms 防抖，请求 /api/search?q=，"
            "空查询不发请求，新请求用 AbortController 取消旧请求，显示 loading 与错误，"
            "组件销毁时清理定时器和请求。只输出关键代码，不超过 70 行，不要解释。"
        ),
        "criteria": "300ms 防抖；空查询短路；AbortController 取消旧请求；loading/error 状态；销毁清理；代码可运行。",
        "checks": [
            "实现 300ms 防抖",
            "空查询直接返回、不发请求",
            "用 AbortController 取消上一个请求",
            "有 loading 与错误显示",
            "卸载或销毁时清理定时器并 abort 请求",
            "代码不超过 70 行且无明显语法错误",
        ],
    },
    "backend": {
        "prompt": (
            "设计任务队列 API：POST /jobs 提交（客户端可重试），GET /jobs/{id} 查询，"
            "POST /jobs/{id}/cancel 取消；状态为 queued/running/succeeded/failed/cancelled。"
            "要求同一 Idempotency-Key 只创建一个任务；多个 worker 不能重复领取同一任务；"
            "支持取消 running 任务。给出表结构（含唯一约束）、状态转换、领取任务的原子 SQL、"
            "失败重试策略。不超过 260 字。"
        ),
        "criteria": "幂等唯一约束；状态机完整；原子领取；取消 running 语义；重试与退避；API 完整。",
        "checks": [
            "表结构包含 Idempotency-Key 唯一约束",
            "定义了完整状态机与合法状态转换",
            "领取任务的 SQL 是原子操作（如 UPDATE … WHERE status='queued' RETURNING）",
            "说明如何取消 running 任务（标记取消并让 worker 检查）",
            "重试有次数上限与退避策略",
            "覆盖三个 API 的语义",
        ],
    },
    "devops": {
        "prompt": (
            "FastAPI 服务部署在 K8s 后偶发 502 和重启。请给出从外到内的排查顺序（至少 4 步）、"
            "需要采集的 5 项指标或日志，以及 Deployment 中避免滚动更新中断的关键配置。不超过 240 字。"
        ),
        "criteria": "排查顺序合理；指标具体；区分 readiness/liveness；滚动更新参数；优雅终止；资源限制。",
        "checks": [
            "给出至少 4 步、由外到内的排查顺序",
            "列出 5 项具体指标或日志",
            "区分 readinessProbe 与 livenessProbe 的用途",
            "配置 maxUnavailable/maxSurge 或等价策略",
            "配置 terminationGracePeriodSeconds 与 preStop 优雅终止",
            "提到资源 requests/limits 与 OOM 排查",
        ],
    },
    "code": {
        "prompt": (
            "实现 merge_intervals(intervals)：合并所有重叠或相邻的区间，输入可能为空、乱序、"
            "包含相同起点；返回按起点排序的新列表，不修改输入。只输出 Python 代码和两行复杂度说明。"
            "示例：[[1,3],[2,6],[8,10],[15,18]] -> [[1,6],[8,10],[15,18]]；"
            "[[1,2],[3,4]] -> [[1,4]]。"
        ),
        "criteria": "函数名与签名正确；空输入；排序；相邻整数区间按 start <= last_end + 1 合并；不修改输入；复杂度说明准确。",
        "checks": [
            "函数名为 merge_intervals，参数为 intervals",
            "空输入返回 []",
            "先按起点排序，能处理乱序输入",
            "相邻区间也合并（整数区间按 start <= last_end + 1 判断，如 [1,2] 与 [3,4] 合并为 [1,4]）",
            "不原地修改输入列表",
            "复杂度说明为 O(n log n) 时间、O(n) 空间或等价正确表述",
        ],
    },
    "general": {
        "prompt": (
            "用不超过 3 句话向产品经理解释什么是“幂等”，必须包含一个生活类比和一个 HTTP API 例子，"
            "不得使用“顾名思义”“简单来说”。"
        ),
        "criteria": "准确；不超过 3 句；有生活类比；有 API 例子；无禁用词；面向非工程读者。",
        "checks": [
            "解释准确：同一操作重复执行与执行一次效果相同",
            "不超过 3 句话",
            "包含一个生活类比",
            "包含一个 HTTP API 例子（如重复提交同一 Idempotency-Key）",
            "未使用“顾名思义”“简单来说”",
        ],
    },
}


class EvaluationRunner:
    """Runs a small prompt suite against a model and asks a judge to score answers."""

    def __init__(self, store: Store, llm: LLMClient, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.settings = settings
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._batches: dict[str, dict[str, Any]] = {}
        self._batch_tasks: dict[str, asyncio.Task[Any]] = {}

    def _resolve_judge(self, judge_id: str | None) -> dict[str, Any]:
        judge = (
            self.store.get_provider(judge_id)
            if judge_id
            else self.store.get_active_provider()
        )
        if judge is None:
            raise ValueError("没有可用的评分模型")
        if self.llm.is_mock:
            raise ValueError("mock 模式不能实测评分，请先配置真实模型")
        return judge

    def _create_job(
        self, target: dict[str, Any], judge: dict[str, Any]
    ) -> dict[str, Any]:
        job_id = new_id("eval")
        job: dict[str, Any] = {
            "id": job_id,
            "provider_id": target["id"],
            "provider_name": target["name"],
            "model": target["model"],
            "judge": f'{judge["name"]} · {judge["model"]}',
            "status": "running",
            "total": len(EVAL_TESTS),
            "completed": 0,
            "current": "",
            "scores": {},
            "results": {},
            "errors": {},
            "usage": {
                "target_tokens": 0,
                "judge_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0,
            },
            "created_at": time.time(),
            "finished_at": None,
        }
        self._jobs[job_id] = job
        self.store.save_evaluation(job)
        if len(self._jobs) > 100:
            oldest = sorted(self._jobs, key=lambda key: self._jobs[key]["created_at"])
            for key in oldest[:-100]:
                self._jobs.pop(key, None)
        return job

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self, target_id: str, judge_id: str | None = None) -> str:
        target = self.store.get_provider(target_id)
        if target is None:
            raise ValueError("provider not found")
        judge = self._resolve_judge(judge_id)
        job = self._create_job(target, judge)
        self._spawn(self._run(job, target, judge))
        return job["id"]

    async def start_batch(
        self,
        provider_ids: list[str] | None = None,
        judge_id: str | None = None,
    ) -> str:
        judge = self._resolve_judge(judge_id)
        if provider_ids:
            targets = [self.store.get_provider(pid) for pid in provider_ids]
            targets = [target for target in targets if target is not None]
        else:
            targets = self.store.get_enabled_providers()
        if not targets:
            raise ValueError("没有可评测的模型")

        batch_id = new_id("batch")
        batch: dict[str, Any] = {
            "id": batch_id,
            "judge": f'{judge["name"]} · {judge["model"]}',
            "status": "running",
            "total": len(targets),
            "completed": 0,
            "current": "",
            "error": "",
            "created_at": time.time(),
            "finished_at": None,
            "items": [
                {
                    "provider_id": target["id"],
                    "provider_name": target["name"],
                    "model": target["model"],
                    "job_id": "",
                    "status": "pending",
                    "average": None,
                    "completed": 0,
                    "total": len(EVAL_TESTS),
                }
                for target in targets
            ],
        }
        self._batches[batch_id] = batch
        task = self._spawn(self._run_batch(batch, targets, judge))
        self._batch_tasks[batch_id] = task
        task.add_done_callback(lambda done: self._batch_tasks.pop(batch_id, None))
        return batch_id

    async def _run_batch(
        self,
        batch: dict[str, Any],
        targets: list[dict[str, Any]],
        judge: dict[str, Any],
    ) -> None:
        active_job: dict[str, Any] | None = None
        try:
            for item, target in zip(batch["items"], targets):
                if batch["status"] != "running":
                    return
                batch["current"] = f'{target["name"]} · {target["model"]}'
                item["status"] = "running"
                active_job = self._create_job(target, judge)
                item["job_id"] = active_job["id"]
                await self._run(active_job, target, judge)
                item["status"] = active_job["status"]
                item["average"] = active_job.get("average")
                item["completed"] = active_job.get("completed", 0)
                batch["completed"] += 1
                active_job = None
        except asyncio.CancelledError:
            if active_job is not None and active_job.get("status") == "running":
                active_job["status"] = "cancelled"
                active_job["error"] = "用户停止实测"
                active_job["finished_at"] = time.time()
                self.store.save_evaluation(active_job)
            for item in batch["items"]:
                if item["status"] in ("pending", "running"):
                    item["status"] = "cancelled"
            batch["status"] = "cancelled"
            raise
        finally:
            if batch["status"] == "running":
                done = [item for item in batch["items"] if item["status"] == "done"]
                batch["status"] = "done" if done else "failed"
                if not done and not batch.get("error"):
                    batch["error"] = "所有模型都评测失败"
            batch["current"] = ""
            batch["finished_at"] = time.time()

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = self._batches.get(batch_id)
        return json.loads(json.dumps(batch)) if batch else None

    async def cancel_batch(self, batch_id: str) -> bool:
        batch = self._batches.get(batch_id)
        if batch is None or batch["status"] != "running":
            return False
        batch["status"] = "cancelling"
        task = self._batch_tasks.get(batch_id)
        if task is not None:
            task.cancel()
        else:
            batch["status"] = "cancelled"
            batch["finished_at"] = time.time()
        return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            job = self.store.get_evaluation(job_id)
            if job is not None:
                job["total"] = len(EVAL_TESTS)
        return json.loads(json.dumps(job)) if job else None

    async def _run(
        self, job: dict[str, Any], target: dict[str, Any], judge: dict[str, Any]
    ) -> None:
        semaphore = asyncio.Semaphore(3)

        async def run_one(domain: str, spec: dict[str, Any]) -> None:
            async with semaphore:
                job["current"] = domain
                started = time.perf_counter()
                try:
                    answer_response = await self.llm.chat(
                        [{"role": "user", "content": spec["prompt"]}],
                        None,
                        provider_id=target["id"],
                    )
                    answer = answer_response.content.strip()
                    if not answer:
                        raise ValueError("模型返回空内容")

                    checks_block = "\n".join(
                        f"{index}. {item}"
                        for index, item in enumerate(spec.get("checks") or [], 1)
                    )
                    judge_prompt = (
                        "你是严格的技术评审，只依据回答本身评分，不猜测作者意图。\n"
                        f"维度：{DOMAIN_LABELS[domain]}（{domain}）\n"
                        f"任务：{spec['prompt']}\n"
                        f"评分标准：{spec['criteria']}\n\n"
                        "硬性检查点（逐项核对；只有回答明确满足才记为 true）：\n"
                        f"{checks_block or '（无）'}\n\n"
                        "评分规则：\n"
                        "- 先逐项给出 pass 与简短证据；\n"
                        "- 任一检查点为 fail，总分不得超过 6；\n"
                        "- 全部 pass：7–8 分；准确、简洁、无冗余：9 分；"
                        "有额外正确洞察：10 分；\n"
                        "- 事实错误、编造来源/数字、违反字数或格式约束：每项扣 1–2 分；\n"
                        "- 不要因为篇幅长而加分，空泛内容不得高于 5 分。\n\n"
                        f"回答：\n{answer[:6000]}\n\n"
                        "只输出 JSON："
                        '{"checks":[{"item":"...","pass":true,"note":"..."}],'
                        '"score":0,"reason":"不超过 120 字","missing":["..."]}'
                    )
                    judge_response = await self.llm.chat(
                        [{"role": "user", "content": judge_prompt}],
                        None,
                        provider_id=judge["id"],
                    )
                    score, reason, missing, checks = _parse_judge(
                        judge_response.content, spec
                    )
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    target_tokens = answer_response.total_tokens
                    judge_tokens = judge_response.total_tokens
                    job["scores"][domain] = score
                    job["results"][domain] = {
                        "score": score,
                        "reason": reason,
                        "missing": missing,
                        "checks": checks,
                        "answer_preview": answer[:800],
                        "latency_ms": elapsed_ms,
                        "target_tokens": target_tokens,
                        "judge_tokens": judge_tokens,
                    }
                    usage = job["usage"]
                    usage["target_tokens"] += target_tokens
                    usage["judge_tokens"] += judge_tokens
                    usage["total_tokens"] = usage["target_tokens"] + usage["judge_tokens"]
                    usage["latency_ms"] += elapsed_ms
                    usage["cost_usd"] = round(
                        (answer_response.prompt_tokens + judge_response.prompt_tokens)
                        / 1_000_000
                        * self.settings.price_in_per_m
                        + (
                            answer_response.completion_tokens
                            + judge_response.completion_tokens
                        )
                        / 1_000_000
                        * self.settings.price_out_per_m,
                        6,
                    )
                except Exception as exc:  # noqa: BLE001 - one failing test should not kill the job
                    job["errors"][domain] = f"{type(exc).__name__}: {exc}"
                finally:
                    job["completed"] += 1
                    job["current"] = ""
                    self.store.save_evaluation(job)

        try:
            await asyncio.gather(
                *(run_one(domain, spec) for domain, spec in EVAL_TESTS.items())
            )
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            job["error"] = job.get("error") or "实测已停止"
            job["finished_at"] = time.time()
            self.store.save_evaluation(job)
            raise
        job["status"] = "done" if job["scores"] else "failed"
        if not job["scores"] and not job.get("error"):
            job["error"] = "所有评测项都失败了"
        job["average"] = (
            round(sum(job["scores"].values()) / len(job["scores"]), 1)
            if job["scores"]
            else 0
        )
        job["finished_at"] = time.time()
        self.store.save_evaluation(job)


def _parse_judge(
    text: str, spec: dict[str, Any] | None = None
) -> tuple[float, str, list[str], list[dict[str, Any]]]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            score = float(data.get("score", 5))
            reason = str(data.get("reason", ""))[:300]
            missing = [str(item)[:120] for item in (data.get("missing") or [])][:5]
            score = max(0.0, min(10.0, round(score, 1)))
            expected = list((spec or {}).get("checks") or [])
            checks: list[dict[str, Any]] = []
            for index, item in enumerate(data.get("checks") or []):
                if not isinstance(item, dict):
                    continue
                label = str(
                    item.get("item")
                    or (expected[index] if index < len(expected) else "")
                )[:160]
                raw_pass = item.get("pass")
                if isinstance(raw_pass, bool):
                    passed: bool | None = raw_pass
                elif isinstance(raw_pass, str):
                    passed = raw_pass.strip().lower() in ("true", "yes", "pass", "1")
                else:
                    passed = None
                checks.append(
                    {
                        "item": label,
                        "pass": passed,
                        "note": str(item.get("note", ""))[:160],
                    }
                )
            if not checks and expected:
                checks = [
                    {"item": item, "pass": None, "note": "评分模型未逐项核对"}
                    for item in expected
                ]
            failed = [check["item"] for check in checks if check["pass"] is False]
            if failed:
                score = min(score, 6.0)
                missing = list(dict.fromkeys(failed + missing))[:6]
            return score, reason, missing, checks
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", raw)
    if match:
        return max(0.0, min(10.0, float(match.group(1)))), raw[:300], [], []
    raise ValueError("评分模型未返回合法评分 JSON")

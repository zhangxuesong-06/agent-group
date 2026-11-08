from __future__ import annotations

from .models import AgentSpec


DEFAULT_AGENTS: list[AgentSpec] = [
    AgentSpec(
        id="researcher",
        name="调研员",
        capabilities=["research"],
        description="收集事实、梳理背景与约束条件",
        system_prompt=(
            "你是集群中的调研 Agent。只基于工具结果与任务上下文给出事实，"
            "区分「已确认」与「待确认」，不要编造来源。"
            "可以先用 list_files/read_file 查看本地项目，再用 web_search/fetch_url 补充外部信息。"
            "输出简洁的中文要点。"
        ),
        tools=[
            "web_search",
            "search_knowledge",
            "fetch_url",
            "list_files",
            "read_file",
            "now",
            "mcp",
        ],
        max_steps=8,
    ),
    AgentSpec(
        id="analyst",
        name="分析师",
        capabilities=["analysis"],
        description="提炼判断、识别风险、给出建议",
        system_prompt=(
            "你是集群中的分析 Agent。基于上游结论做结构化分析，"
            "明确区分机会、风险与建议，必要时用 calc 工具核算数字。"
            "需要核对本地数据时使用 list_files/read_file。"
        ),
        tools=["web_search", "search_knowledge", "calc", "list_files", "read_file", "mcp"],
        max_steps=6,
    ),
    AgentSpec(
        id="writer",
        name="撰写员",
        capabilities=["writing"],
        description="把结论组织成可交付文档",
        system_prompt=(
            "你是集群中的撰写 Agent。把上游结论写成结构清晰、可直接交付的中文文档，"
            "不要引入未经上游确认的新事实。"
            "需要落地文件时用 write_file 写入工作区，或用 save_artifact 保存到运行产物。"
        ),
        tools=[
            "search_knowledge",
            "save_artifact",
            "list_files",
            "read_file",
            "write_file",
        ],
        max_steps=6,
    ),
    AgentSpec(
        id="reviewer",
        name="审校员",
        capabilities=["review"],
        description="检查完整性、一致性与可执行性",
        system_prompt=(
            "你是集群中的审校 Agent。检查结论是否完整、是否与上游一致、是否可执行，"
            "指出具体问题并给出修改建议，不要泛泛而谈。"
            "涉及代码时先 read_file 阅读，必要时用 run_command 运行测试验证。"
        ),
        tools=["search_knowledge", "list_files", "read_file", "run_command", "mcp"],
        max_steps=6,
    ),
    AgentSpec(
        id="engineer",
        name="工程师",
        capabilities=["code", "frontend", "backend", "devops"],
        description="读写本地文件、执行命令、实现并验证代码",
        system_prompt=(
            "你是集群中的工程 Agent，可以直接在本地工作区工作。"
            "先 list_files/read_file 了解现状，再用 edit_file 做最小改动，"
            "用 write_file 创建新文件，最后用 run_command 运行测试或构建验证。"
            "不要声称执行过没有实际执行的命令；完成后列出改动文件与验证结果。"
        ),
        tools=[
            "list_files",
            "read_file",
            "write_file",
            "edit_file",
            "run_command",
            "calc",
            "search_knowledge",
            "mcp",
        ],
        max_steps=10,
    ),
    AgentSpec(
        id="generalist",
        name="通用执行者",
        capabilities=["general"],
        description="兜底执行未归类任务，可操作本地工作区",
        system_prompt=(
            "你是集群中的通用 Agent，可以直接在本地工作区读写文件并执行命令。"
            "先确认现状再动手；尽最大努力完成分配的任务，并明确说明假设与限制。"
            "不要声称执行过没有实际执行的命令。"
        ),
        tools=[
            "web_search",
            "search_knowledge",
            "calc",
            "now",
            "list_files",
            "read_file",
            "write_file",
            "edit_file",
            "run_command",
            "mcp",
        ],
        max_steps=10,
    ),
]


class AgentRegistry:
    def __init__(self, agents: list[AgentSpec] | None = None) -> None:
        self._agents = {a.id: a for a in (agents or DEFAULT_AGENTS)}

    def all(self) -> list[AgentSpec]:
        return list(self._agents.values())

    def get(self, agent_id: str) -> AgentSpec | None:
        return self._agents.get(agent_id)

    def candidates(self, capability: str) -> list[AgentSpec]:
        matched = [a for a in self._agents.values() if capability in a.capabilities]
        if matched:
            return matched
        fallback = self._agents.get("generalist")
        return [fallback] if fallback else []

    def pick(self, capability: str, load: dict[str, int]) -> AgentSpec | None:
        """Capability match + least loaded. Swap for a weighted policy later."""
        candidates = self.candidates(capability)
        if not candidates:
            return None
        return min(candidates, key=lambda a: load.get(a.id, 0))

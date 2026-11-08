# Agent 集群软件与多 Agent 系统调研

日期：2026-11-08  
范围：现有多 Agent 框架、托管平台、协议、评测与研究结论；对照当前项目（`mvp/`）给出改进优先级。

---

## 0. 结论先行

1. **多 Agent 不是默认更优。** 它适合“广度优先、可并行、上下文超过单窗口、工具多”的任务；对强依赖、长链条、需要一致决策的任务，单线程 Agent + 压缩/记忆往往更可靠。
2. **成本是硬约束。** Anthropic 的实测：多 Agent 约为普通聊天的 **15 倍 token**，普通 Agent 约为 4 倍；token 使用量本身解释了 BrowseComp 上 **80%** 的性能方差。
3. **当前项目最缺的不是“更多 Agent”，而是生产化能力**：可恢复执行、可观测性、评测、上下文工程、验证/QA、沙箱与权限。
4. **建议顺序**：先做“可恢复 + 可评测 + 可追踪”，再做“验证 Agent / 沙箱 / 记忆 / 协议扩展”。不要在可靠性和评测之前继续堆角色。
5. **WorkBuddy 专家团的核心不是角色数量，而是主理人制度 + SOP + 成员回传 + 轮次编排。** 当前项目已经具备这个骨架，下一步应把它做成可恢复、可度量、可审计的运行时。

---

## 1. 现有软件与框架地图

| 类别 | 代表 | 核心能力 | 值得借鉴 | 注意 |
|---|---|---|---|---|
| 图/状态机编排 | **LangGraph** | 图执行、持久化 checkpoint、durable execution、流式、human-in-the-loop、time travel | 把 Run 做成可 checkpoint 的状态机；中断后可恢复；支持回放 | 学习曲线；不要为了“图”而图 |
| 企业级 Agent SDK | **Microsoft Agent Framework** | Agent + Harness Agent + 函数式/图工作流；规划与 todo、上下文压缩、文件/记忆、免重复审批、可观测性 | Harness Agent 的“长任务默认能力包”；工作流显式化 | 2025 年起整合 AutoGen + Semantic Kernel，API 仍在演进 |
| 轻量 Agent SDK | **OpenAI Agents SDK** | Agents、handoffs、agents-as-tools、guardrails、sessions、tracing、sandbox agents、MCP | handoff 与 manager 两种编排；输入/输出 guardrails；内置 tracing | 强绑定 OpenAI 生态，但可接第三方模型 |
| 多语言 Agent 框架 | **Google ADK 2.0** | Python/TS/Go/Java/Kotlin；图工作流；评测与部署 | 图工作流 + 多语言；企业部署路径 | 生态偏 Google Cloud |
| Crew/Flow | **CrewAI** | Crews、Flows、guardrails、memory、knowledge、observability、持久化/恢复 | “Crew 负责协作，Flow 负责确定性流程”；Flow 可恢复 | 角色化 Crew 容易过度拆分 |
| 研究谱系 | **AutoGen / MetaGPT / ChatDev** | 多 Agent 对话、SOP、角色流水线、代码生成 | MetaGPT 的 SOP/流水线思想；AutoGen 的对话式编排 | 早期系统；现代生产更看重可靠性与评测 |
| 托管 Agent 平台 | **AWS Bedrock AgentCore** | Harness、Runtime（microVM）、Memory、Gateway（MCP）、Identity、Code Interpreter、Browser、Observability（OTel）、Policy、Registry、Evaluations、Optimization、Payments | 一份完整的“生产化清单” | 云绑定、成本；可只借架构 |
| 持久化执行 | **Temporal** | Durable Execution、长时状态、HITL、自动重试、可观测、水平扩展 | 把 Agent 循环当 workflow；失败可恢复；审批作为 signal | 引入运行时复杂度 |
| 专家团产品 | **WorkBuddy `software-company`** | 主理人 + 产品/架构/工程/QA；SOP 路由（快速/BugFix/标准/部分）；TeamCreate/SendMessage；成员不直连 | 主理人制度、阶段门禁、回传机制、按复杂度选流程 | 角色提示词长；需要控制轮次与成本 |
| 单线程代表 | **Cognition / Devin** | 强调上下文工程与单线程可靠性；子 Agent 只做边界清晰的问题 | 共享完整上下文；压缩模型；避免并行写代码 | 与“多 Agent 并行”是对照方案，不是教条 |
| 记忆层 | **Mem0 / Letta** | 事实抽取、作用域（user/agent/app/run）、长期记忆、共享记忆、MemFS、子 Agent | 记忆分层与作用域；跨会话召回 | 记忆写入策略比存储更重要 |
| 可观测/评测 | **Langfuse / LangSmith / AgentOps** | Trace、Session、Agent Graph、成本、LLM-as-judge、数据集、实验、标注队列 | 以 trace 为中心的开发闭环 | 自托管/云的选择 |
| 协议 | **MCP / A2A** | MCP：Agent→工具/数据；A2A：Agent↔Agent，跨框架互操作 | MCP 已支持；A2A 可做外部 Agent 适配 | A2A 不是子 Agent/工具协议，不能替代 MCP |

### 1.1 协议现状

- **MCP**：当前文档版本 `2026-07-28`，定位是“AI 应用的 USB-C”，连接数据源、工具、工作流；已支持 Agent Skills、Registry、安全章节。当前项目已支持 stdio / SSE / Streamable HTTP。
- **A2A**：Google 发起、捐给 Linux Foundation，2026-08 加入 **Agentic AI Foundation**；定位是“不透明 Agent 之间的互操作”，与 MCP 互补。A2A 明确不是子 Agent/工具调用协议。
- 结论：**内部子 Agent 继续用自己的调度器；对外部 Agent 再考虑 A2A 适配**。不要为了协议而拆分内部系统。

---

## 2. 关键研究结论

### 2.1 Anthropic：多 Agent 研究系统的收益与代价

来源：<https://www.anthropic.com/engineering/multi-agent-research-system>（2025-06）

- 架构：LeadResearcher + 并行 Subagents + CitationAgent；Lead 规划、派发、汇总，Subagent 独立搜索并回传。
- 内部评测：Opus 4 Lead + Sonnet 4 Subagents 比单 Agent Opus 4 **高 90.2%**（内部 research eval）。
- 成本：多 Agent 约 **15×** 普通聊天 token；普通 Agent 约 4×。
- 性能归因：BrowseComp 上 **80%** 方差由 token 使用量解释，其次是工具调用次数和模型选择。
- 速度：并行 Subagents + 并行工具调用，复杂查询最多快 **90%**。
- 适用：广度优先、信息超过单窗口、工具多的任务；**不适合**高度依赖、需要共享同一上下文的任务（大多数编码任务）。
- 工程经验：
  - 长任务要 **durable execution + checkpoint**，失败不能从头重跑。
  - 需要 **full production tracing**，监控决策模式与交互结构。
  - 同步执行是瓶颈：Lead 不能实时 steer Subagent；异步化会带来状态一致性挑战。
  - 子 Agent 产出写文件系统，只回传引用，减少“传话游戏”。
  - 评测：约 20 条真实用例即可开始；LLM-as-judge + 人工抽检；按最终状态评估，不要求固定路径。

**对本项目**：现在的“主理人 + 专家轮次”方向正确；下一步要补 durable execution、tracing、子 Agent 产物引用、异步 steer。

### 2.2 Cognition：不要轻易做多 Agent

来源：<https://cognition.ai/blog/dont-build-multi-agents>（2025-06）

- 两条原则：
  1. **共享上下文，共享完整 Agent trace，而不只是单条消息。**
  2. **动作携带隐含决策；冲突的隐含决策会产生坏结果。**
- 多 Agent 的典型失败：子 Agent 各自误解任务、风格/接口不一致，最后由主 Agent 收拾残局。
- 更稳的默认：**单线程 Agent**；上下文快满时，用“压缩模型”把历史压成关键决策/事件。
- Claude Code 的子 Agent 只做“边界清晰的问题回答”，不并行写代码；目的主要是隔离调查上下文，而不是并行决策。
- 作者判断：当前 Agent 还不擅长长上下文主动协商；多 Agent 协作的可靠性需要跨 Agent 上下文传递能力的突破。

**对本项目**：主理人必须把**完整相关上下文/上游产物**交给专家，而不是只给 1200 字摘要；专家产出要落 artifact，主理人只拿引用。不要让多个执行 Agent 同时改同一份代码/文件。

### 2.3 MAST：多 Agent 失败的 14 种模式

来源：<https://arxiv.org/abs/2503.13657>（v3，2025-10）

- 数据：1600+（正文 1642）条标注 trace，覆盖 7 个主流 MAS 框架。
- 三大类、14 种失败模式：
  1. **System Design Issues**：角色/流程/停止条件设计问题（如 disobey role specification、step repetition、unaware of stopping conditions）。
  2. **Inter-Agent Misalignment**：对话重置、任务跑偏、不请求澄清等。
  3. **Task Verification**：没有验证或验证不完整。
- 干预案例：ChatDev 中让 CEO 拥有最终决定权，任务成功率 **+9.4%**。
- 结论：很多失败不是模型能力，而是 MAS 设计问题；需要更系统的验证与结构改造。

**对本项目**：需要
- 明确的角色边界与停止条件（每轮任务的最大步数/验收标准）。
- 任务失败后的“验证/回退/重试”路径，而不是直接汇总。
- 记录失败模式，做按类别的回归评测。

### 2.4 评测基准与可靠性

| 基准 | 测什么 | 关键结论 | 对本项目 |
|---|---|---|---|
| **GAIA** <https://arxiv.org/abs/2311.12983> | 真实世界问答，推理/多模态/浏览/工具 | 2023 年人类 92% vs GPT-4+插件 15%；说明工具使用与长链条仍是难点 | 做“调研类”黄金集 |
| **τ-bench** <https://arxiv.org/abs/2406.12045> | 工具 Agent 与模拟用户的多轮交互、领域规则 | 2024 年 SOTA <50% 成功率；pass^8 在零售场景 <25% | 做 `pass^k` 可靠性指标；加规则遵循测试 |
| **AgentBench** <https://arxiv.org/abs/2308.03688> | 8 个交互环境 | 长期推理、决策、指令遵循是主要障碍 | 做多环境回归 |
| **SWE-bench** <https://arxiv.org/abs/2310.06770> | 真实 GitHub issue 修复 | 代码任务更适合单线程/强验证 | 代码类任务加测试门禁 |

**结论**：只看“一次成功”会高估系统；要跟踪 **pass^k**、成本、时延、人工介入率、失败模式分布。

---

## 3. 生产化能力清单（AgentCore 的启发）

AWS AgentCore 把 Agent 平台拆成一组可组合能力，适合当 checklist：

| 能力 | AgentCore | 当前项目 | 建议 |
|---|---|---|---|
| Harness（规划/todo/上下文/工具/审批） | 有 | 部分有（规划、任务、审批） | 补 todo、上下文压缩、免重复审批 |
| Runtime（隔离、长时、异步） | microVM + serverless | 本机进程 + workspace | 先做 checkpoint/resume，再考虑容器 |
| Memory（短期/长期/共享） | 有 | 只有 per-run messages | 加长期记忆与作用域 |
| Gateway（API→MCP） | 有 | 已有 MCP 客户端 | 加 MCP Registry/工具版本 |
| Identity（Agent 身份/权限） | 有 | 只有网页 token | 加 Agent/工具级权限 |
| Code Interpreter / Browser | 有 | 本机 shell + fetch | 沙箱化；浏览器自动化可选 |
| Observability（OTel） | 有 | SSE 事件日志 | 加 OTel spans + trace UI |
| Policy（工具调用前拦截） | 有 | 命令审批 | 加细粒度工具策略 |
| Registry（Agent/MCP/Skill） | 有 | 集群预设/模型列表 | 加 Agent/Skill 注册表 |
| Evaluations | 有 | 只有模型评分 | 加 run/trajectory eval |
| Optimization | 有 | 无 | 基于 trace 的 prompt/tool 优化 |

---

## 4. 对照当前项目的差距

当前代码：
- `mvp/app/orchestrator.py`：主理人决策、轮次任务、guidance、调度。
- `mvp/app/store.py`：SQLite（runs/tasks/events/messages/clusters/providers）。
- `mvp/app/bus.py`：进程内 EventBus。
- `mvp/app/mcp.py`：MCP 连接。
- `mvp/static/index.html`：单文件 UI、SSE、轮次活动面板。

| 维度 | 现状 | 风险 | 优先级 |
|---|---|---|---|
| **可恢复执行** | Run 状态在内存；重启后活动运行丢失/被取消 | 长任务、审批等待、服务重启会白跑 | **P0** |
| **可观测性** | SSE 事件 + 日志；无 trace/span/成本归因 | 出问题只能翻事件，难定位 | **P0** |
| **评测** | 模型 9 维评分；无 run 级/轨迹级评测 | 改 prompt/编排无法量化 | **P0** |
| **上下文工程** | 上游结果截断 1200 字；无压缩/artifact 引用 | 专家误解、重复劳动、token 膨胀 | **P0** |
| **验证/QA** | 任务完成即汇总；无独立验收 | MAST 的 verification failure | **P1** |
| **沙箱/安全** | 本机 shell + 命令审批；workspace 限制 | 误操作、MCP 工具投毒、提示注入 | **P1** |
| **成本控制** | 每轮重置预算；无缓存/模型级联 | 多轮对话成本不可控 | **P1** |
| **长期记忆** | 仅 per-run messages | 跨会话不记得用户偏好/项目决策 | **P1/P2** |
| **协议** | MCP | 无法接外部 Agent | **P2** |
| **UI** | 单一活动 dock；无每轮 trace/artifact diff | 复杂任务可读性不足 | **P2** |

---

## 5. 建议路线图

### P0：1–2 周内做（不改框架也能做）

#### 5.1 可恢复执行（最高优先）
- 新增 `run_checkpoints` / `task_steps`：
  - 每个 round、每个 task step、每次 tool call 后写 checkpoint。
  - 保存：run 状态、当前 round、task 状态、messages、待执行工具调用、attempt、idempotency key。
- 启动时扫描 `status in (planning, running, cancelling)` 的 Run：
  - 有 checkpoint → 恢复；无 → 标记 failed 并说明。
- 工具调用要 **幂等**：`idempotency_key = run_id + task_id + step + call_id`；写文件/命令要有去重或 dry-run 记录。
- 优雅关闭：`SIGINT` 时停止接新任务，等待/取消在途任务，写最终状态。
- 单用户本地系统先用 SQLite 即可；并发/跨进程需求变大再考虑 Temporal。

#### 5.2 评测闭环
- 建 20–30 条黄金任务，覆盖：调研、分析、写作、代码、工具调用、审批、取消、恢复。
- 指标：成功率、`pass^k`、token/任务、USD/任务、P50/P95 时延、人工介入率、失败模式（MAST 三类）。
- LLM-as-judge + 人工抽检；把结果写回 `evaluations`，支持对比不同 prompt/模型/编排。
- 每次改主理人 prompt/工具描述，先跑回归集。

#### 5.3 可观测性
- 用 OpenTelemetry GenAI 语义约定（已迁到 `open-telemetry/semantic-conventions-genai`）：
  - span：run → round → task → llm.chat → tool.call；
  - 属性：model、provider、tokens、cost、tool name、status、error；
  - 导出到 Langfuse（可自托管）或本地 JSON trace。
- UI 增加“本次运行 trace”视图：树/时间线/成本；支持从事件跳到 span。
- 记录 lead 决策：action、tasks、guides、原始工具调用，便于复盘。

#### 5.4 上下文工程
- 每轮构造上下文时：
  - 已完成轮次只给“决策 + 结论 + artifact 引用”，不塞全文；
  - 子 Agent 拿到**完整相关上游 trace**（Cognition 原则），而不是 1200 字摘要；
  - 工具输出超限写 artifact，消息里只放引用；
  - 接近上下文上限时，用压缩模型生成“关键决策/未完成事项/约束”。
- 给每个任务明确：目标、输出格式、可用工具/来源、边界、验收标准。

### P1：2–6 周

#### 5.5 验证/QA 阶段
- 增加 `verifier`（或复用 reviewer）作为每轮最后一步：
  - 输入：用户目标、验收标准、专家产出、工具证据；
  - 输出：`PASS/FAIL` + 缺失项 + 建议路由（回 engineer/researcher/重新规划）。
- 失败自动回退一次；仍失败则带“已知问题”交付。
- 对应 MAST 的 Task Verification / role / stopping 问题。

#### 5.6 沙箱与权限
- 每个 task 独立工作目录/容器；命令在沙箱执行（Docker / E2B / Daytona / microVM）。
- 工具策略：allowlist、参数校验、网络/文件/命令能力分级；MCP 工具做来源与描述审查。
- 秘密与 token 不进入模型上下文；审计每次工具调用。

#### 5.7 成本与性能
- 每轮预算（tokens/USD/时延）独立；超限降级或停止。
- 模型级联：便宜模型做分类/摘要/路由，强模型做关键推理/代码。
- Prompt caching、并行工具调用、工具输出裁剪、失败快速重试。
- 成本仪表盘：按 run/round/task/model/tool 归因。

#### 5.8 记忆
- 作用域：user / project / run / agent；写入策略（什么值得记、何时过期）。
- 可参考 Mem0 的“抽取事实 + 召回”，Letta 的“stateful agent + MemFS + shared memory”。
- 与现有 self-memory 打通：把长期偏好/项目决策作为受控上下文注入，不要把全部历史塞进 prompt。

### P2：6 周以上

- **A2A 适配**：把外部 Agent 作为远程专家；MCP 继续做工具层。
- **Agent/Skill Registry**：版本化 prompt、工具描述、技能包；灰度发布。
- **优化闭环**：基于 trace 的 prompt/tool description 自动优化 + A/B（AgentCore Optimization 思路）。
- **多拓扑实验**：supervisor / blackboard / debate / single-thread+compress，用同一评测集比较。
- **UI**：每轮独立 trace/artifact/diff；人工审批队列；运行中 steer。

---

## 6. 不要做什么

- **不要**在 durable execution / eval / tracing 之前继续增加角色和“专家”。
- **不要**让多个执行 Agent 并行修改同一份代码/文件；冲突决策的代价高于并行收益。
- **不要**把“多 Agent”当成默认架构；简单任务用单 Agent，复杂/广度任务才用专家团。
- **不要**只给子 Agent 一段摘要就让它执行；至少给完整相关上下文和明确验收标准。
- **不要**只测“能不能跑通”；要测 `pass^k`、成本、时延、失败模式。
- **不要**急着换框架；当前 FastAPI + SQLite 对单用户足够，先补运行时能力。需要分布式/跨进程时再考虑 Temporal / AgentCore / LangGraph。

---

## 7. 推荐跟踪指标

- 任务成功率、`pass^k`（k=3/5/8）
- 每任务 token / USD / 时延（P50/P95）
- 工具调用成功率、工具错误率、重试次数
- 轮次数/任务数（是否过度拆分）
- 人工介入率（审批、纠偏、重跑）
- 失败模式分布（MAST 三类 14 种）
- 上下文压缩率、artifact 引用率
- guidance 生效率和提前/滞后情况

---

## 8. 主要来源

- Anthropic, *How we built our multi-agent research system*：<https://www.anthropic.com/engineering/multi-agent-research-system>
- Cognition, *Don't Build Multi-Agents*：<https://cognition.ai/blog/dont-build-multi-agents>
- Cemri et al., *Why Do Multi-Agent LLM Systems Fail?*（MAST）：<https://arxiv.org/abs/2503.13657>
- GAIA：<https://arxiv.org/abs/2311.12983>
- τ-bench：<https://arxiv.org/abs/2406.12045>
- AgentBench：<https://arxiv.org/abs/2308.03688>
- SWE-bench：<https://arxiv.org/abs/2310.06770>
- MetaGPT：<https://arxiv.org/abs/2308.00352>
- AutoGen：<https://arxiv.org/abs/2308.08155>
- LangGraph overview：<https://docs.langchain.com/oss/python/langgraph/overview>
- Microsoft Agent Framework overview：<https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview>
- OpenAI Agents SDK：<https://openai.github.io/openai-agents-python/>
- Google ADK：<https://google.github.io/adk-docs/>
- CrewAI：<https://docs.crewai.com/>
- AWS Bedrock AgentCore：<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html>
- Temporal for AI：<https://temporal.io/solutions/ai>
- MCP：<https://modelcontextprotocol.io/docs/getting-started/intro>
- A2A：<https://a2a-protocol.org/latest/>
- A2A joins Agentic AI Foundation：<https://a2a-protocol.org/latest/blog/2026/08/27/a-new-chapter-for-a2a-joining-the-agentic-ai-foundation/>
- OpenTelemetry GenAI semconv：<https://github.com/open-telemetry/semantic-conventions-genai>
- Langfuse：<https://langfuse.com/docs>
- Mem0：<https://docs.mem0.ai/overview>
- Letta：<https://docs.letta.com/>
- 本机 WorkBuddy `software-company` 专家团插件（lead + PM/architect/engineer/QA，SOP 路由，TeamCreate/SendMessage）

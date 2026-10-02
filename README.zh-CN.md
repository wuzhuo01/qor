# QOR — AI-Native Organization Runtime

QOR 是一个通用的 AI 原生组织运行时，通过任务、计划、基于能力的分配、执行、评审、综合、决策、人类责任、动作、核验与结果记录来协调 AI 原生组织工作。

QOR 提供组织运行时能力。具体业务能力由调用方提供。

## 示例

AI Agent 可以完成任务。

谁决定？
谁审核？
依据是什么？
没有能力怎么办？

这个示例使用 StubPlanner 和脚本 Agent，不调用 LLM。它不是一个自己运转的 AI 组织。运行时记下生产者、审核者、证据、决策和理由。

```bash
python -m examples.organization_runtime
```

FULL：

```text
Goal
 ↓
Plan
 ↓
Assignment
 ↓
Artifact
 ↓
Review
 ↓
Decision
 ↓
Outcome
```

GAP：

```text
Capability Gap
 ↓
BLOCKED
```

## 为什么需要 QOR

使用 AI 的组织仍然要让工作可追责。需要能看见计划是什么、执行者是按能力而不是按名字分来的、产出了什么、独立评审是否接受、决定了什么；如果动作改变了外部状态，还要看见核验是否一致，以及负责的人是否记下了结果。

QOR 是这层运行时。它不是一家自主运转的公司，不是 AI 员工平台，也不是某个行业的 Agent 平台。

## 核心模型

```text
Task
→ Plan
→ Assignment
→ Execution
→ Artifact
→ Review
→ Synthesis
→ Decision
→ COMPLETED
```

规划器选择能力以及计划的结构。它不选择具体 Agent。分配把每项所需能力交给持有该能力的候选人。

决定不再采取动作时，任务可以完成。评审返工不在已证明的路径里。

## 需要动作时

```text
Decision
→ Action
→ Permission / Risk / Approval
→ WorldWriter
→ Independent WorldReader
→ Verification
→ SETTLED
→ Human CIO Outcome
```

`EXECUTED` 不是 `SETTLED`。结算要求独立重读，并且读回与期望一致。只有 Human CIO 在这次已核验的结算之后记录权威结果。系统不会自己写下这个结果。核验失败不会被记成结果。

## QOR 提供什么

- 任务接入，以及以任务为范围的执行生命周期
- 对照已配置能力目录做校验的计划
- 按能力分配
- 独立评审、综合与决策
- 等待来源上的人类责任，以及一条收窄的升级路径
- 一条活跃的人类责任需要处理时的显式通知
- 受权限、风险和批准约束的沙箱动作
- 结算前的独立世界核验
- 能把任务打开的调度器和事件路由
- 作为后续计划参考的教训检索

## QOR 不提供什么

- 业务能力目录
- 调度触发或事件之后的自动执行
- 由教训改变后续行为的学习
- 自动写入的权威结果
- 执行身份、重试、崩溃恢复、执行幂等、并发控制、worker、queue 或 dispatcher
- `BLOCKED` 之后的完整生命周期

## 当前 V1.0 证据

V1.0 核心已经冻结。契约测试覆盖：调用方提供工作能力时的组织闭环，经结算直到人记录结果的受治理动作路径，调度器和事件路由创建任务，通知，入口守卫，责任闭合，升级契约，以及状态机和权限不变量。

仍然成立的限制：

- 与具体业务无关、由真实语言模型完成的组织规划，尚未充分证明。
- generic profile 不提供业务能力目录。
- 公开示例使用 StubPlanner 和脚本 Agent，不调用模型。

契约测试通过，并不取消这些限制。

## 快速开始

QOR 需要 Python 3.11 或更高版本。

```bash
python -m pip install -e ".[dev]"
```

默认命令加载 generic profile，并在规划之前停止：

```bash
qor --objective "准备本周期的组织状态。"
```

该命令以退出码 2 结束。generic profile 有意不包含可规划的业务能力。同一入口也可以写成 `python -m qor.cli`。

参见 [入门](docs/zh-CN/getting-started.md)。

## 测试

在仓库根目录运行公开契约测试：

```bash
python -m pytest \
  tests/test_cli_generic_entry.py \
  tests/test_core_organization_loop_acceptance.py \
  tests/test_core_organization_loop_action_outcome_acceptance.py \
  tests/test_qor_dogfooding_acceptance.py \
  tests/test_scheduler_acceptance.py \
  tests/test_event_router_acceptance.py \
  tests/test_notification_acceptance.py \
  tests/test_notification_auto_trigger_acceptance.py \
  tests/test_entry_guard_acceptance.py \
  tests/test_human_responsibility_acceptance.py \
  tests/test_task_escalated_contract_acceptance.py \
  tests/test_state_machines.py \
  tests/test_invariants.py
```

这组测试最近一次记录是 190 passed。不要把全仓库测试运行当作公开契约。树里的其他测试是本地开发材料。

## 文档

- [入门](docs/zh-CN/getting-started.md)
- [概念](docs/zh-CN/concepts.md)
- [架构](docs/zh-CN/architecture.md)
- [证据边界](docs/zh-CN/evidence-boundary.md)
- [English README](README.md)

## 当前边界

公开示例不发明业务目录。运行 `python -m examples.organization_runtime`。它使用 StubPlanner 和脚本 Agent，不调用模型。

调度器和事件路由把任务创建为 `OPEN`。它们不调用任务执行。

教训可以被检索。它们不改变下一次计划，也不是绑定约束。

`ESCALATED` 只表示这条路径：

```text
AWAITING_HUMAN → ESCALATED → AWAITING_HUMAN
```

`BLOCKED` 是异常停止。它之后的生命周期不在 V1.0 契约内。

## 许可证

许可证：Apache-2.0。见 [LICENSE](LICENSE)。

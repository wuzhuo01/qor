# 入门

QOR 是协调 AI 原生组织工作的通用运行时。具体业务能力由调用方提供。

## 安装

QOR 需要 Python 3.11 或更高版本。在仓库根目录执行：

```bash
python -m pip install -e ".[dev]"
```

运行时依赖 Pydantic 和 PyYAML。`dev` 额外包含 pytest、Ruff 和 mypy。安装之后可以使用 `qor` 命令。`python -m qor.cli` 是同一个入口。

## 第一次运行

默认 profile 是 `generic`。它有意不包含业务能力，因此规划器没有可以变成工作的能力。

```bash
qor --objective "准备本周期的组织状态。"
```

进程会打印这个 profile 没有可规划工作能力，并以退出码 2 结束。它不会调用规划器，也不会为了让命令成功而编造业务目录。

`--objective` 是必填项。默认规划器是 `llm`。只有 profile 声明了可规划的工作能力之后，才会走到这个规划器。若此时没有配置提供者，它会失败，并且不会退回脚本化计划。提供者设置从环境变量名 `QOR_LLM_BASE_URL`、`QOR_LLM_API_KEY` 和 `QOR_LLM_MODEL` 读取。不要提交这些值。

`qor` 命令不接受额外的业务目录。契约测试里的完整闭环，是把调用方提供的工作配置传给应用构建器之后启动的。那份配置不是 generic 产品目录，QOR 也不附带这样一份目录。

## 运行测试

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

这些测试使用真实的任务、计划、分配、评审、决策服务；场景需要动作时，也使用真实的动作服务。场景需要工作能力时，由测试自己提供配置。那不是 generic 产品目录。

不要把 `tests/` 下的每个文件都当成这组公开测试。

## 成功是什么意思

对这个入口，成功是指：

- 运行时能够启动
- generic 配置能够加载
- 因为没有可规划的业务工作，命令以退出码 2 停止
- 调用方提供了工作配置时，公开契约测试能够通过

成功不是 generic profile 上完成了一项业务任务。这个 profile 故意只是入口外壳。

# AgentHub Core

[English](README.md) | [简体中文](README.zh-CN.md)

AgentHub 是一个轻量、决策先行的多 Agent 协作控制面。决策者发布紧凑且不可变的执行合同；
执行者在隔离的 Git worktree 中工作，提交代码、证据或明确的阻塞项。

本仓库只包含可复用的机制、模板和测试。项目注册、业务代码、数据集、模型、凭据和工作流
数据库不进入公开历史。运行时只使用 Python 标准库。

完整的命令、合同、配置和状态语义见 [docs/SPEC.md](docs/SPEC.md)；本文件只是快速入口。

## 环境要求

- Python 3.10 或更高版本，以及支持 worktree 的 Git。
- Windows 或 POSIX 主机；CI 覆盖 Windows/Linux 与 Python 3.10/3.13。
- 远端客户端需要 OpenSSH；部署需要 PowerShell 或 sh + sha256sum。

## 架构

```text
AgentHub core：生命周期、角色权限、合同校验、SQLite 状态
    |
私有项目注册：Agent、设备、路径、SSH 与部署映射
    |
权威主机：规范仓库 + 本地 SQLite 状态 + 任务 worktree
```

角色可叠加：`administrator`、`decider`、`executor`。一个 Agent 可同时具备多个角色并
使用统一的 `context`。默认允许自审；设置 `agents.<id>.self_review: "forbid"` 可要求
独立复核。

## 快速开始

```sh
python agent_hub.py --project <project_id> --device <device_id> context <decider_agent_id>
python agent_hub.py --project <project_id> --device <device_id> next <executor_agent_id>
python agent_hub.py --project <project_id> --device <device_id> --session <window_id> \
  next <executor_agent_id> --available
```

全局参数必须写在子命令之前。本地权威主机可使用 `--local --config <config_file>`。
始终声明执行设备。`--spec`、`--progress`、`--result` 接受内联 JSON 或 `@文件路径`。

## 命令

| 命令 | 用途 |
| --- | --- |
| `status`、`context`、`next`、`show`、`audit` | 查看授权范围内的状态 |
| `decision`、`post` | 记录决策并发布合同 |
| `claim`、`release` | 领取任务或交回队列 |
| `checkpoint`、`heartbeat`、`handoff` | 保存进度、刷新存活、切换设备 |
| `submit`、`review`、`close` | 提交证据、复核并集成 |
| `cancel`、`supersede`、`prune` | 保留取消/替代历史并回收 worktree |

`claim <agent> [task_id]` 领取指定任务；省略 task_id 时领取下一个可用任务。2.4.5 已
删除 `init` 和 `claim-next`；每个命令都会打开或迁移状态库，不需要单独初始化。

所有命令输出统一 JSON envelope（`{"ok": true, "data": ...}`）；领域错误在 stderr 输出
`{"ok": false, "error": {"code": ...}}`。`--text` 输出人类可读摘要。详见
[命令契约](docs/SPEC.md#4-command-line-contract)。

## 合同与流程

最小合同必填：`id`、`title`、`objective`、`assignee`、`decision_key`、`base_commit`、
`instructions`、`acceptance`、`allowed_paths`。可选：`forbidden_paths`、`depends_on`、
数值 `gates`、`artifacts`、`allow_path_overlap`、`priority`、`chain_id`。

```sh
# 决策与合同
python agent_hub.py ... decision <admin> --key D-1 --summary "..." --rationale "..."
python agent_hub.py ... post <admin> --spec @task.json

# 执行
python agent_hub.py ... --session win-a claim <executor> TASK-001
python agent_hub.py ... --session win-a checkpoint <executor> TASK-001 --progress @progress.json
python agent_hub.py ... --session win-a submit <executor> TASK-001 --result @result.json

# 复核与集成
python agent_hub.py ... review <decider> TASK-001 --action approve --note "..."
# 通过项目正常的已复核 Git 流程合并，然后：
python agent_hub.py ... close <admin> TASK-001 --merge-commit <sha> --target-branch main --note "..."
```

`submit` 由核心计算门禁；必需门禁失败必须改用 `blocked` 提交。`close` 校验目标分支
已包含批准提交，然后删除 worktree。

## 并发、窗口与状态

每个任务拥有独立 worktree、分支和活动设备。执行者默认只推进一个任务；设置
`execution.max_active_tasks_per_executor`（1-32）可提高上限，也可用
`agents.<id>.max_active_tasks` 单独下调。只有 `in_progress` 任务占用槽位。

认领会记录调用方声明的 session。`checkpoint`、`submit`、`handoff`、`release` 会拒绝
同一身份的其他窗口。`execution.session_policy: required` 下，认领必须带 session；旧
的未绑定认领需先用 `--session <id> heartbeat <executor> <task_id>` 绑定一次。租约只做提示，过期不会自动释放。

权威主机拥有 SQLite 状态和 worktree；不要放在同步目录中，并单独备份状态。首次打开
schema 4 状态时会先写 `state.db.v4-<UTC 时间戳>.bak`，再原地迁移到 schema 5。
2.4.3 不能直接读 schema 5；回滚需恢复该备份。

## 验证与部署

```sh
python scripts/audit_agenthub.py
python -m unittest discover -s tests -v
python scripts/check_release.py
```

部署逐个替换受管文件，但在第一次替换前校验全部哈希；保留被替换的上一代，部署后自检
失败会自动恢复；支持 `--dry-run`、`--verify`、`--rollback`。部署不会修改 SQLite 状态。
smoke 脚本会克隆配置仓库到临时目录，并使用一次性状态。

## 从 2.4.3 升级

1. 停止权威主机命令，用 SQLite 感知方式备份状态。
2. 安装 2.4.5 运行时。
3. 首次命令会把 schema 4 迁移到 5，并生成 v4 备份；在新状态验证完成前保留该备份，
   它是唯一的降级路径。
4. 从私有注册中删除旧可选键：`agenthub_version`、`project_name`、`workflow`、
   `messaging_transports`、`lark_workflow`、`execution.concurrency`、
   `execution.isolation` 以及命名目录子键。2.4.5 会忽略它们，但不应继续保留。
5. 更新仍提到 `init` 或 `claim-next` 的角色说明。
6. 恢复工作前运行审计、测试和隔离 smoke。

## 贡献与许可

阅读 [CONTRIBUTING.md](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md) 和
[SECURITY.md](SECURITY.md)。发布与 tag 流程见 [RELEASING.md](RELEASING.md)。
本项目使用 [MIT](LICENSE) 许可。

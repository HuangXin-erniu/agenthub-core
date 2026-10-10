# AgentHub Core 2.3.2

[English](README.md) | [简体中文](README.zh-CN.md)

AgentHub 是一个轻量、以决策为起点的多 Agent 协作控制平面，支持多个独立项目。决策者发布精简、不可变的任务合同；执行者在隔离的 Git worktree 中工作，并返回提交、验证证据或明确的阻塞原因。

本仓库仅包含通用机制、实现、模板和测试。真实项目注册、业务源码、数据集、模型、凭据和工作流数据库均不进入公开 Git 历史。代码只使用 Python 标准库，无需安装额外的 Python 包。

## 环境要求

- Python 3.10 或更高版本，以及支持 worktree 的 Git。
- Windows 或 POSIX 主机；CI 覆盖 Windows/Linux 与 Python 3.10、3.13。
- 远程客户端需要 OpenSSH，并单独配置可信 SSH 主机。
- Windows 部署需要 PowerShell；POSIX 部署需要 sh 和 sha256sum。

下文使用 `python`；也可以使用平台对应的 `python3` 命令。

## 架构

```text
AgentHub 核心：生命周期、角色权限、合同校验、SQLite schema
    |
私有项目注册：Agent、设备、路径、SSH 与部署映射
    |
权威主机（Authority）：规范 Git 仓库 + 本地 SQLite 状态 + 任务 worktree
```

| 角色 | 职责 |
| --- | --- |
| administrator | 治理、初始化、集成关闭、取消和替代任务 |
| decider | 记录决策、发布任务合同、检查任务和复核证据 |
| executor | 认领已分配任务、保存 checkpoint、交接设备和提交结果 |

```text
decision -> post -> claim -> checkpoint / handoff -> submit -> review
                                                           | rework -> claim
                                                           | approve -> integration -> close
```

批准任务不会自动合并代码。应先按照正常的 Git 复核流程完成集成，再由 `close` 验证合并并移除任务 worktree。取消和替代操作保留历史。SQLite 状态由单一权威主机管理；每个任务只有一个活动设备，每名执行者同一时间只推进一个任务。

## 配置项目

1. 将 `templates/project.example.json` 复制到私有的 `projects/<project_id>/project.json`。项目注册目录已被 Git 忽略。
2. 替换所有尖括号占位符，包括 JSON 对象的键。声明稳定的项目、Agent 和设备 ID，指定规范 Git 仓库、权威主机本地状态文件及独立 worktree 目录。
3. 本地使用时将 `remote.enabled` 设为 `false`。远程使用时选择 `windows_cmd` 或 `posix_sh`、对应部署适配器、可信 SSH 别名和严格的主机密钥检查。凭据应存放在 SSH 配置或凭据管理器中。
4. 添加私有 `README.md`、`AGENTS.md` 及 `instructions` 声明的角色文件。根据操作系统启动器模板填写运行时、解释器、配置和状态路径。
5. 部署时在 `deployment.managed_files` 中列出全部所需文件，包括角色说明。先审计、预览部署，再按自己的变更流程执行部署。

参见[项目注册规范](docs/PROJECT_REGISTRATION.md)和[安全边界](SECURITY.md)。模板用于指导替换，不是可直接运行的实例。本仓库不附带或自动初始化任何项目。

## 核心命令

```sh
python agent_hub.py --help
python agent_hub.py --project <project_id> --device <device_id> context <decider_agent_id>
python agent_hub.py --project <project_id> --device <device_id> next <executor_agent_id>
```

在本地权威主机使用显式私有配置时，采用 `--local --config <config_file>`。始终声明执行设备。2.3.2 保留 2.3.1 的 CLI 参数与配置格式。

| 命令 | 用途 |
| --- | --- |
| init, status, context, next, show, audit | 初始化或查看已授权的工作流状态 |
| decision, post, claim | 记录决策、发布合同并认领分配的任务 |
| checkpoint, handoff, submit | 保存进度、交接活动设备并提交证据 |
| review, close, cancel, supersede | 复核、验证集成，或保留取消和替代记录 |

各子命令均支持 `--help`。`--spec`、`--progress` 和 `--result` 接受内联 JSON 或 `@<file>`。任务合同包含执行者、决策键、基础提交、允许和禁止路径、验收标准、验证要求、交付物及证据要求。启用 `task_naming` 时，还必须提供符合配置的链 ID 和准确的下一序号。任务包最多为 16,384 个 UTF-8 字节。

## 工作流完整性与路由

- **合同校验**：`post` 与 `supersede` 共用必填字段、数组、优先级、预算、命名、决策和提交校验。序号在项目内全局递增，包含已取消任务，不能复用。新任务可以沿用被替代任务的链 ID，但不能与其他活动任务冲突。建立新任务与取消旧任务在一个事务内完成。
- **远程路由**：启用远程访问后，非 `state_authority.host` 的已注册设备通过 SSH 代理，适用于 Windows 和 POSIX 客户端。权威主机、远程访问关闭及显式 `--local` 均在本地执行。SSH 失败不会回退到本地数据库。
- **设备交接**：当前执行者必须在活动设备上保存一个晚于最近一次认领或交接的 checkpoint。其他管理员可以通过现有交接命令恢复失联设备，但必须填写非空说明；恢复审计会记录是否缺少有效检查点。顺序依据事件 ID 判断，同一秒内的事件也能正确排序。
- **路径边界**：提交时检查全部变更路径，重命名同时检查源路径和目标路径。禁止规则优先。以 NUL 分隔的 Git 输出保留 Unicode、空格及平台支持的特殊文件名；无法可靠解码为 UTF-8 的路径会被拒绝。保留现有 `fnmatch` 语义：`src/*` 也匹配 `src/nested/file.py`，大小写规范化遵循主机操作系统。

这些检查约束可信工作流及提交差异，不提供运行时沙箱，也不限制执行进程读取文件系统。

## 验证与部署

```sh
python scripts/audit_agenthub.py
python -m unittest discover -s tests -v
python scripts/check_release.py
```

结构审计支持没有项目注册的核心仓库；存在私有注册时，还会检查配置、说明文件、部署源文件和项目隔离。发布检查器核对精确的公开文件白名单并扫描内容，不输出匹配到的敏感值。开发目录包含私有注册时，可使用 `--staged` 或 `--history` 仅检查计划提交的 Git 内容。

```sh
python scripts/deploy_project.py --project <project_id> --dry-run
python scripts/deploy_project.py --project <project_id>
python scripts/smoke_project.py --config <config_file> --runtime agent_hub.py
```

部署会校验文件哈希并逐个替换文件，但不提供跨文件事务。应单独备份权威状态，仅在明确需要重建状态时使用 `--reset-state`。烟雾测试将配置中的仓库克隆到临时目录，使用独立状态和 worktree，不调用私有启动器或部署文件；它用合成任务 ID 验证通用生命周期，命名规则由单元测试覆盖。单元测试创建临时仓库，无需 SSH 或私有项目注册。

消息传输通道保持关闭；旧 Feishu/Lark 实现未包含在仓库中。Agent 身份和角色校验用于组织可信工作流，不为不可信网络用户提供身份认证。

## 从 2.3.1 升级

1. 暂停权威主机命令，使用 SQLite 备份机制，或在权威服务停止后备份状态库。按自己的变更流程保留旧运行时，以便回滚。
2. 阅读[变更记录](CHANGELOG.md)，安装 2.3.2 运行时，并将私有配置中的 `agenthub_version` 更新为 `2.3.2`，使结构审计与运行时版本一致。
3. 配置 schema 仍为 **1**，数据库 schema 仍为 **4**。历史任务与检查点保留，不新增数据库列或执行破坏性迁移。
4. 普通交接前保存本轮检查点。以前被接受的不完整替代合同、重复序号和涉及禁止路径的重命名，现在会被拒绝；不会改写历史合同。
5. 运行结构审计、测试和隔离烟雾测试；恢复任务前，按部署流程验证真实启动器。

公开版本不会自动部署或修改任何现有私有项目。

## 贡献与许可证

参阅[贡献说明](CONTRIBUTING.md)、[Agent 指引](AGENTS.md)和[安全说明](SECURITY.md)。采用 [MIT 许可证](LICENSE)。

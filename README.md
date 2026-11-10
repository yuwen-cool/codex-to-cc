# Codex to CC

**在 Codex 里，把审查、编码和测试任务交给本机 Claude Code。**

A lightweight Codex skill for delegating review and coding tasks to your local, unmodified Claude Code CLI—with scoped permissions, resumable jobs, and explicit parent verification.

[![Tests](https://github.com/yuwen-cool/codex-to-cc/actions/workflows/tests.yml/badge.svg)](https://github.com/yuwen-cool/codex-to-cc/actions/workflows/tests.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Codex to CC 是独立开源项目，不隶属 OpenAI 或 Anthropic，也不提供模型服务。当前版本为 **v0.1.1（公开预览）**。

## 用一句话开始

安装后，对 Codex 说：

| 你说 | Codex to CC 帮你做什么 |
|---|---|
| “让 Claude 审查当前分支相对 main 的改动。” | 取共同祖先到 HEAD 的差异，进行只读审查 |
| “请 Opus 看看这个方案有哪些遗漏。” | 在独立会话中给出第二意见 |
| “交给 Claude 修复这个问题并运行测试，保留我已有的改动。” | 在项目内执行授权的修改与受限测试，交回实际结果 |
| “刚才那个 Claude 任务进展如何？” | 查询真实工具活动、调用状态和验收状态 |
| “继续刚才 Claude 的任务。” | 续接经过验证的原生会话 |
| “停止刚才的 Claude 任务。” | 请求取消并确认进程组停止；已有改动保留 |

## 它解决什么

- **减少手动交接**：任务携带目标、项目规则、改动范围与验收条件，主 Agent 获取报告并继续处理。
- **明确审查对象**：支持未提交改动、暂存区、分支、普通／根提交和指定路径。空范围会在调用前报错。
- **看清任务状态**：后台启动、查询、历史、取消和续接；区分受阻、失败、待验收和结果过期。
- **不把“调用成功”当作“工作通过”**：父 Agent 检查实际 diff 与测试结果，再记录验收。Git 代码变化会提示旧结果过期。
- **尽量少加一层**：Python 标准库辅助脚本；无常驻服务、额外账户、MCP 服务或凭据代理。

它不承诺双模型一定更强，也不声称替代完整的多代理工作台。

## 安装

前置条件：

1. Codex、Git、Python **3.11+**。
2. 已安装未经修改的官方 **Claude Code ≥ 2.1.311**，并能在本机正常使用。
3. 目前真实模型调用在 **macOS + Claude Code 2.1.311** 上验证。Linux 的模拟 CLI 测试与真实沙箱兼容性不是一回事；原生 Windows 不支持。

最方便的方式，把这句话交给 Codex：

> 请从 https://github.com/yuwen-cool/codex-to-cc 安装 v0.1.1 的 skills/codex-to-cc。先读 README 和 SKILL.md，使用已有的 Skill 安装器，不修改 Claude 的全局配置。

如果你的 Codex 带有官方 Skill 安装器，也可以执行：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo yuwen-cool/codex-to-cc --path skills/codex-to-cc --ref v0.1.1
```

已存在同名 Skill 时安装器会停止，避免覆盖。手动安装可下载 [Release](https://github.com/yuwen-cool/codex-to-cc/releases/tag/v0.1.1) 的 Skill ZIP，将其中的 `codex-to-cc` 文件夹放到 `${CODEX_HOME:-~/.codex}/skills/`；升级前先备份旧目录。Skill 在后续新一轮对话中可发现。

本机已有 Claude Code 登录态时直接复用。尚未登录的用户需自行完成官方 CLI 登录；也支持用户自己配置的官方 `ANTHROPIC_API_KEY`。工具开源不等于模型用量免费，用量遵守你自己的账户协议和额度。

安装后检查：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-to-cc/scripts/jobs.py" doctor
```

`doctor` 不启动登录、不直接读取或导出凭据文件、不输出邮箱；它检查配置，不能替代一次真实任务。

从旧版 `ywrelay` 升级：先备份旧 Skill（包括自定义 `config.json`），安装 `codex-to-cc` 后停用旧目录，避免重复触发。历史任务仍在同一 `claude-jobs` 目录，无需迁移。`v0.1.0` 保留为历史版本。

## 权限与默认模型

| 模式 | 能力 |
|---|---|
| `review`（默认） | Read / Glob / Grep；不能改文件或运行命令 |
| `execute` | 项目内读取与编辑、强制 OS 沙箱内 Bash，用于已授权的编码和测试 |

默认固定为 **`claude-opus-5-5` / `high`**，每次最多 600 秒、40 回合。可通过 Skill 内的 `config.json` 或任务参数调整；不悄悄切换备用模型。报告核对实际模型，后端实际 effort 无法独立观测。

两个模式均隔离自定义扩展、hooks、MCP 等。执行模式默认禁止命令联网、监听端口和访问常见凭据；因此依赖下载、浏览器端到端测试等可能需要主 Agent 在现有授权内另行准备。沙箱不可用时停止，不自动降低限制。

## 直接使用命令

日常由 Codex 按 Skill 操作即可。需要手动调用时：

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/codex-to-cc"

# 将任务说明保存在项目外的文件；命令返回 job_id。
python3 "$skill_dir/scripts/jobs.py" run \
  --cwd /absolute/path/to/project --mode review --branch-base main \
  --task-file /absolute/path/to/task.txt --owner terminal --background

# 将 <job_id> 替换为返回的编号；owner 与启动时一致。
python3 "$skill_dir/scripts/jobs.py" status \
  --cwd /absolute/path/to/project --owner terminal --job <job_id> --wait 30
python3 "$skill_dir/scripts/jobs.py" result \
  --cwd /absolute/path/to/project --owner terminal --job <job_id>
```

“改代码并测试”使用 `--mode execute`。从只读切换为执行需要新建任务，不能提升原会话的权限。完整使用说明见 [SKILL.md](skills/codex-to-cc/SKILL.md)，恢复与隔离见 [任务管理](skills/codex-to-cc/references/management.md)。

## 已验证与限制

- 首次公开发布前运行本仓库的单元与子进程测试；测试使用合成 CLI，不调用真实模型、不需要 API key。准确结果见 [验证记录](docs/validation.json) 与上方 CI。
- 2026-11-10 发布代码再次真实调用 Opus 5.5 完成修复，主 Agent 独立重跑为 3/3 通过，已有未提交改动和测试文件保持不变。此前内部版本另有真实分支审查、修改测试与原生会话续接记录。
- Git 指纹是有上限的变更检测，不是文件锁或原子快照；忽略文件、秘密路径内容和符号链接目标不在内容覆盖内。父 Agent 可以检查新变化后显式验收当前状态，不能静默沿用旧报告。
- 并发保护仅覆盖本管理器、同一任务存储内的调用。它不能阻止用户终端或其他工具写入；并行写任务应按需使用独立 worktree。
- 没有自动完成推送、聊天唤醒、运行中注入指令、自动合并或完整聊天迁移。主 Agent 用有界等待跟进；取消／失败不会自动回滚修改。
- 秘密过滤针对已知路径和环境变量名，不是通用秘密扫描器。报告与官方 CLI 原生会话仍可能保留代码，发 issue 时不要上传真实凭据、私有任务记录或原始会话。

## 本地数据与认证

辅助脚本本身没有服务端或遥测。任务记录默认保存在 `${CODEX_HOME:-~/.codex}/claude-jobs`，目录 0700、文件 0600。目录名和 JSON schema 保留内部版本的名称以兼容历史任务；这不是另一项服务。

代码与提示会通过官方 CLI 发送给 Anthropic，遵守用户所选账户的条款和数据政策。Codex to CC 不提取／转发订阅 token，不管理用户的账户，不代理共享额度，不修改 CLI 二进制。项目名称说明调用方向，不使用供应商 logo、不暗示官方背书；兼容性与使用边界见 [官方依据](skills/codex-to-cc/references/compliance.md)。

## 开发与反馈

```bash
python3 -m unittest discover -s skills/codex-to-cc/tests -v
```

提交问题时提供操作系统、Python 与 Claude Code 版本、脱敏后的命令和失败状态。修改保持小范围，并给具体修复补充测试。请勿上传任务存储目录、凭据或私人代码。

## License

[MIT](LICENSE)。Claude Code、Codex 及其模型服务仍受各自提供方的条款约束。

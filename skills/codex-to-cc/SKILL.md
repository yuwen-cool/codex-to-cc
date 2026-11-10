---
name: codex-to-cc
description: 通过本机官方 Claude Code 获取独立审查、方案评估，或执行已授权的编码与测试任务，支持续接、后台进度、历史与取消。用于“让 Claude review”“请 Opus 看看”“交给 Claude 修复并测试”“查看/停止 Claude 任务”或已授权的跨模型协作；不用于未指定 Claude 的普通编码/审查、Claude 产品调研或账号管理。
---

# Codex to CC

在 Codex 中通过本机未经修改的官方 Claude Code CLI 委派任务。独立开源项目，不隶属 OpenAI 或 Anthropic。主 Agent 负责划定任务、核实发现与验收修改；`claude mcp serve` 的工具暴露不等于模型会话。调用与权限机制已实测，尚无对照证据证明它稳定提高审查或编码质量。

## 选择权限

| 模式 | 使用场景 | 能力 |
|---|---|---|
| `review`（默认） | 审查、讨论、第二意见 | Read / Glob / Grep；不能改文件或运行测试 |
| `execute` | 已授权交给 Claude 实现、修复或运行测试 | 项目内读取、Edit / Write，以及强制沙箱内 Bash |

“让 Claude 看看”保持只读；“让 Claude 修复并测试”可直接执行。若已有明确的 Claude 执行授权，不重复询问。普通编码或 review 请求不自动外发给 Claude。先告知将调用 Claude，再执行。

执行的硬边界是项目目录与受限命令沙箱；“只改某几个业务文件”等细粒度范围由交接指令与最终 diff 验收约束，不能误称为 OS 强制隔离。不要让两个 Agent 同时修改同一工作树；按现有任务需要安排独占写入或独立 worktree。

## 常用流程

将 `<skill-dir>` 替换为本 SKILL.md 所在目录。默认 `claude-opus-5-5` / `high`、600 秒、40 回合；配置只在本技能 `config.json`，不改全局 Claude 设置。两个模式均需要官方 CLI >=2.1.311，执行还需要可用的官方沙箱。本机验证为 macOS；管理器使用 POSIX 文件锁，不支持原生 Windows，Linux 未做真实调用验证。

1. 明确任务范围，读取仓库规则，检查已有改动。在工作区 `work/` 写交接文件：原始目标、允许及禁止修改的范围、验收命令和通过条件、约束、必要基线。可按需使用 [交接模板](assets/task-template.txt)，不传整段聊天、终端记录或作者的审查结论。提交时自动冻结 Git 工作树内从根到 cwd 的 AGENTS.md/CLAUDE.md；非 Git 目录只附 cwd 规则，其他适用规则由主 Agent 补充。Claude 编辑前还应读取相关子目录规则。review 没有 Bash，代码审查应在仓库根选下表的差异范围；纯讨论无需附 diff。敏感路径与符号链接会过滤，但不能检测所有内容秘密；主 Agent 仍需检查分享范围。
2. 日常优先使用管理入口，自动分配任务编号和报告目录。多步骤任务用后台方式启动；无需为已有授权再次确认。

```bash
python3 <skill-dir>/scripts/jobs.py run \
  --cwd /absolute/path/to/project --mode review \
  --title '本次改动审查' \
  --task-file /absolute/path/to/work/claude-task.txt --background
```

修复/实现/运行测试用 `--mode execute`。省略 mode 的新任务默认 review。短任务可省略 `--background` 等待结果；简短非敏感任务可用 `--task '任务描述'`，较长任务用文件，避免 shell 引号和命令行泄露问题。

3. 用 `status --job <编号> --cwd <项目> --wait 30` 获取有界进度，完成后用 `result` 读取报告。`attention` / `next_action` 区分运行、待验收、受阻、失败与结果过期；权限拒绝会结束受影响的调用，不是在后台等用户点击。工具报错可能已恢复，须核对报告。**后台启动只表示已排队，不代表完成**。除非用户明确要求只启动后台任务，主 Agent 应持续跟进至结果或具体阻碍，并在当前聊天交付；没有常驻通知或自动唤醒，不能承诺结束当前聊天后自动推送。进度是最近工具活动，不编造完成百分比。
4. `completed` 只表示调用成功、实际模型匹配且无工具权限拒绝，**不表示任务正确或测试通过**。核实发现、实际 diff 与必要测试后，用 `assess --verdict passed|failed|blocked --note '命令、退出码、结果与范围检查'` 留下验收结论。Git 项目记录提交及结束时的代码指纹；status/result/assess 会复查。只读审查期间或结果之后代码变化，标为 `stale`，不能直接沿用旧验收。若变化只是验收产物，或主 Agent 已检查新改动并重新验证，可加 `--accept-current-state` 显式验收当前指纹，备注具体变化和实际检查；原 Claude 报告仍对应旧状态，不冒充重新审查。否则启动当前范围的新验证。非 Git、超限或不支持的文件类型明确为 unknown，由主 Agent 人工核对；原先可核实而后丢失指纹时显示 unverified。assess 不运行测试。取消、超时、失败可能已修改部分文件，不自动回滚。

## 选择审查范围

范围参数互斥；`--path` 可叠加且重复指定。无范围参数时只交接任务与规则，不隐式选择 diff。继续会话需要新一轮代码审查时重新指定当前范围。

| 用户意图 | 参数 |
|---|---|
| 当前未提交改动（暂存和未暂存） | `--review-scope worktree`；需要已有 HEAD |
| 仅暂存区，包括首次提交前 | `--review-scope staged` |
| 当前分支相对 main 新增的提交 | `--branch-base main`；自动取共同祖先到 HEAD，不包含本地未提交改动 |
| 某次普通提交／根提交 | `--commit <ref>`；合并提交应明确比较方式 |
| 仅某些文件或目录 | 叠加 `--path src/ --path tests/`；单独使用默认 worktree |
| 明确要求两个状态直接比较 | 兼容 `--diff-base <ref>`；不等于分支共同祖先比较 |

worktree/literal 范围只列出符合筛选的未跟踪文件名，Claude 按需读取；staged/branch/commit 不附未跟踪文件。选中范围没有可读改动时在调用前报错，检查分支、暂存状态和路径拼写／大小写，不当作“审查通过”。分支／提交审查的磁盘文件可能与历史补丁不同，应说明覆盖限制。`--path` 只筛选上下文，不是写权限限制。超过 128 KiB 时缩小到相关路径或提供经过检查的差异，不静默截断或自动发送整个仓库。

## 续接与管理

“继续刚才 Claude 的任务”使用当前 Codex 聊天、当前项目的最近任务，无需手工寻找 result.json：

```bash
python3 <skill-dir>/scripts/jobs.py continue \
  --cwd /absolute/path/to/project \
  --task-file /absolute/path/to/work/followup.txt --background
```

- `list --cwd <项目>`：当前聊天最近任务；`--all` 查看本管理器所有聊天/项目。`--attention` 复查最近 `--limit` 个任务的代码状态，只显示需处理项；普通 list 为轻量历史，不复查代码。
- `status --cwd <项目> [--job <编号>] [--wait 30]`：进度；wait 最大 60 秒。
- `result --cwd <项目> [--job <编号>]`：最终报告；`--json` 包含完整调用元数据。
- `cancel --cwd <项目> [--job <编号>]`：请求取消；再用 status 确认。只有 `cancelled` 才表示调用已停止，文件不回滚。
- `doctor [--cwd <项目>]`：检查 CLI、默认模型和现有登录态；提供 cwd 时列出工具链及沙箱读权限。它只检查配置，不能代替实际运行测试；不启动登录，不输出邮箱或凭据。
- `assess --cwd <项目> --job <编号> --verdict passed|failed|blocked --note '证据'`：记录主 Agent 验收；未完成或失败的调用不能标 passed。

聊天归属自动取 `CODEX_THREAD_ID`；省略编号时只选当前聊天与项目。没有聊天标识时必须用确切编号或显式 `--owner`，不猜测最近任务。跨聊天交接先 `list --all` 定位，再显式指定原任务的 `--owner` 和编号。新任务不会自动续接；最近任务仍在运行或无可验证会话时拒绝继续，不悄悄跳到更早任务。

续接继承原模式、模型、effort 和显式工具链读路径，项目必须相同。**原会话不能切换读写权限**；review 转 execute 要在已有授权内新建任务并交接结论。取消/超时后，先检查部分修改与进程停止情况；已有原生 init 或 final 会话确认的任务可继续。没有这些证据的旧取消结果不能自动恢复。旧版完整报告可用 `continue --resume-from <旧 result.json>` 显式导入；有聊天归属的结果仍校验 owner。

本次覆盖用 `--model <完整官方ID>`、`--effort low|medium|high|xhigh|max`、`--timeout <秒>`（上限 3600）、`--max-turns <1–200>`。模型/effort 优先级：显式参数 > 恢复会话 > config.json；时间/回合每次按参数或配置重设。不默默换模型；effort 同时通过 CLI flag 和环境变量提交，后端实际强度不能独立观测。

任务默认保存在 `${CODEX_HOME:-~/.codex}/claude-jobs`，目录 0700、文件 0600，位于项目外。仅管理由本入口创建/导入的任务，不扫描用户所有 Claude 聊天。Git 内按整个工作树判断写入冲突，包括相邻子目录；非 Git 按重叠目录判断。独立 worktree 可并行。这是本管理器内的保护，不会自动阻止用户终端或主 Agent 写文件。

异常中断、存储位置调整和底层兼容调用时，读取 [管理与恢复](references/management.md)。

## 执行边界

- 使用 safe-mode、restricted、空 MCP、禁用自定义 hooks/skills/plugins；只开放所选模式工具。不能访问或操作微信界面，不能使用任何 UI、委派代理或网络工具。
- Bash 强制官方 OS 沙箱，禁止退出沙箱重试，沙箱不可用则停止。仅允许项目及官方临时目录写入；保护 `.git`、`.claude`、`.codex` 和 `.mcp.json`。两个模式的文件工具、命令沙箱、自动 diff 使用同一敏感路径表，覆盖 `.env` / `.env.*`、常见私钥、npm 配置、云凭据目录等；项目外用户目录及常见秘密环境变量受限。这不是所有秘密的完整检测器。
- 自动只读放行 PATH 中当前选中的 `~/.nvm/versions/node/vX.Y.Z`，不开放整个家目录或所有 Node 版本。npm 不加载用户/全局配置；内置空配置文件单独只读开放，项目 `.npmrc` 仍禁读。其他受限工具链可在主 Agent 核实任务需要后用 `--read-path <具体目录或文件>`，只放行本任务 Bash 读取，不授予写入或文件工具的目录外读取。不要为消除报错扩大到整个家目录。
- 命令默认不能联网、监听本地端口或使用 Unix sockets；Claude CLI 自身仍需连接官方服务。需要安装依赖、读取受限工具链或启动本地服务时报告具体限制，由主 Agent 在原任务授权内处理；不为使测试通过而自动放宽配置。
- 不提交、push、部署、发布或改权限/全局配置。禁止 `rm` 或语言 API 删除文件；必要删除交回主 Agent 用 `trash`。命令 deny 规则与提示词不是任意程序行为的完整防线；有写入权限就有覆盖项目文件的风险。
- 超时终止调用进程组，但不保证终止自行脱离进程组的子进程；不允许留下后台服务。

## 合规与结果

仅供账户所有者在自己机器上按需使用。直接复用本机官方 Claude Code 已有登录态，或使用用户已配置的官方 API key；不读取、导出或中转 OAuth token，不自动登录，不使用转发端点。遵守服务拒绝、限额、地区规则，不轮换账号、不规避拒绝。只有 CLI 确认登录态失效或不存在时，才交由用户在官方 CLI 登录，不索要密钥贴到聊天。

不把个人订阅做成共享服务、凭据代理或转售接口。涉及团队托管、公共服务、无人值守批量调用或认证变化时，先读 [合规依据与权限限制](references/compliance.md) 并核实最新官方规则。使用原版 CLI 是本方案的合规依据，不构成 Anthropic 单独授权或绝对保证。

交付时说明实际模型、请求的 effort、发现或修改、已验证和未验证项。保留 result.json 供追问；CLI 原生会话也可能保留代码。用户需下载的报告放入当前任务 outputs。CLI 成本估算不等于实际账单。不同模型一致不构成正确性证明。

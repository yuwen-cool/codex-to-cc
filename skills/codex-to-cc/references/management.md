# 任务管理与恢复

所有命令入口为 `python3 <skill-dir>/scripts/jobs.py`。这是一层个人本机任务管理，不是公共服务或持续批量自动化；不启动常驻服务、监听端口或自建认证。

## 状态与记录

- queued：后台工作进程尚未开始；running：正在调用。phase 反映最近的 Read/Edit/Bash 等活动，不保证模型当前正在做什么。
- completed：仅调用协议成功；检查 report、diff 与测试后才能验收任务。
- cancelled / timed_out：停止调用进程组，已有文件改动保留。
- incomplete_permissions / failed / unverified_model_or_output 等：查看 status、result；不自动重试、降级或放宽权限。
- interrupted：工作进程已不在，可能有孤儿进程和部分改动；不误报完成，也不自动释放执行冲突。

存储中每个任务有独立 task.txt（含提交时冻结的上下文）、context.json（来源、哈希与覆盖范围）、request.json、job.json、output/result.json 和 report.md；续接保留 parent_job、owner_chat_id 与原生 session_id。工具进度只存工具名计数，不额外持久化原始工具输出和推理。任务描述、最终报告与 Claude 原生会话仍可能含代码。

`assess` 将主 Agent 的结论保存为独立 assessment.json，不改原始调用结果。status/list/result 呈现最新验收状态，明确来源为 parent_assessment；直接读取原始 result.json 仍能看到当时的 needs_parent_verification。不能把保存了验收备注当成脚本自动证明了测试通过。

status/result/assess 和 `list --attention` 会复查 Git 代码指纹；普通 list 不扫描文件。指纹覆盖整个 Git 仓库的 HEAD、索引及未忽略的已跟踪/未跟踪普通文件，避免遗漏 cwd 外依赖；不把秘密文件内容、符号链接目标、忽略的构建产物纳入内容扫描。上限为 50,000 个路径、256 MiB 内容及约 10 秒文件扫描；非 Git、子模块或超限等返回 unknown，由主 Agent 人工检查。它不是原子快照、文件锁、秘密扫描器，也不能发现忽略文件中的行为变化。

只读审查过程中或调用结束后代码发生变化，旧结果呈现 `acceptance_status: stale`，result 返回非零，不能直接 assess passed；原始报告和原验收记录保留。若之前自动核实过的指纹现在无法检查，显示 unverified；快照期间文件被改动也属于这种情况。执行任务本身修改代码是预期行为，比较的是完成之后的状态。

验收测试产生未忽略的 `.pyc`、报告，或主 Agent 后续修改了代码，也会触发 stale，不自动把这些路径排除。检查 `git status` 与 diff，确认具体变化并完成对当前状态的必要验证后，可 `assess --verdict passed --accept-current-state --note '哪些变化、实际命令和结果'`。这将新的主 Agent 验收绑定到当前可测指纹，不更新 Claude 原报告；后续再变化仍会过期。指纹暂时无法读取时先解决或复查，不能用该参数跳过检查。非 Git 等从始至终无法生成指纹的任务仍允许普通人工 assess，且保留 unknown，不保证自动识别后续变化。

需要在审查进行中准备下一份交接文件时，放在被审项目外的本次任务 work/，或项目原已忽略的合适目录，避免人为改变被审代码状态。不要为让指纹通过而修改用户的 .gitignore 或静默过滤源代码。

`attention` 是供主 Agent 处理的状态，不是桌面通知。权限拒绝看 result 的 permission_denials；沙箱内命令失败可能只表现为工具错误或报告里的失败，不能把所有工具错误归为权限不足，也不能凭 completed 忽略它们。依赖安装、端口或工具链受限时，在已有授权内由主 Agent 完成必要准备；只有确实缺少授权/信息才询问。恢复时带上具体失败及新条件，不反复执行相同失败步骤。

默认选择同时限定 cwd 和 CODEX_THREAD_ID；旧任务没有归属时只可用明确编号/结果路径导入，不会被当前聊天的 latest 隐式接管。无 CODEX_THREAD_ID 的终端可自己选稳定 `--owner`，或者始终使用明确编号。owner 防止误操作，不是同一用户本机进程之间的安全隔离或身份认证。

`--state-dir /absolute/path` 必须放在子命令前，用于隔离测试或用户指定的存储。更换存储不会迁移任务，也不会共享冲突保护；正常使用固定默认存储。不得放到 Claude 可写的项目目录内。

## 中断恢复

正常取消通过控制文件通知所属工作进程，不直接对记录的 PID 发信号，避免 PID 重用导致误杀。取消后用 `status --wait 10` 确认结果；没有确认时只能说“已请求取消”。

取消/超时会保留官方流中 init/final 的会话确认；只有 ID 一致、无模型或流解析冲突的记录才允许 continue。继续前检查部分修改，不自动重跑已执行命令。原生 CLI 如果找不到会话会正常报错，不回退到最新会话。旧取消记录没有确认事件时，先检查并新建任务；不从未验证的 UUID 推断会话可恢复。

工作进程被强杀、机器重启等情形会出现 interrupted。检查 status 中的 worker_pid、cli_pid（若已有），用系统工具验证实际进程身份、运行时间和命令；**不能仅凭保存的 PID 杀进程**。检查项目 diff 和后台测试是否仍写文件。按当前授权停止确实属于该任务的残留进程后，记录核实依据：

```bash
python3 <skill-dir>/scripts/jobs.py resolve \
  --cwd /absolute/path/to/project --job <编号> \
  --note '已核实该任务进程停止，检查过部分修改及测试产物'
```

resolve 仅关闭 interrupted 的管理记录并释放冲突，不终止进程、不恢复文件、不证明任务完成。不能为启动新任务而跳过核实。单个任务保留为 closed_interrupted，历史可查。

正常运行的工作进程收到 TERM/INT 会请求取消并清理 Claude 进程组；无法保证清理主动脱离该进程组的子进程。主 Agent 不让 Claude 启动持久后台服务。

## 按需隔离与交接

单个写任务优先独占当前工作树；不为普通只读审查创建 worktree。确有并行写入需要时，主 Agent 使用 Git 原生 worktree，在项目外新目录建立独立分支，然后将该目录作为 --cwd。先检查源工作树的脏改动；worktree 默认只有已提交内容，不含用户当前修改。若任务依赖这些修改，应顺序执行或明确检查并转移所需补丁，不自动复制 `.env`、凭据或整个工作目录。

独立 worktree 不等于安全沙箱；Claude 的执行权限仍按本技能限制。结束后主 Agent 检查相对起点的 diff、实际测试和源工作树的新变化，再按当前授权接收改动；合并冲突不能通过覆盖用户文件解决。确认结果已接收且无独有改动后再清理，删除走可恢复的 trash 流程，不自动删除分支或工作目录。

停止后改需求可在确认停止并检查部分修改后 continue；当前入口没有运行中注入指令。原生会话丢失或需要 review→execute 时，新建任务，携带原始目标、当前实际 diff、未解决问题、原有禁改范围与验收命令。使用 assets/task-template.txt 按需组织，历史总结只作线索，磁盘文件与真实检查结果为准；不转发完整终端记录，不自动重跑已完成步骤。

## 旧调用与诊断

原来的 consult.py 参数继续兼容，适用于旧结果追问或检查底层调用：

```bash
python3 <skill-dir>/scripts/consult.py \
  --cwd /absolute/path/to/project --mode review \
  --task-file /absolute/path/to/work/task.txt \
  --output-dir /absolute/path/to/work/new-result --dry-run
```

去掉 dry-run 才实际调用；需要续接加 `--resume-from <result.json>`。此底层入口不登记任务，也没有管理器冲突保护，日常优先 jobs.py。后台任务需继续用同一份已安装代码管理，升级前检查现有任务。

需要挑战方案时，把“质疑实现选择、假设、取舍，给出替代方案和证据”写进 review 任务即可；尚无独立挑战式审查管线、完整聊天迁移或结束前自动审查钩子。

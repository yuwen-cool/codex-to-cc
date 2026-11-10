# 调用边界与官方依据

文档复核日期：2026-11-10。适用范围：账户所有者在自己的机器上，经未经修改的 Claude Code CLI，按需完成代码审查、讨论、编码、测试和追问。YwRelay 是独立开源工具，不隶属 OpenAI 或 Anthropic。以下是实现依据与设计判断，不是 Anthropic 对本项目的单独授权或合规保证。

## 认证与使用范围

[Claude Code Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) 按账户类型区分适用条款，并对在产品／服务中运行 Claude Code 单列条件：使用未经修改的程序、保留内置认证方式、由各终端用户自行认证并承担用量。不得代付、转售或中介用户用量。本仓库只分发 Skill 与辅助代码，不捆绑 Claude Code，不提供托管推理或共享账户；安装本项目不替代用户同意适用条款。

[Consumer Terms §3](https://www.anthropic.com/legal/consumer-terms) 对自动化访问设有一般限制，API key 或明确许可属于例外；同时禁止规避保护措施、共享账户和转售等行为。

[官方程序化运行文档](https://code.claude.com/docs/en/headless) 明确提供 `claude -p`、JSON 输出与恢复会话。结合原版 CLI 的个人登录说明，本实现采用用户自己的 CLI 进程进行按需调用，不提取 token，不直接模拟订阅接口。此为对官方文档的设计解释，不能推广成“所有订阅自动化都被允许”。

托管服务、公共接口、团队共享或批量自动化不在本发布版支持范围内；应另行核实官方运行条件及适用商业条款，不能把本地开源工具的使用方式推广为凭据中转许可。

对外名称使用 YwRelay，不使用供应商 logo，也不暗示官方背书。Claude Code 与 Codex 只用于说明兼容对象；相关名称属于各自权利人。

## 模型、effort 与失败

[模型配置](https://code.claude.com/docs/en/model-config) 给出固定版本 `claude-opus-5-5`，并支持 `high` effort；`opus` 是会变化的别名。环境变量 effort 优先于 CLI flag，因此子进程显式设置两者。

请求不配置备用模型，关闭遇到内容标记时自动换模型；不规避分类器、拒绝或限额。脚本从官方输出核对实际模型，后端未提供实际 effort 证明时明确标为请求配置。模型/访问可用性取决于账户和服务，不凭本地配置推断。

## 任务权限与实现边界

[CLI reference](https://code.claude.com/docs/en/cli-reference) 的 safe-mode 保留正常认证而关闭自定义扩展；restricted 限制文件工具的工作目录并移除未显式开放的命令工具。两个模式均隔离用户/项目设置、MCP 和扩展；组织管理策略仍优先，不自动改动它们。

[权限文档](https://code.claude.com/docs/en/permissions)：review 仅开放 Read/Glob/Grep。execute 额外开放 Edit/Write/Bash，通过 `Edit(./**)` 预授权项目内编辑，Edit 规则同时覆盖 Write。dontAsk 不弹确认，权限不足即拒绝；Bash 显式预授权，但每次命令仍必须进入 OS 沙箱。v2.1.311 不支持后来新增的 blockWritesOutsideWorkingDirectories，文件写边界依赖 restricted 和路径权限，不能假设新参数在旧版有效。

[官方沙箱文档](https://code.claude.com/docs/en/sandboxing)：execute 设置 enabled、failIfUnavailable、allowUnsandboxedCommands=false、excludedCommands=[]，禁止命令绕开沙箱。默认项目/临时目录可写；关闭外部网络、本地监听、Unix sockets、Apple Events，并限制项目外用户目录读取与常见环境秘密。shell 与文件工具分别限制；shell 沙箱不保护 Read/Edit/Write 工具本身。实测采用 >=2.1.311，旧版先拒绝。

两个模式共享 Read deny 规则；privacy.py 同时供工具权限、Bash 路径规则和 Git diff 过滤使用。官方说明 Read deny 对 Grep/Glob 为 best-effort，而 Bash 内任意程序必须靠 OS 沙箱约束；不能把一次探针通过扩大为完整防泄漏保证。

本机 nvm 安装的 npm 需要读取同一 Node 版本目录内的脚本和库。只重新开放所选版本的读取，不开放整个 home；其他运行时需任务级 --read-path。npm 用户配置指向 /dev/null，全局配置指向本技能内独立空文件（两者不能同一路径，否则 npm 会报 double-loading config）。命令外网、写权限和凭据规则不因工具链读取放行而放宽。

safe-mode 不加载项目规则，因此主代理程序在提交时显式附加适用的 AGENTS.md/CLAUDE.md 与可选过滤 diff。规则和 diff 是任务上下文，不能扩张权限。过滤按已知路径和符号链接进行，不承诺发现任意源码中嵌入的秘密；原始手工任务文件仍由调用者负责选择内容。

这些是降低暴露范围的技术限制，不是完整恶意代码隔离：项目可写、系统工具可读、秘密命名无法穷尽，管理员配置、CLI/OS 缺陷仍会影响结果。任意解释器也可能删除项目文件，所以删除/提交等限制包含任务指令，不能声称仅靠命令黑名单完全防止。任务范围必须通过 diff 与测试验收。

只读会话不能升级为执行会话；恢复绑定原权限和项目。输出目录 0700、文件 0600，必须放在 execute 项目目录外。无新增对外监听服务；官方 CLI 的网络连接与本地会话文件仍存在。

## 本机任务管理

jobs.py 在用户本机启动有限时的官方 CLI 工作进程，复用同一登录态。注册表只保存任务描述、进度摘要、最终报告和会话关联；不保存认证凭据，不监听端口。按需后台执行不改变认证方式，不构成对批量托管或对外转售的许可。默认目录在项目外；管理器冲突保护只覆盖同一存储中登记的任务。

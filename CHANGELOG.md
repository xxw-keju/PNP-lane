# 0.4.2

- Add an opt-in `enforce_default_for_agents` setting that rejects stale explicit publisher overrides during agent preparation. Preserve deliberate per-job selection through the dashboard and account assign.

# 0.4.1

- Restore access through local SSH/editor port forwards when the browser port differs from the server port. Keep loopback-only hosts and exact Origin/Host checks for account mutations.

# 0.4.0

- Separate proof agents from GitHub publishers; register multiple local gh users and choose the default in the dashboard or CLI.
- Allow explicit publisher selection at prepare and reassignment of idle jobs before any publication attempt; retain valid compilation receipts and rebuild unpublished commits with the chosen author.
- Isolate each job's credentials, fork, commit identity and PR head. Ignore ambient GitHub tokens and avoid global authentication switches; pin attempted publications for safe recovery.
- Show publisher identities alongside agents, include all registered accounts in branch checks/history, and protect local account controls with origin, CSRF, revision and job-lock checks.

# 0.3.0

- 自动发现尚未上架的公开提案，独立刷新并缓存公开题面。
- 任务清单新增全部、已上架、未上架切换，默认展示全部；显示预研登记人。
- 未上架权重保持未知，上架后去重并采用正式权重，已 checked 或撤回项目不会重新出现。
- CLI 新增 tasks --proposed / --include-proposed，保留 --eligible 的正式任务边界。

# 0.2.0

- 支持公开提案提前登记，正式上架后核对完整题面与发布资格。
- 新增 precompile 和同工作目录编译记录复用，输入变化或记录过期时重新编译。
- 新增 ready 查找入口，支持发布包装器复用提前登记的作业。
- 网页显示预研、预编译、复用情况和独立发布阶段耗时。
- 保留同名分支原子防覆盖、单文件提交、PR 恢复和发布后完整验证。

# 0.1.0

- 从现有本地 lane 拆出独立 Python 包，提供 `pnp-lane` 和兼容入口 `pnp-fastlane`。
- 配置 GitHub 身份、公开仓库、数据目录和可选 agent 会话路径；不携带原部署数据。
- 保留实时任务/PR 清单、1–5 难度排序、带证据和历史的 agent 评估。
- 保留原子创建分支、单文件提交、PR 恢复及提交后完整验证。
- 作业冻结作者身份，恢复时复核当前 GitHub 账号。
- 打包只读本地网页，正确关联多 agent 同题作业的实际发布者及最终 PR 状态。
- 增加初始化、只读环境诊断与前台双服务启动命令。

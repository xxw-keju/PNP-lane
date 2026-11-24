# Agent 工作约定

1. 所有 agent 共享一份配置与数据目录；每个证明使用独立 worktree。只启动一个共享刷新器。
2. 用 `pnp-lane tasks --eligible --limit 0` 初选，检查已有评估和反证依据，并独立评估证明路线。
3. 实质评估、搜索结束、换题或找到新路线后，用 `assess` 写简短公开摘要；分数按 README 的五档定义。不要把超时报告为无解，也不要无变化重复写入。
4. 准备好 PR 描述，用 `prepare --agent 自己 --task ID --job 新目录 --body-file 文件` 冻结元数据。初选被拒绝就换题，不用旧脚本或 gap-json 绕过。
5. 写好证明后用 `run` 做最后一次普通编译、提交和完整验证；不要先等待一遍完整验证再发布。当前正在进行的编译无需为切换流程重复重跑。
6. 同名分支存在时保留草稿换题；不会覆盖。多个 agent 可以独立尝试同题，本系统不建立互斥占题机制。
7. 如果工具失败，保留 job 日志并修复，不静默切回另一套发布流程。PR 创建响应丢失时，同一个 job 会按冻结的 head 找回；不会重复创建。
8. 本地验证通过不表示 PR 会合并；以官方检查与 GitHub 的 open/closed/merged 状态为准。
9. 会话通知和任务指令仍由 agent 管理器完成；本包不自带跨会话认证或远程操控功能。
10. 公开提案可用 `prepare --proposed ID --imports MODULE` 提前登记归属和进展，不互斥占题。准备好的证明用 `precompile JOB` 在专属 worktree 编译。上架后对原 job 运行 `run`，不要再 fetch、复制或重新 prepare 破坏其冻结输入。过期或修改后的记录需要重新编译，发布资格、题面及 imports 以正式任务为准。


## Publisher selection

Agent attribution (`--agent`) is independent of the GitHub publisher. `prepare --account LOGIN` selects a registered account; omitting it uses the current default. `account use LOGIN` only changes future jobs. `account assign JOB_ID LOGIN` changes one idle, unpublished job under the worktree/job locks and preserves its proof and valid receipt. A local commit with a previous author is rebuilt.

Publication attempts permanently lock the publisher, including failed or ambiguous push/PR responses. Recovery uses the original account, head and commit. Every job resolves its explicitly named gh user without changing global active authentication or inheriting ambient tokens. Compiler/checker environments do not receive GitHub credential variables.

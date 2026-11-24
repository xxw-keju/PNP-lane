# PNP Lane 0.4.1

一个可本地部署的 pequalnp 协作工具：实时任务/PR 清单、agent 难度评估、证明提交流水线和可选择提交账号的进度网页。

程序使用 Python 标准库，**没有 Python 运行时依赖**。支持 Linux / WSL；使用 POSIX 文件锁，尚未验证 macOS，不支持原生 Windows。

## 安装与启动

需要 Python 3.10+、Git、GitHub CLI (`gh`)、curl、Node.js，以及项目要求的 elan/Lean/Lake。Lean 和 Mathlib 使用证明仓库锁定的版本。

从本仓库安装：

```bash
git clone https://github.com/xxw-keju/PNP-lane.git
cd PNP-lane
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

gh auth login
pnp-lane init --from-gh
# 先在 GitHub 建立项目 fork，再登记已登录的账号。
pnp-lane account add YOUR_LOGIN
pnp-lane doctor --repo /path/to/pequalnp-proofs
pnp-lane serve
```

打开 **http://127.0.0.1:8766**。`serve` 同时启动任务刷新器和网页，Ctrl-C 停止本次启动的两个进程。它只读任务与 PR，不会选题占用 desk、编译或提交证明。

如需放在后台，可在 tmux 中运行 `pnp-lane serve`。端口被当前系统占用时用 `--port 8767`。远程机器请转发相应端口。

如使用完整 ZIP 包，解压后也可用 `python -m pip install ./wheels/pnp_lane-0.4.1-py3-none-any.whl` 安装。兼容原命令名 `pnp-fastlane`；两者指向同一入口。

## 配置和数据

`init --from-gh` 读取当前 GitHub 登录的账号和 id，生成该账号的 GitHub noreply 邮箱，不复制 token。也可显式配置：

```bash
pnp-lane init --account YOUR_LOGIN --email YOUR_NOREPLY_EMAIL
```

- 默认配置：`~/.config/pnp-lane/config.json`，或 `$XDG_CONFIG_HOME/pnp-lane/config.json`。
- 默认数据：`~/.local/state/pnp-lane/`，或 `$XDG_STATE_HOME/pnp-lane/`。
- `PNP_LANE_CONFIG` 可指定独立配置文件；`PNP_LANE_STATE_DIR` 可指定独立数据目录，优先于配置中的 `state_dir`。
- 程序安装目录只保存代码和网页资源。缓存、作业、评分及服务日志都写入数据目录。
- 多个 agent 使用同一份配置及数据目录即可协作；不同部署使用不同目录，避免重复刷新。

完整格式见 [config.example.json](config.example.json)。其中 `account`、`email` 应换成自己的信息。`repository` 和 `feed_url` 默认指向 pequalnp；流水线依赖该项目的目录和检查协议，不是任意 GitHub 仓库的通用发布器。

会话观察是可选功能。为对应 agent 填写 `transcript`，可读取 Codex/Claude 的本地 JSONL；Codex 也可填写顶层 `codex_db` 和 agent 的 `thread`。默认不连接任何现有会话，不包含原机器的会话 id。仅展示公开说明和工具活动，不展示内部推理或原始工具输入。配置中的 agent id 也用于作业和评分归属。

## 选择提交账号

agent 负责证明；GitHub 账号负责 commit、fork 推送及创建 PR。二者独立，同一 agent 可用不同账号提交，不同 agent 也可共用一个账号。

```bash
# 登录并为每个账号建立项目的同名 fork 后登记；登记只读取凭据，不复制 token。
pnp-lane account add FIRST_LOGIN
pnp-lane account add SECOND_LOGIN
pnp-lane account list

# 默认账号只影响随后 prepare 的新作业。
pnp-lane account use SECOND_LOGIN

# 指定新作业的账号，优先于默认设置。
pnp-lane prepare --repo /path/to/worktree --agent pnp2 --account FIRST_LOGIN --task L-meta-XXXX

# 更换一个尚未尝试推送、且当前未运行的作业。JOB_ID 是 status 中的 job_id。
pnp-lane account assign JOB_ID SECOND_LOGIN
```

网页的「提交账号」可设置默认账号；「阶段详情」可为尚未发布的作业单独切换。切换会保留证明、agent 归属和有效的预编译记录；已构造但尚未推送的 commit 会按新身份重建。运行中的作业会拒绝切换。只要开始尝试推送，哪怕响应丢失，也锁定账号，恢复时继续使用原身份和分支；已发布 PR 的作者不能通过切换改变。

`account add` 验证登录身份、项目 fork 和写权限，提交作者邮箱自动使用该账号的 GitHub noreply 邮箱。默认设置、登记记录和变更历史保存在数据目录的 `accounts.json`，不写入证明仓库。旧配置的 `account/email` 仅作为首次登记前的兼容默认值；有 registry 后以 registry 为准。

每次发布从本机 `gh` 的凭据库按明确的用户名取凭据，不调用 `gh auth switch`，也不继承外部 `GH_TOKEN/GITHUB_TOKEN`。凭据只传给该作业的 Git/GitHub 子进程，编译和检查进程不继承这些变量。允许多个账号的作业并行。其他脚本自行使用 gh 时的账号不受 lane 控制。

如想完全分离登录目录：

```bash
GH_CONFIG_DIR=/path/to/second-gh gh auth login --hostname github.com
pnp-lane account add SECOND_LOGIN --gh-config-dir /path/to/second-gh
```

不要在网页输入 token。额外账号需要先在本机登录并登记；单靠改用户名无法切换真实提交身份。任务与 PR 数据共用一份，清单会检查所有登记账号的同名分支，看板也会保留旧作业所属账号的历史 PR。

## 任务清单和难度评估

```bash
# 默认联网更新；筛选无任何状态 PR、无己方分支、权重大于 1 的题。
pnp-lane tasks --eligible --limit 0
pnp-lane tasks --cached --eligible --json
pnp-lane tasks --task L-meta-969
pnp-lane tasks --eligible --max-difficulty 2
pnp-lane tasks --sort weight
pnp-lane tasks --cached --proposed --json
pnp-lane tasks --cached --include-proposed --json
```

默认先按难度升序，同分按权重降序。难度定义：

| 分数 | 含义 |
|---|---|
| 1 | 直接可证明 |
| 2 | 可以证明 |
| 3 | 需要一点难度 |
| 4 | 需要很长枚举 |
| 5 | 开放问题 |

未评分单独标注，暂按 3 档排序。不同 agent 的有效评分暂取最高分，并标明分歧。分数仅是评估，不改变官方权重。

网页任务清单默认展示全部，可切换“已上架／未上架提案”。后台从公开 head 和 map 自动发现所有仍待证明的提案，不要求 agent 已经登记。未上架提案按公开更新时间排列，权重标为未公布；已登记的预研 agent 也会显示。正式上架后自动转入已上架列表；已 checked、撤回或不再属于公开 conjecture/in_work 的项目不会作为新提案出现。公开提案不保证以后上架，也不会因出现在清单中获得提交资格。

首次同步先显示题号，标题和题面在后台分批补全。正式题单、PR 和提案分别刷新，网络慢时请求不堆积。CLI 的 `--proposed` 只显示未上架提案，`--include-proposed` 包含它们；`--eligible` 始终只匹配通过正式资格核对的已上架任务。

```bash
pnp-lane assess --task L-meta-XXXX --agent pnp2 --difficulty 2 \
  --feasibility viable --reason '已有可复用引理，剩余有限验证' --minutes 20

pnp-lane assess --task L-meta-969 --agent pnp2 --feasibility refuted \
  --evidence-task L-meta-970 --reason '同一模型已验证的下界为12，目标要求至多11门'

pnp-lane assessments --task L-meta-969
pnp-lane assess --task L-meta-XXXX --agent pnp2 --retract --reason '先前判断不再适用'
```

`unknown` 是尚无结论，`viable` 是有具体路线，`blocked` 是当前受阻，`refuted` 是有反证依据。搜索超时只记 `blocked`。反证报告必须引用官方 checked 中的另一题并说明矛盾；一般逻辑蕴含仍由报告者核对，界面会标明是 agent 判断。完整题面的直接否定可自动识别。

每次评估独立保存，带作者、时间、题面指纹、依据和历史。新记录只更新自己的当前意见；题面或依据改变后旧记录失效。反证或相互冲突的可行/反证报告会暂时排除初选；高难度本身仅影响排序。

## 提交证明

先建立 pequalnp 的 fork 和独立工作目录，配置 `upstream` 为项目仓库；发布目标由所选账号确定，不依赖工作目录的 `origin`，并按官方说明预建 Mathlib。`doctor` 会检查必需的仓库脚本是否存在。

```bash
git fetch upstream main
# 选择自己的独立工作目录后准备题面。job 必须是仓库外的新目录。
pnp-lane prepare --repo /path/to/worktree --agent pnp2 --task L-meta-XXXX \
  --job /path/outside/repo/job-XXXX --body-file /path/to/pr-body.md

# 写好证明后，这个命令负责最终普通编译及后续步骤。
pnp-lane run /path/outside/repo/job-XXXX
pnp-lane status /path/outside/repo/job-XXXX
```

顺序：格式守卫 → 普通编译 → 单文件提交 → 原子创建分支 → 立即创建 PR → 完整本地验证。

- `prepare --task` 联网核对可选条件并冻结题面、base，记录当前选定的作者身份；兼容 `--gap-json`，但它用于已完成初选的元数据，不应绕过拒绝。
- 编译和完整验证调用证明仓库自带的 `tools/bin/lake`、`tools/build.sh` 和 `tools/check-lean.mjs`，沿用官方队列、固定工具链与仓库的构建缓存。包不携带 Lean、Mathlib 或证明仓库。
- 编译后不查询竞争 PR、不 fetch/rebase。编译期间源码或基线变化会停止发布。
- 使用 create-only 的空旧值 lease 推送：同名远端分支已存在就停止，绝不覆盖或改名重复提交。
- 不移动调用者 HEAD，不改真实 Git index；通过临时 index 创建仅新增目标证明文件的 commit。
- GitHub HTTPS 认证使用该作业指定的 `gh` 用户凭据；运行前复核身份，commit 作者和提交者、fork 以及 PR 使用同一账号。
- PR 正文由 AI 的 `--body-file` 提供，lane 原样提交；未提供时生成固定简短模板。lane 不生成证明思路或文献引用。
- PR 打印 URL 后仍在运行完整本地检查，退出前不要改变该工作目录。
- 本地 ACCEPTED 与官方合并是不同状态；网页分别显示，关闭的 PR 不会显示成等待合并。

同一 job 可恢复推送后创建 PR 的失败，或重新进行完整验证；已关闭 PR 不自动重开。详见 [运行规则](docs/WORKFLOW.md)。

## 提案预研与预编译

公开提案尚未上架时，可以先登记到共享网页，并让 lane 提前编译。使用专属工作目录，保留到发布及完整验证结束。提案源只有数学题面，尚无正式权重及允许 imports；以下 imports 和证明方向是暂定值，正式上架时必须完全一致。

```bash
pnp-lane prepare --repo /path/to/dedicated-worktree --agent pnp4 \
  --proposed L-meta-XXXX --imports PEqualNP.Defs --kind check \
  --job /path/outside/repo/proposed-job --body-file /path/to/pr-body.md

# 在该工作目录的目标文件中写好证明，再预编译；此步不会推送或创建 PR。
pnp-lane precompile /path/outside/repo/proposed-job

# 上架后发布。run 自动识别并核验已有的 lane 编译记录。
pnp-lane run /path/outside/repo/proposed-job
```

- `--proposed` 从公开 head、map 和 lemma 文档读取题面，不领取任务。网页显示“提案预研／预编译通过”；未上架不计入可选任务，也不虚构权重。
- `--imports` 可重复；refute 方向显式用 `--kind refute`。提案的 `kind=helper` 是分类，不是正式任务的证明方向。
- 预编译记录有效期 30 分钟，绑定同一 job、工作目录、源码与文件状态、base、题面、imports、方向、编译器、依赖源码和构建缓存。依赖目录的符号链接也会跟踪。编译失败或输入变化不能沿用旧记录。
- 发布时重新核对正式任务仍开放、权重 > 1、没有同题 PR 或己方分支，以及共享反证评估。仅当完整共享快照中任务与 PR 核对时间都在 2 秒以内且包含此题时复用快照，否则联网更新。快照与 GitHub 操作之间仍可能发生竞争；原子创建分支防止覆盖。
- 收到正式题面、imports 或方向变化时停止，重新 prepare。源码或环境变化、记录过期时普通 run 会重新编译；base 已改变则重新 prepare。可用 `run JOB --compiled-sha SHA256` 要求必须复用有效记录，无法满足就停止，不能凭该参数跳过验证。
- 提前编译完成的作业在发布前不占工作目录锁，但它的专属目录应保持不变。不要将草稿复制到另一个 worktree 后沿用原记录。
- 包装器可用 `pnp-lane ready --task ID --agent pnp4 --source /path/to/proof.lean` 找到相同源码的登记 job，随后直接 run，省去上架后的 fetch、prepare 和复制。ready 只负责查找，发布资格仍由 run 验证。
- 发布时并行核对身份和编译环境，复用有效记录后直接构造提交并原子推送；完整本地检查仍在创建 PR 后执行。网页单独展示发布阶段耗时，避免把等待上架的时间误算成发布延迟。6–8 秒是目标，实际取决于网络、缓存与资格刷新，不保证达成。

编译记录是同一机器上的完整性检查，不是针对拥有同一账号写权限的恶意进程的认证凭证。不同部署不应复制记录后直接发布。

## 实时刷新与独立启动

```bash
pnp-lane tasks --watch 1 --pr-interval 5 --eligible --limit 0
pnp-lane dashboard --port 8766
```

任务源目标间隔 1 秒，PR/己方分支独立 5 秒，官方检查评论 60 秒。使用条件请求；一个来源同时只保留一个请求，慢请求不堆积。来源本身的缓存仍会影响新数据发布时间。首次 PR 历史扫描可能较慢；失败会保留旧快照并明确显示错误。

网页只监听 loopback，进度 API 为只读；写接口仅允许切换已登记的默认账号或尚未发布的作业账号，要求同源请求、CSRF token 和版本核对。无提交、重试、登录或评分写入接口。agent 通过 CLI 回写评估。记录目录和可选会话文件仅供部署机器读取。

## 测试与打包

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v
python -m pip install build
python -m build
python scripts/make_bundle.py --wheel dist/pnp_lane-0.4.1-py3-none-any.whl --output /path/to/releases
```

测试使用临时真实 Git 仓库和模拟编译器/GitHub，不创建真实 PR。独立安装验证使用空的配置及数据目录。

发行包仅含源码、网页、示例配置、文档、测试及 wheel。不包含任何现有运行记录、会话内容、GitHub 登录、token、证明草稿、模型工具或原机器进程信息。升级时保留配置和数据目录；在途作业仍使用原身份。备份与迁移现有数据的边界见 [迁移说明](docs/MIGRATION.md)。

仓库沿用 [Apache-2.0 许可证](LICENSE)。公开内容与本地数据的边界见 [隐私说明](docs/PRIVACY.md)。

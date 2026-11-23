# Little-Bear-Bookkeeping（小熊记账）

基于多范式编程（Multi-Paradigm Programming）构建的个人记账与预算预警应用，涵盖数据持久化、纯函数业务规则与汇总计算、异步维护与备份检查以及前后端交互界面。

- [GitHub 仓库](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping)
- [前端应用入口 · Streamlit Community Cloud](https://little-bear-bookkeeping-2ppuqrbguqmlayds7pcqkn.streamlit.app/)
- [后端 API 文档 · Render](https://little-bear-bookkeeping.onrender.com/docs) · [健康检查](https://little-bear-bookkeeping.onrender.com/health)

---

## 1. 系统架构与目录结构

项目采用模块化分层设计，将数据库会话与模型、纯函数规则与汇总计算、异步维护控制、前后端入口与自动化测试解耦：

```text
Little-Bear-Bookkeeping/
├── .github/workflows/       # GitHub Actions CI 工作流配置
├── backend/                 # 后端 API / 控制器与服务封装
├── data/                    # 示例数据（如 sample_data.json）
├── database/                # 数据库模型（models.py）、会话管理（session.py）
├── frontend/                # 前端界面（app.py，Streamlit 应用）
├── maintenance/             # 维护与备份检查逻辑（controller.py）
├── processing/              # 核心业务规则（rules.py）与汇总服务（service.py）
├── tests/                   # 单元测试（test_unit.py）与集成测试（test_integration.py）
├── .env.example             # 环境变量示例模板
├── .gitignore               # Git 忽略规则
├── README.md                # 项目说明与工程记录
├── requirements.txt         # Python 依赖清单
└── start.py                 # 本地/统一启动入口
```

用户在 Streamlit 表单输入类别和金额。前端向 FastAPI 提交 JSON，后端从 SQLAlchemy 查询该类别当月已有支出，由 `BudgetAdvisorService` 调用预算规则，将账目及其入账时的预警状态写入数据库，再把结果交回前端。页面通过 API 刷新图表和账目列表。当前演示使用单本账簿，适合合成数据。

## 2. 本地运行（Windows PowerShell）

在仓库根目录执行。项目的 `.python-version` 指定 Python 3.12.7，当前 GitHub Actions 使用 Python 3.11。旧压缩包附带的虚拟环境记录了原电脑的解释器路径，新电脑应重新创建。以下命令在 `venv` 中安装依赖，不依赖 PowerShell 的激活脚本。

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe start.py
```

打开 [本地前端](http://localhost:8501) 或 [本地 API 文档](http://127.0.0.1:8001/docs)。用 Ctrl+C 关闭启动脚本。若要独立启动，在两个终端分别运行以下两条命令，并分别用 Ctrl+C 停止。修改端口时同步更新前端的 `BACKEND_URL`。

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
.\venv\Scripts\python.exe -m streamlit run frontend/app.py --server.port 8501
```

本地使用 Python 自带的 SQLite 驱动。后端启动时建表，默认文件为 `expense_tracker.db`。`DATABASE_URL` 选择数据库，`DEFAULT_MONTHLY_BUDGET`、`BUDGET_FOOD` 等变量可设定默认预算和各类别预算，`BUDGET_WARNING_RATIO` 设置预警比例。示例配置见 [.env.example](.env.example)。前端部署时，入口为 `frontend/app.py`，在 Streamlit 的配置中将 `BACKEND_URL` 设为 Render 的公开后端地址。Render 的 Web Service 使用仓库根目录和启动命令 `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`。数据库连接值只填在相应环境的私有设置中。

## 3. 月度预算演示

钱款来自用户录入。仓库中的 [三笔合成消费](data/sample_data.json) 可手工录入。使用空的本地演示数据库、同一 UTC 自然月和默认 Food 预算 1500，按文件顺序提交，应看到下表。重复提交会新增一笔消费。历史账目可以跨月查询。

| 新增金额 | 本月累计 | 预期状态 | 原因 |
| ---: | ---: | --- | --- |
| 300 | 300 | NORMAL | 已用 20% |
| 900 | 1200 | WARNING | 恰好达到 80% 预警线 |
| 350 | 1550 | EXCEEDED | 超出预算 50 |

金额按分录入，单笔录入与分类汇总共用预警判断。预算为 1500 时，1199.99 仍为 NORMAL，1500.01 已为 EXCEEDED；先根据金额判定状态，再格式化百分比。某类预算为零时，没有消费显示 NORMAL，新增正金额则全部计为超支；这时比例在 API 中为 `null`，页面显示 `N/A (zero budget)`。

也可以在启动本地后端后执行下列 PowerShell 命令。先在 `.env` 中给演示设置独立的 SQLite 文件，例如 `DATABASE_URL=sqlite:///./demo_expense.db`。后端启动后用这个文件保存演示记录。

```powershell
$demoRecords = Get-Content -Raw data/sample_data.json | ConvertFrom-Json
foreach ($record in $demoRecords) {
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8001/expenses' -ContentType 'application/json' -Body ($record | ConvertTo-Json -Compress) |
        Select-Object updated_spent, status, over_amount
}
```

| 业务接口 | 用途 |
| --- | --- |
| `POST /expenses` | 添加一笔消费，返回入库 ID、预算状态及超支额 |
| `GET /expenses` | 倒序读取历史账目，可加 `?category=Food` |
| `GET /expenses/{expense_id}` | 按 ID 取详情，缺失返回 404 |
| `GET /summary` | 返回当月各类别累计金额、预算和状态，供图表使用 |

## 4. 代码讲解与设计取舍

- **过程式**。在 [backend/main.py](backend/main.py) 中，`create_expense` 按查询本月累计、评估、写库及返回的顺序工作；[database/session.py](database/session.py) 中的 `get_db` 管理事务提交、回滚与关闭。
- **函数式**。[processing/rules.py](processing/rules.py) 中的 `evaluate_budget_status` 和 `summarize_expenses_by_category` 只根据传入的数值与记录计算。固定输入会得到固定结果，可以用表中的数值直接检验。
- **面向对象**。[processing/service.py](processing/service.py) 中的 `BudgetAdvisorService` 持有分类预算和预警线，提供单笔评估与分类汇总两种行为。FastAPI 通过依赖注入取得该服务。

**月份与历史**。本地修订按账目录入时间所属的 UTC 自然月计算。同一类别的上月记录仍可查；月初无需清表，也不依赖部署服务器的本地时区。这一做法没有提供按用户时区结账或补录消费日期的功能。

**预警状态留档**。每笔账保存入账时的预警标签，概览依据当前预算重新汇总。预算调整后，历史详情仍能显示当时的标签。要完整还原触发原因，还需要当时的预算和预警线，当前表只保存账目与标签。恰好用完预算时显示 WARNING，超过预算才显示 EXCEEDED。

## 5. 测试与 CI 记录

在项目根目录执行 `python -m pytest`（需使用项目虚拟环境）。[测试目录](tests/) 包含参数化预算规则测试、维护判断及 API 与隔离 SQLite 的集成测试。CI 工作流在 push 和 pull request 时安装固定依赖并运行同一命令。下文列出本地验证结果，并附已推送提交对应的公开 CI 运行。

2026-11-23，已推送至 `main` 的提交 `778fdb4` 在本地 Python 3.11 上通过 34 项测试，另有 1 条依赖弃用提示。该提交覆盖月度预算、API 持久化与 SQLite 恢复，对应的 GitHub Actions 已完成并通过。[778fdb4 的 CI 运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45159557990)

加入云服务接口模拟、写保护和恢复校验后，2026-11-23 在 SQLite 和独立的 PostgreSQL 16.16 上运行完整套件，88 项通过，另有 1 条依赖弃用提示。若设置 `MAINTENANCE_TEST_POSTGRES_URL`，同一 pytest 套件还会在本机 PostgreSQL 上为每个测试创建独立 schema。CI 配置启动隔离的 PostgreSQL 16 服务。

在独立的 `debug/ci-fault-demo` 分支，提交 `6132f4f` 将超支判断从 `usage_ratio > 1.0` 改为 `> 10.0`，使已有用例中累计 1010、预算 1000 的消费被判为 WARNING。提交 `970a536` 恢复原阈值，测试转绿。[故障运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45039678262) · [修复后运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45043678181) · [主线 6edba96 的运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45132165957)

## 6. 云数据库与维护操作

### 6.1 线上已观察到什么

**2026-11-23 的跨重新部署持久性检查失败。** 原部署 `dep-dc1up9u0tbcc73b6n8hg` 中可读到合成消费 `id=1`、类别 `Other`、金额 `1.23`、备注 `course-persistence-20261123-r1`。同一提交 `6edba96` 的新部署 `dep-dc21jfajnfac73a2vdug` 于北京时间 18:48:41 显示 Live。18:48:53 再次读取时，`/health` 返回 `ok`，`/expenses/1` 返回 HTTP 404，`/expenses` 返回空列表，`/summary` 中 Other 合计从 1.23 变为 0。后端已恢复服务，但这笔账目未保留。接入持久数据库后，应使用新标记重复同样的前后比对。[部署前记录](docs/evidence/persistence-before-20261123.json) · [部署后记录](docs/evidence/persistence-after-20261123.json)

重部署后，Streamlit 的 Connected API 显示本项目的 Render 地址，Monthly Budget Overview 图表已加载。Expense Records 选择 All 时显示 `No expense records found for the selected filter.`，与后端返回的空列表一致。[前端及 API 核验记录](docs/evidence/persistence-after-20261123.json)

2026-11-23 在 Render 账号内核查，Production 项目列出一个免费 Web Service。服务没有配置环境变量、Secret File 或环境组，启动命令为标准 uvicorn；当时部署的 `6edba96` 未包含 `.env`，按源码回退到 `sqlite:///./expense_tracker.db`。该免费服务不支持持久磁盘。当前应用在临时文件中保存账目。若其他平台上已有实例，仍须查明该实例并接入应用。课程要求第 6.5 节的真实云替换尚待验收。支持写保护的版本在 `GET /health` 响应头中返回 `X-Bookkeeping-Write-Guard: 1` 和数据库方言 `X-Bookkeeping-Database`。若 Render 提供合法发布 SHA，还会返回 `X-Bookkeeping-Commit`。这些头可核对运行版本与驱动；数据库归属、配额及内容保留需另行验证。

### 6.2 只读诊断和定时检查

在控制器电脑的私有 `.env` 填写 `RENDER_API_KEY`、后端 `RENDER_SERVICE_ID`。下列命令读取指定 Web Service 的数据库配置，仅输出数据库类型与可识别的提供商。若结果为 `unknown` 或服务上未找到该配置，需在服务及其环境组内核实。前端和 Render Dashboard 的域名都不能确定数据库托管方。

```powershell
.\venv\Scripts\python.exe -m maintenance.render_provider diagnose
```

当前云实例适配器支持 **Render Postgres**。确认实际数据库与资源权限后，在同一份私有配置中设置 `MAINTENANCE_PROVIDER=render-postgres`、`SOURCE_DB_ID`、`RENDER_OWNER_ID`，并将 `BACKEND_URL` 设为该已部署后端地址。控制器与后端应使用相同的分类预算和预警线。可在独立终端运行一次检查或连续监测。

```powershell
.\venv\Scripts\python.exe -m maintenance.workflow check
.\venv\Scripts\python.exe -m maintenance.workflow watch
```

`check` 执行一次异步查询；`watch` 按 `CHECK_INTERVAL_SECONDS` 定期查询活动实例，按 Ctrl+C 停止。统一入口 `start.py` 在配好 Render 监测条件后会自行启动只读 `watch`，无需再开第二个。未启用云监测时仍可运行本地记账演示。监测与应用启动均不授予替换或删除权限。

### 6.3 有明确资源授权后的正常替换

1. 选择只存放本演示合成数据的专用源实例，核对源、工作区与后端标识、源仍可读、网络权限、真实配额和备份主机空间。按实际情况设置 `TARGET_DB_PLAN`、`TARGET_DB_REGION`、`TARGET_DB_VERSION` 与 `DB_INSTANCE_LIMIT`（1 或 2）。若指定时刻演练，`MAINTENANCE_AT_UTC` 使用带偏移的 ISO 8601 时间，例如末尾为 `+00:00`；`MAINTENANCE_WAIT_SECONDS` 限制本次等候时间。后端须先部署支持数据库写保护的版本。云端另有业务数据或所用服务商与适配器不符时，应先确定相应的专用演示资源和课程认可的方案。
2. 若只允许一个云实例，先在本机安装与源、目标相同主版本的 PostgreSQL，在 pgAdmin 中新建专用的**空数据库**，为仅用于此次试恢复的角色授予该库建表权限。将私有 `TRIAL_DATABASE_URL` 指向该库的 loopback 地址。控制器使用 `requirements.txt` 中固定的 `psycopg2-binary` 驱动，以 SQLAlchemy 和 JSON 完成备份还原。该实现无需安装 `pg_dump`。两实例路径在验证新库与切换成功后才删除旧库。[PostgreSQL Windows 安装入口](https://www.postgresql.org/download/windows/)
3. 仅当负责人已授权对**指定源实例**执行替换及删除时，才执行下列命令。首次生成一次运行 ID，将其与三个经核实的资源 ID 留存。命令会与私有配置逐一匹配。

```powershell
$runId = [guid]::NewGuid().ToString()
$sourceId = Read-Host '已获准替换及最终删除的数据库实例 ID'
$backendId = Read-Host '已获准切换的 Render Web Service ID'
$workspaceId = Read-Host '上述资源所在的工作区 ID'
.\venv\Scripts\python.exe -m maintenance.workflow run --run-id $runId --authorize-source $sourceId --authorize-backend $backendId --authorize-workspace $workspaceId --authorize-delete $sourceId
```

`run` 检查维护触发条件并等待已进入的写事务完成，再阻止普通写入、备份当前账目。单实例模式必须先在隔离库还原最终备份并校验；两实例模式保留源库直到新库验证通过。控制器调用提供商 API 创建目标，记录实例 ID，还原并校验账目全部字段、同一参考月预算结果及下一条记录的 ID。随后更新后端数据库连接、检查部署完成，读取已部署 API，再在目标写入一笔带随机备注的受控记录、从该 API 按 ID 读回并清理。普通写入在维护期间返回 HTTP 503。成功的目标实例与核验状态持久化后才恢复正常写入。

**中断与日志。** 默认目录为控制器用户目录下的 `.little-bear-maintenance`，也可指定源库和后端之外的持久目录 `MAINTENANCE_STATE_DIR`。`state.json` 记录活动实例和当前阶段，`monitor.jsonl` 记录只读检查，`<运行ID>.jsonl` 记录本次演练，`backups/<运行ID>.json` 保存源标识、参考时间和当前账目。失败时先查看记录的阶段、最后事件和资源状态；保留备份、状态目录、预算设置与试恢复库。修正原因后，用**原运行 ID 和同一组资源 ID**再次执行上面的最后一条命令。创建结果不明时先核对该运行标记对应的资源，不能用新 ID 重试创建。源实例在取得最终备份前就已失联时，需另行授权从已有的已验证备份恢复，说明该备份之后可能遗漏的变更。

**课程证据。** 完成实际云演练后，选取同一运行 ID 的脱敏记录，将定时状态检查、备份时间及摘要、目标 ID、恢复验证、后端部署 ID、受控读写和最终活动 ID 一起链接在本节。原始备份包含账目，私有连接、令牌、网络白名单及状态目录不应整体提交。已在本机独立 SQLite 和 PostgreSQL 16.16 验证写锁、恢复、受控写入及主键生成；真实云演练结果待补。[维护流程](maintenance/workflow.py) · [数据库与后端核验测试](tests/test_write_guard.py)

## 7. 数据、工具与扩展

账目来自用户手工录入，演示和测试均使用合成记录。三笔 Food 示例由本次课程核查编写，输入及其预期结果见第 3 节。项目采用 FastAPI、Streamlit、SQLAlchemy、Pydantic 与 HTTPX，云维护调用 Render 的服务和 Postgres 接口。[Render API 参考](https://api-docs.render.com/reference) · [项目依赖](requirements.txt)

此次使用 OpenAI Codex 对照课程要求审查代码，辅助修订金额和月份逻辑，编写维护流程、测试、示例与文档。核验包括真实 API 和隔离数据库的读写比对、Streamlit 表单经本机 HTTP 新增消费、对照 GitHub Actions 故障与修复记录，以及在独立 PostgreSQL 中验证并发写锁、恢复与持久化。更早开发阶段的 AI 工具、外部代码和例子的使用情况由作者按实际经历补记。当前没有申报多线程、爬虫、邮件通知或 Docker 四项可选加分功能。

## 8. 提交与 15 分钟答辩

答辩通常在第 13 周，另有考试期初的线上场次。以个人确认的场次为准，**最迟在答辩前 3 天**将 GitHub、Streamlit 和 Render 后端链接发送至 **kovacs.adam.eke@gmail.com**。从提交到答辩须保持仓库和应用可访问，私有仓库须在同一期限内授权教师访问；已计划的维护提前通知教师。演示前提前打开两个云应用。

| 建议时长 | 演示与讲解 |
| --- | --- |
| 0–4 分钟 | 从 Streamlit 添加一笔合成消费，打开分类列表与预算图，说明前端 → `POST /expenses` → `BudgetAdvisorService` → SQLAlchemy → API 响应的一次完整数据流。 |
| 4–8 分钟 | 打开 `create_expense`、`evaluate_budget_status` 和 `BudgetAdvisorService`，分别说明流程、纯函数、类的作用；选“为什么按 UTC 自然月结算并保留历史账目”说明设计取舍。 |
| 8–11 分钟 | 展示已提交版本的 pytest、一次 CI 故障与对应修复，再展示同一运行 ID 下实际取得的云替换证据；尚未完成的项目按实际进度说明。 |
| 11–15 分钟 | 现场先预测边界行为，再用独立演示库或测试验证。例如预算 1500 时比较累计 1500 与 1500.01 的状态及超额，指出相应判断代码。 |

### 英文提交邮件草稿

```text
To: kovacs.adam.eke@gmail.com
Subject: Python project submission – Little Bear Bookkeeping – [Student name]

Dear Ádám Kovács,

I am submitting Little Bear Bookkeeping for the Multi-Paradigm Programming Languages practical course. The application records expenses and displays monthly spending against category budgets.

GitHub repository: https://github.com/Sunshine20051224/Little-Bear-Bookkeeping
Streamlit application: https://little-bear-bookkeeping-2ppuqrbguqmlayds7pcqkn.streamlit.app/
Render backend (interactive API documentation): https://little-bear-bookkeeping.onrender.com/docs

My presentation is scheduled for [date, time, and time zone].

Kind regards,
[Student name]
```

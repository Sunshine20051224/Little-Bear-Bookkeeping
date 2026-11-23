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

在项目根目录执行 `python -m pytest`（需使用项目虚拟环境）。[测试目录](tests/) 包含参数化预算规则测试、维护判断及 API 与隔离 SQLite 的集成测试。CI 工作流在 push 和 pull request 时安装固定依赖并运行同一命令。以下记录来自已推送的提交。

2026-11-23 本地工作区在 Python 3.11 上运行 34 项测试，全部通过，另有 1 条来自 Starlette/AnyIO 的弃用提示。SQLite 恢复测试覆盖原账目字段和时间保留、主键生成、失败后重试及跨月归属；云实例替换另行验收。

在独立的 `debug/ci-fault-demo` 分支，提交 `6132f4f` 将超支判断从 `usage_ratio > 1.0` 改为 `> 10.0`，使已有用例中累计 1010、预算 1000 的消费被判为 WARNING。提交 `970a536` 恢复原阈值，测试转绿。[故障运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45039678262) · [修复后运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45043678181) · [主线 6edba96 的运行](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45132165957)

## 6. 云数据库维护进度

[maintenance/controller.py](maintenance/controller.py) 包含异步 HTTP 状态查询、维护触发判断及 `expenses` 表的 JSON 导出和恢复方法。备份记录源实例标识、创建时间和消费数据，默认目录为执行控制器主机上的 `backups/`。本地方法使用 Python JSON 和 SQLAlchemy，未调用外部备份工具。启动脚本仅在配置了状态 URL 与 API key 时执行一次只读检查；默认跳过。

课程所需的独立周期调度、针对指定资源的授权、后端停写与等待在途事务、持久化运行状态、通过云 API 替换实例及切换后的读写核验仍待完成。当前依赖清单覆盖 SQLite；若云端选 PostgreSQL，须增加并锁定匹配的驱动、在该数据库上实测恢复。完整替换试验结束后，在本节链接同一次运行的脱敏日志及验证结果。

## 7. 数据来源与 AI 使用

本仓库的三笔演示数据为这次课程核查新编的合成数据。此次使用 OpenAI Codex 对照作业要求检查已有实现，协助修订月份与金额规则、备份恢复，补充示例、测试及使用说明。核验方式包括对照原代码、用独立 SQLite 比较接口响应与落库值、用 Streamlit 表单经本地 HTTP 写入合成账目，以及核对 GitHub Actions 的运行记录。项目作者提交前还应按实际情况补记早期使用过的工具、复用代码或参考示例的来源。

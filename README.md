# Little-Bear-Bookkeeping（小熊记账）

基于多范式编程（Multi-Paradigm Programming）构建的个人记账与预算预警应用，涵盖数据持久化、纯函数业务规则与汇总计算、异步维护与备份检查以及前后端交互界面。

- **GitHub 仓库**：`[https://github.com/Sunshine20051224/Little-Bear-Bookkeeping](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping)`
- **前端地址（Streamlit Cloud）**：`[部署完成后填入 Streamlit Cloud 公开访问链接，暂可留空或写 TBD]`
- **后端 API 地址（Render）**：`[部署完成后填入 Render 后端服务链接，暂可留空或写 TBD]`

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
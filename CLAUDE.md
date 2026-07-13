# 管理端后端（info-admin-backend）— Claude Code 规则

> 进入本目录时自动叠加，补充根目录 CLAUDE.md 的全局规则。本文件只约束局部编码；若与代码/OpenAPI、父 App 根 `README.md` 或 k8s v5 权威文档冲突，以后者为准。

## 技术栈

- Python ≥ 3.12 + FastAPI + async SQLAlchemy + Alembic
- pydantic-settings 管理配置
- Redis（async）存储 session
- DDD 分层：`interfaces` / `application` / `domain` / `infrastructure`

## 关键约定

**兼容优先**：保持现有路由前缀与响应结构；变更需评估联调影响。

**配置管理**：所有凭据通过 `pydantic-settings` 从环境变量读取，禁止写死在代码里。

**错误处理**：统一通过 `AppException` 体系抛出，不吞异常；日志包含关键上下文。

**依赖控制**：不随意升级 FastAPI / SQLAlchemy 等核心库，升级需单独任务。

**数据库迁移**：变更模型后必须生成 Alembic migration，不手动改表结构。

**可测试性**：新增接口至少提供 1 条冒烟验证（curl 示例 + 预期响应）。

## 目录结构速查

```
app/
├── core/config.py                    # 全局配置（pydantic-settings）
├── app/main.py                       # FastAPI 入口，lifespan 初始化
├── app/interfaces/
│   ├── endpoints/                    # 路由
│   ├── schemas/                      # 请求/响应 schema
│   └── middleware/                   # 依赖注入（get_current_user 等）
├── app/application/services/         # 业务逻辑
├── app/domain/                       # 领域对象（如有）
└── app/infrastructure/
    ├── storage/postgres.py           # 数据库连接
    ├── storage/redis.py              # Redis 连接
    └── models/                       # SQLAlchemy 模型
```

## 开始一个接口前

1. 读 `app/interfaces/endpoints/` 相关文件，确认现有路由和响应结构
2. 读 `app/infrastructure/models/` 确认数据字段
3. 以路由/OpenAPI/schema/tests 为接口真相；跨仓契约只更新 k8s v5 contracts 和 provider/consumer tests，不创建按 AI 工具命名的契约副本

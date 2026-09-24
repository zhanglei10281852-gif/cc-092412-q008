# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告与部门：公告草稿、送审、通过、驳回、定时发布、撤回、归档和更正版本化，置顶、分类检索、部门信息及关联业务查看。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执；公告定时发布由单线程调度器在应用启动时自动扫描，进程重启后仍会准确补发，且每条公告版本最多发布一次。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告编审发全流程（驳回、定时发布与重启补发、撤回并发、更正版本化）、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         居民、事务、公告（公开与管理两组）、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计、公告编审发、定时调度和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 公告编审发与版本管理

公告不再直接写入公开列表，而是按「草稿 → 送审 →（通过 / 驳回）→ 发布 → 撤回 → 归档」流转：

- 拟稿（`announcements.write`）与审阅（`announcements.review`）必须是不同账号，作者本人审阅自己的公告会被拒绝并写入 `denied` 审计记录；撤回和归档需要 `announcements.publish`。内置 `clerk`（拟稿）与 `reviewer`（审阅/撤回/归档）两个角色。
- 已发布内容的修改必须走「更正」：旧版本保留为历史版本，更正稿重新送审；更正通过前公众继续读到旧的生效版本。
- 公开列表与阅读接口一律以**生效版本**为准（标题、正文、分类、置顶顺序）。旧链接可加 `?version_no=N` 阅读当时版本，响应会标明 `has_later_correction` 与更正提示；撤回后的旧链接仍可访问，并返回撤回原因。
- 定时发布：送审时可指定带时区的发布时间，审阅通过后进入待发布；到点由后台调度器发布。任务持久化在 SQLite 中，进程重启后启动即补发，配合状态条件更新保证只发布一次；驳回会取消计划，迟到或重复回调幂等确认成功。
- 撤回与定时发布并发时结果确定：先撤回则发布回调不会复活公告，先发布则撤回作用于新生效版本。

接口分两组：

| 接口 | 说明 |
| --- | --- |
| `GET /announcements` | 公众列表，只含已发布公告的生效版本，按置顶与发布时间排序 |
| `GET /announcements/{id}` | 公众阅读，默认返回生效版本，`?version_no=N` 返回历史版本 |
| `POST /api/announcements` | 创建草稿 |
| `PUT /api/announcements/{id}/revise` | 修订草稿/驳回稿（产生新版本） |
| `POST /api/announcements/{id}/submit` | 送审，可带 `scheduled_for` 定时发布 |
| `POST /api/announcements/{id}/review` | 审阅通过或驳回（驳回需填写意见） |
| `POST /api/announcements/{id}/correct` | 对已发布公告发起更正 |
| `POST /api/announcements/{id}/withdraw` | 撤回（必须填写原因） |
| `POST /api/announcements/{id}/archive` | 归档 |
| `GET /api/announcements/{id}/events` | 流转记录 |

后台调度器随应用启动（`TOWNSHIP_SCHEDULER_ENABLED=0` 可关闭）；进程长期停机后也可手动补发：

```bash
python -m app.cli publish-announcements
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

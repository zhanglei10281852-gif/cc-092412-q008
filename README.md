# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告全生命周期：草稿、送审、审阅（撰写人与审阅人职责分离）、立即或定时发布、撤回、更正新版本与归档；公众接口只按生效版本阅读，旧版本链接永久保留当时内容。定时发布任务持久化在 SQLite 中，进程重启后自动补发且只发布一次。
- 公告与部门：公告置顶、分类检索、部门信息及关联业务查看。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

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

## 公告生命周期

公告不再直接写入公开列表，必须经过完整流转（管理接口前缀 `/api/announcements`，需登录）：

1. `POST /api/announcements`：有 `announcements.write` 权限的账号创建草稿。
2. `PATCH /api/announcements/{id}`：草稿或被驳回版本可继续修改。
3. `POST /api/announcements/{id}/submit`：送审，可声明 `immediate`（审阅通过即发布）或 `scheduled`（指定未来的 ISO 8601 时间）；过期时间会被拒绝。
4. `POST /api/announcements/{id}/review`：有 `announcements.review` 权限的账号通过或驳回，**撰写人与审阅人不能是同一账号**（即使同一账号同时拥有两种权限也会被拒绝）。
5. 已公开内容不能覆盖修改：`POST /api/announcements/{id}/corrections` 产生新版本，重新走送审与审阅；生效前公众仍读旧版本。
6. `POST /api/announcements/{id}/withdraw`：撤回已发布或已批准待发的公告，必须填写撤回原因；在途送审版本会被打回草稿、定时任务会被取消。
7. `POST /api/announcements/{id}/archive`：草稿或已撤回公告可归档。
8. `POST /api/announcements/{id}/reschedule`：调整已批准、尚未到期的定时计划（旧任务取消、新任务入队）。

公众阅读接口无需登录，且一律以**生效版本**为准：

- `GET /announcements`：只列已发布公告，置顶顺序取生效版本。
- `GET /announcements/{id}`：返回当前生效版本；撤回后仍返回当时版本快照并标明撤回原因。
- `GET /announcements/{id}/versions/{version_no}`：旧链接永久返回当时发布的内容，并标明 `superseded_by` 等后续更正信息。

定时发布由后台轮询线程执行，轮询间隔可用 `TOWNSHIP_ANNOUNCEMENT_POLL_SECONDS`（默认 2 秒）调整，`TOWNSHIP_SCHEDULER_ENABLED=0` 可关闭。任务持久化在 `background_jobs` 表中，进程重启时会先补发停机期间到期的任务；发布由条件更新保证只生效一次，重复回调幂等。也可用 `POST /api/announcements/run-due`（需 `jobs.run` 权限）手动触发到期扫描。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告草稿到归档全生命周期（职责分离、版本更正、定时发布重启补发与单次语义、撤回竞争）、后台任务去重与领取，以及数据库时间格式。

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
  routers/         居民、事务、公告、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

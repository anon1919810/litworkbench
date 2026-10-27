# 页面与证据工作区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking。执行方式已由用户指定：OpenCode 实现，Codex 审查与独立验收。

**Goal:** 在现有文献应用中交付可保存、关联来源、追踪历史的块式笔记工作区，作为云端与 Codex 共用业务基础。

**Architecture:** 新增 notes 领域模块，在既有 Workspace / SQLite 与增量迁移上存储页面快照及引用，使用同一个修订与幂等边界。网页路由只做请求适配，React 使用 BlockNote 基础编辑器与应用自有块契约。现有文献、校订与摘录保持权威。

**Tech Stack:** Python 3.12 / FastAPI / SQLite；React / TypeScript / Vite；BlockNote 基础包（精确兼容版本在安装时锁定）。

**Spec:** `D:/Codex/litworkbench/docs/superpowers/specs/2026-10-26-cloud-workspace-codex-design.md`。

## Global Constraints

- 用户已明确开始实施，无须再增加实施方式选择或逐任务确认；本轮不 commit、push、reset、clean 或操作用户真实资料库。
- Windows/PowerShell；测试使用项目 `.venv/Scripts/python.exe`，依赖由 `C:/Users/杨睿/.local/bin/uv.exe` 管理；前端使用 `npm.cmd`。
- 单个个人工作区；页面正文最大 1 MiB、最多 2000 个块、嵌套最多 8 层。
- 网页自动保存约 800ms，同页最多一个在途写入，修订冲突保留当前草稿。
- 页面固定属性：标题、图标、标签、未开始/进行中/已完成、关联文献、排序与收藏；归档为独立状态。
- 页面 JSON 权威、稳定块 ID、引用索引与历史及幂等在同一短写事务内提交；禁止 INSERT OR REPLACE。
- 图片限 PNG/JPEG/WebP/GIF，单个最大 20 MiB；上传与访问经过原有会话与 CSRF 边界。
- 本阶段完成设计第 4–6 节及第 5 节内容服务。不实现 cloud 身份、远程 MCP、生产迁移或内置模型聊天。
- 不重新设计已有 Reader、OCR 流程、证据核验或全文文献索引；现有增量迁移不能更改历史。
- 不擅自升级现有 React、TypeScript 与 Vite；新增编辑器基础依赖须锁定版本并保留许可证声明，不使用 XL 包。

## Review Focus

1. 两个标签页或以后 MCP 交错写入：同修订只能一项提交；冲突不能丢草稿或部分更新引用。
2. 同一请求在响应丢失后重试：返回原结果，不重复建页或多记历史；换参数重用 ID 要失败。
3. 页面树和块树中的环、重复 ID、过深/超大正文与错误引用：拒绝且数据库不留半条成功记录。
4. 中文输入、emoji 字符、选择粘贴、切换页面时慢保存：不把旧页响应写到新页，不截断代码点。
5. 引用已有待核/过期证据及归档页面：状态真实，引用文字来自快照，改名或移动不改变目标。

## Task 1: 页面领域、迁移与事务契约

**Files:**
- Create `src/litworkbench/notes.py`：领域与事务入口。
- Create `src/litworkbench/note_blocks.py`：纯块文档校验、文本/引用抽取和块操作。
- Modify `src/litworkbench/migrations.py`：追加 notes 相关表，不动旧版本。
- Create `tests/test_notes.py`、`tests/test_note_blocks.py`。

**Interfaces:**
- `create_page(workspace: Workspace, *, title: str, parent_id: str | None, body: dict | None, properties: dict | None, request_id: str, actor: str = "owner", origin: str = "web") -> dict`。
- `get_page(workspace: Workspace, page_id: str) -> dict`。
- `list_pages(workspace: Workspace, *, query: str = "", include_archived: bool = False, page: int = 1, page_size: int = 30) -> dict`。
- `update_page(workspace: Workspace, page_id: str, *, expected_revision: int, request_id: str, changes: dict, actor: str = "owner", origin: str = "web") -> dict`：属性/正文/父页排序通过同一入口。
- `get_page_history(...)`、`restore_page(... expected_revision, request_id, revision)`、`get_backlinks(...)`、`edit_page_blocks(... expected_revision, request_id, operations)`。
- 页面响应至少 `id,parent_id,title,body,schema_version,revision,tags,status,archived,effective_archived,position,favorite,document_ids,created_at,updated_at`；`status` 取 `not_started|in_progress|done`，显示由前端中文化。`body={schema_version:1,blocks:[...]}`。
- 块 ID 允许稳定非空字符串；块结构以 BlockNote 基础块为兼容目标，最终受限字段在契约文档写清楚。JSON 不能存编辑器实例或任意 HTML。
- 领域错误使用既有 LitError 子类；revision 冲突与 request_id 重用错误要有稳定码，网页映射 HTTP 409。

- [ ] 先写测试：`test_create_and_reopen_page`、`test_same_revision_conflict_keeps_history`、`test_replayed_request_is_idempotent_before_revision_check`、`test_reused_request_id_with_changed_payload_rejected`、`test_failed_mutation_does_not_consume_request_id`、`test_moves_cannot_create_cycle`、`test_ancestor_archive_preserves_child_own_archive`、`test_evidence_reference_uses_snapshot_and_review_status`、`test_old_evidence_survives_notes_migration`。
- [ ] 用 `.venv/Scripts/python.exe -m pytest tests/test_notes.py tests/test_note_blocks.py -q` 观察因新功能缺失而失败，记录摘要。
- [ ] 实现纯校验与增量迁移，再实现上述入口。幂等摘要含业务请求但不包含可变服务器时间；先查已成功请求，再检查当前修订。页、引用、历史及成功请求记录在一个 BEGIN IMMEDIATE 事务内提交。
- [ ] 源证据/文献关系校验使用数据库当前记录；正文自定义引用块保存 target ID 和原文快照，不能信任客户端伪造出处。
- [ ] 加边界测试：1 MiB、2000 块、8 层、重复块 ID、块环、缺引用、未知类型、Unicode、块操作最多100项且整批原子。
- [ ] 跑上述目标测试及 `tests/test_integrity.py tests/test_evidence.py tests/web/test_migrations.py`。

## Task 2: 网页 API、资产与导出

**Files:**
- Create `src/litworkbench/web/notes.py`、`src/litworkbench/web/note_schemas.py`。
- Create `src/litworkbench/note_assets.py`、`src/litworkbench/note_exports.py`。
- Modify `src/litworkbench/web/app.py` 注册路由；`web/errors.py` 仅在需要新冲突码时最小改动。
- Create `tests/web/test_notes_api.py`、`tests/test_note_exports.py`。
- Create `docs/note-workspace-contract.md`，记录准确请求与响应。

**Interfaces:**
- GET/POST `/api/v1/pages`；GET/PATCH `/pages/{id}`；GET `/pages/{id}/history`、`/backlinks`；POST `/pages/{id}/restore`；POST `/pages/{id}/blocks`。
- 正文保存为 PATCH pages 的 body；`expected_revision`、`request_id` 必填，所有写入口调用 Task1 业务服务。
- POST `/api/v1/note-assets` multipart；GET `/note-assets/{asset_id}`；不暴露本机绝对路径。
- POST `/pages/{id}/exports`，format=`json|markdown`；返回经过认证可下载的包或安全下载标识。资产和链接清单随导出保存，JSON不是页面导入接口。
- `list_pages` 使用现有分页信封；响应包含可直接打开的 `/pages/{id}` 链接。

- [ ] 写并观察 API 测试失败：缺会话/CSRF、非法修订、跨页块 ID、分页与乱码路径、未知接口 JSON 404、文件伪造类型/超大/路径穿越、导出含图片和证据清单。
- [ ] 实现 API 适配、文件类型签名/大小验证、哈希命名与原子文件提交；失败不遗留数据库引用。无永久删除入口。
- [ ] 导出动态内容使用纯文本/受控 Markdown，不把原文 HTML 当作可信输出；导出损失限制写进契约。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/web/test_notes_api.py tests/test_note_exports.py tests/web/test_evidence_api.py tests/web/test_exports_api.py -q`。

## Task 3: React 笔记工作区与真实保存

**Files:**
- Create `web-client/src/notes/types.ts`、`api.ts`、`blocks.ts`：应用自有契约与编辑器适配。
- Create `web-client/src/notes/NotesPage.tsx`、`PageEditor.tsx`、`PageProperties.tsx`、`PageHistory.tsx`、`PageReferences.tsx`。
- Create `web-client/src/notes/usePageEditor.ts`、`navigation.ts`、相关 `.test.ts(x)`。
- Modify `App.tsx`、`state/AppState.tsx`、`components/Sidebar.tsx`、`pages/ProjectsPage.tsx`、必要样式及 package.json/package-lock.json。
- Modify `api/client.ts` 仅在新模块需要现有调用函数的导出时进行小改动。

**Interfaces:**
- 新的 `notes` 导航不移除旧入口；`/pages/{id}` 可刷新/前进/后退并直接打开；文献/原文深链接复用已有 Reader 目标。
- usePageEditor 隔离页面身份、基准 revision、未提交草稿、在途请求与排队编辑；旧页异步响应不能覆盖新页。
- BlockNote只通过适配层接触持久化正文；自定义 page/document/evidence 引用显示真实状态并跳转稳定地址。
- 每800ms无输入自动保存；一次一个请求；失败/409保留编辑器和冲突选择；全局 refresh不能抹掉草稿。

- [ ] 安装前核对 BlockNote 最新兼容基础版本及 peer依赖；锁定同版本基础包，不修改旧栈版本来迁就它。
- [ ] 写失败测试：排队保存使用上次成功的 revision、冲突保留草稿、慢旧页响应被忽略、IME组合输入不提前提交、深链接刷新、归档引用显示、固定属性筛选。
- [ ] 完成页面树/表格、编辑器、属性、引用/回链、历史恢复、归档与恢复、图片上传和导出入口；空态与错误态真实。
- [ ] 运行 `npm.cmd --prefix web-client test`、`npm.cmd --prefix web-client run build`。
- [ ] 既有工作区风格继续使用白底灰侧栏与紧凑排版；不另起视觉重设计，不保留无法操作的假按钮。

## Task 4: 独立验收与操作说明

**Files:**
- Create `scripts/accept_notes.py`：只创建指定的新验收目录与合成文献，预设页面/摘录供真实浏览器验证；拒绝覆盖已有目录。
- Update `docs/web-user-guide.md` 新入口与保存/冲突/导出说明。
- Create `docs/note-workspace-acceptance.md`：测试、浏览器与限制分别记录。

- [ ] 全量非慢后端 `.venv/Scripts/python.exe -m pytest -m "not slow" -q`，前端全量测试与构建。
- [ ] Codex 独立运行上述命令，并读取所有 changed_files；OpenCode 报告不算验收。
- [ ] Codex 用浏览器真实创建页面、中文编辑、保存、关闭/重开、新建子页、来源引用打开原页、改名后回链、历史恢复、两标签修订冲突、导出文件读回和窄屏检查。
- [ ] 浏览器受环境阻碍则保留候选结果并报告未验收项，不宣称页面闭环通过。
- [ ] 接着为阶段 B/C 写独立实施计划并继续用户已授权工作；实际发布需要平台条件与真实认证配置。

## Self-review

已覆盖设计阶段A所有功能；页面导入、通用数据库和云端认证明确在本阶段之外。Task1承载事务/幂等/结构边界，Task2覆盖旧认证与资产，Task3覆盖草稿/IME/导航，Task4覆盖真实用户闭环。接口命名由本计划固定；后续MCP必须复用这些服务。无 Git 提交步骤，避免替用户提交尚未验收的工作。

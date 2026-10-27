# 笔记工作区后端契约（Task 1 / Task 2）

状态：已实现，未提交。范围仅限阶段 A 后端：页面领域、块文档、v10 迁移、网页 API、笔记图片和页面导出。本文不描述也未实现 Task 3/4、前端编辑器、云端/MCP、页面导入或部署。

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `litworkbench/note_blocks.py` | 纯函数：块文档结构校验、文本/引用抽取、原子块操作、大小/深度/数量边界 |
| `litworkbench/notes.py` | 页面领域：页面树、属性、标签/文献关系、引用快照、历史、幂等写入和事务 |
| `litworkbench/note_assets.py` | 笔记图片：真实格式校验、内容哈希存储、受控读取 |
| `litworkbench/note_exports.py` | 页面导出包：JSON / Markdown / ZIP、引用清单、图片资产 |
| `litworkbench/web/note_schemas.py` | 页面 API 请求/响应模型 |
| `litworkbench/web/notes.py` | `/api/v1` 页面、块、图片、导出路由 |
| `litworkbench/migrations.py` | v10 `notes-workspace` 迁移，只增表/索引，不动旧文献、摘录、校订数据 |

旧库升级时保留备份并幂等执行；v10 迁移名固定为 `notes-workspace`。

## 2. 页面对象

页面读取响应（摘要字段）：

```json
{
  "id": "pg_...",
  "parent_id": null,
  "title": "白塔研究",
  "icon": "",
  "body": {"schema_version": 1, "blocks": []},
  "schema_version": 1,
  "revision": 1,
  "tags": ["文物"],
  "status": "not_started",
  "archived": false,
  "effective_archived": false,
  "position": 0,
  "favorite": false,
  "document_ids": [],
  "created_at": "2026-10-27T...",
  "updated_at": "2026-10-27T...",
  "url": "/pages/pg_..."
}
```

- `status`：`not_started` / `in_progress` / `done`。
- `tags`：有序字符串数组，存 `page_tags`。
- `document_ids`：有序文献 ID 数组，只存 `page_documents` 关系；`pages` 表没有 `document_ids` 列。
- `archived`：页面自己的归档标记。
- `effective_archived`：自己或任一祖先归档即为 `true`；恢复祖先不会改写子孙自己的 `archived`。
- `position`：同级排序值。显式插入/移动会让目标准确落位；被挤开的同级页面也推进 revision。
- `url`：前端页面地址；不暴露工作区绝对路径。

## 3. 页面树与修订契约

### 3.1 新建

`create_page` 的稳定参数：

```python
create_page(
    workspace, *,
    title, parent_id, body, properties, request_id,
    actor="owner", origin="web",
) -> dict
```

- 父页面必须存在；不能形成环。
- `properties` 只接受：`icon`、`tags`、`status`、`favorite`、`document_ids`、`position`、`archived`；未知字段拒绝。
- 显式 `position` 会插入到该位置并挤开同级；未提供时追加到同级末尾。
- 被挤开的同级页面在**同一事务**中增加 revision、更新 `updated_at` 并写入 `page_revisions` 快照。
- 新页面 revision 从 1 开始，同时写入 revision 1 快照。

### 3.2 修改

```python
update_page(
    workspace, page_id, *,
    expected_revision, request_id, changes,
    actor="owner", origin="web",
) -> dict
```

`changes` 允许：`title`、`icon`、`tags`、`status`、`favorite`、`document_ids`、`parent_id`、`position`、`archived`、`body`；未知字段拒绝。

- 先做幂等检查，再比较 `expected_revision`。
- 不匹配返回 `REVISION_CONFLICT`（409），当前页面和历史不改变。
- `document_ids` 只在 `page_documents` 增删，绝不拼进 `UPDATE pages` 的列名。
- 任何实际变化都推进主页面 revision、`updated_at` 和 `page_revisions` 快照。
- 排序导致的其他页面位置变化同样推进这些页面的 revision、`updated_at` 和历史；它们与主页面共享本次请求的 `origin`、`actor`、`request_id`，但 `mutation_requests` 只保存主页面结果。
- 无实际变化的保存返回当前结果，不制造假历史。

### 3.3 幂等

`mutation_requests(owner, request_id)` 是幂等键：

1. 事务开始后先查成功记录。
2. 相同 `operation` + 相同规范化载荷摘要：原样返回保存的结果，即使当前 revision 已前进。
3. 相同 `request_id` 但操作或载荷不同：`REQUEST_ID_REUSE`（409）。
4. 校验、引用、正文或后续写入失败：整个事务回滚，不消耗 request_id，可用于改正后重试。

### 3.4 历史与恢复

- `get_page_history` 按 revision 倒序返回不可变快照。
- `restore_page` 恢复标题、正文、属性、标签、文献关系和归档标记，产生**新 revision**，不删除旧历史。
- 恢复同样受 `expected_revision` 与 `request_id` 约束。
- 删除接口不存在；归档是唯一下线方式。

## 4. 块文档契约

正确的 `body`：

```json
{"schema_version": 1, "blocks": [块, ...]}
```

每个块：

```json
{
  "id": "稳定块 ID",
  "type": "paragraph",
  "props": {},
  "content": [行内内容],
  "children": []
}
```

- 块 ID 必须非空且全页唯一；不支持重复 ID、环、未知块字段、未知块属性。
- 正文最多 2000 块、最多 8 层、规范化 JSON 最多 1 MiB。
- 一次块操作最多 100 个。
- 未知块类型、未知 props、未知行内字段一律 422，不静默删除。
- 服务端会拒绝 `javascript:`、`data:`、`file:` 等 URL；只允许带主机名的 `http` / `https`。

### 4.1 块类型与 props

| type | 说明 | 必要 props | 其他允许 props |
| --- | --- | --- | --- |
| `paragraph` | 段落 | — | `textColor`、`backgroundColor`、`textAlignment` |
| `heading` | 标题 | — | 1–6 的 `level`，以及通用颜色/对齐 |
| `bulletListItem` | 无序列表 | — | 通用颜色/对齐 |
| `numberedListItem` | 有序列表 | — | 通用颜色/对齐 |
| `checkListItem` | 勾选列表 | — | `checked` 布尔，以及通用颜色/对齐 |
| `quote` | 引用 | — | 通用颜色/对齐 |
| `divider` | 分隔线 | — | 无 |
| `link` | 网页链接块 | `url`（http/https） | 无 |
| `image` | 笔记图片 | `assetId` | `caption`；其余 `sha256`、`media_type`、`byte_size`、`src` 由服务端填写 |
| `pageRef` | 页面引用 | `pageId` | `title`、`icon`、`archived`、`effective_archived`、`missing` 由服务端维护 |
| `documentRef` | 文献引用 | `documentId` | `title`、`filename`、`author`、`year`、`missing` 由服务端维护 |
| `evidenceRef` | 摘录引用 | `evidenceId` | `quote`、`review_status`、`doc_id`、`unit_id`、`version`、`locator`、`filename`、`title`、`file_page`、`printed_page`、`printed_page_status`、`missing` |

`divider`、`image`、`pageRef`、`documentRef`、`evidenceRef` 不允许行内 `content`。

### 4.2 行内内容

普通文字：

```json
{"type": "text", "text": "白塔位于城北。", "styles": {}}
```

`styles` 只接受 `bold`、`italic`、`underline`、`strike`、`code`（布尔）和 `textColor`、`backgroundColor`（字符串）。

BlockNote 网页链接使用受限 inline 形状：

```json
{
  "type": "link",
  "href": "https://example.com/a?b=1",
  "content": [
    {"type": "text", "text": "白塔资料", "styles": {}}
  ]
}
```

- 保留原 link 结构，不降级成纯文本；`extract_text` 会把链接文字计入页面搜索。
- Markdown 导出生成 `[白塔资料](https://example.com/a?b=1)`。
- 只接受 `http` / `https`，必须含主机名，拒绝空白/控制字符、超长 URL、危险 scheme。
- link 的 `content` 必须是非空 text 数组；不允许嵌套 link 或未知字段。

### 4.3 引用快照

引用块只接受稳定目标 ID。保存时服务端从当前数据库覆盖展示快照：

- `pageRef`：当前标题、图标、归档与有效归档状态。
- `documentRef`：当前文献标题、文件名、作者、年份。
- `evidenceRef`：保存的摘录原文、当前核验状态、当前出处/定位信息。
- `image`：当前资产哈希、媒体类型、大小和认证下载地址。

读取时动态补充当前状态。摘录原文是保存时的快照；核验状态和出处定位显示当前值。目标缺失时读取标 `missing: true`，但保存时必须存在。

`page_links` 是当前正文的派生索引，每次正文保存先删旧行再插入；页面引用支撑 backlinks。`page_documents`、`page_tags` 按请求顺序保存。

### 4.4 块操作

```python
apply_operations(body, operations) -> dict
```

支持且只支持：

- `{"op": "insert", "parent_block_id": str|null, "index": int, "block": 块}`
- `{"op": "replace", "block_id": str, "block": 块}`；替换块 ID 必须不变
- `{"op": "move", "block_id": str, "parent_block_id": str|null, "index": int}`

操作按顺序原子应用；任一失败则整次失败，不修改输入。`index` 按移除源块后的目标列表解释。跨页移动、移动到自身/后代、重复 ID、未知块 ID 都拒绝。

## 5. 事务与持久化

所有页面写入口使用 `BEGIN IMMEDIATE`，并在**一个事务**中提交：

1. 幂等检查；
2. revision 检查；
3. 页面行、标签、文献关系、正文；
4. 引用索引 `page_links`；
5. 主页面及被挤开的同级页面的 `page_revisions`；
6. 主页面成功结果 `mutation_requests`。

不使用 `INSERT OR REPLACE`。失败事务整体回滚，不会留下“成功但缺历史”或“排序改了但 revision 没变”的中间状态。

v10 表：`pages`、`page_tags`、`page_documents`、`page_revisions`、`page_links`、`note_assets`、`mutation_requests`、`page_exports`。`pages.search_text` 是从标题与正文抽出的派生搜索文本，与正文在同一事务维护。

## 6. 网页 API

所有路由前缀 `/api/v1`。读接口要求有效会话；写接口还需要 CSRF 令牌。错误响应统一：

```json
{"error": {"code": "REVISION_CONFLICT", "message": "..."}}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/pages` | 列表/搜索；参数 `q` 或 `query`、`page`、`page_size`、`include_archived` |
| POST | `/pages` | 新建页面，201 |
| GET | `/pages/{page_id}` | 读取页面 |
| PATCH | `/pages/{page_id}` | 修改页面 |
| GET | `/pages/{page_id}/history` | 修订历史 |
| GET | `/pages/{page_id}/backlinks` | 页面反向引用 |
| POST | `/pages/{page_id}/restore` | 恢复历史修订 |
| POST | `/pages/{page_id}/blocks` | 原子块操作 |
| POST | `/pages/{page_id}/exports` | 创建导出，201 |
| GET | `/page-exports/{export_id}` | 导出元数据 |
| GET | `/page-exports/{export_id}/download` | 下载 ZIP 包 |
| POST | `/note-assets` | 上传笔记图片，201 |
| GET | `/note-assets/{asset_id}` | 认证读取图片 |

`POST /pages` 请求：

```json
{
  "title": "白塔研究",
  "parent_id": null,
  "body": {"schema_version": 1, "blocks": []},
  "properties": {"tags": ["文物"], "status": "in_progress"},
  "request_id": "前端生成的唯一键"
}
```

`PATCH /pages/{id}` 请求：

```json
{
  "expected_revision": 3,
  "request_id": "前端生成的唯一键",
  "title": "新标题",
  "document_ids": ["doc_..."],
  "body": {"schema_version": 1, "blocks": []}
}
```

`POST /pages/{id}/blocks`：

```json
{
  "expected_revision": 3,
  "request_id": "前端生成的唯一键",
  "operations": [
    {"op": "insert", "parent_block_id": "b1", "index": 0,
     "block": {"id": "b2", "type": "paragraph", "props": {},
               "content": [{"type": "text", "text": "新段落", "styles": {}}],
               "children": []}}
  ]
}
```

`POST /pages/{id}/restore`：`{"expected_revision": 3, "request_id": "...", "revision": 2}`。

导出请求：`{"format": "json" | "markdown" | "zip"}`。没有页面导入接口。

## 7. 错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | `SESSION_REQUIRED` | 未认证访问页面/图片/导出 |
| 403 | `FORBIDDEN` / CSRF 错误 | 写请求缺少或错误 CSRF |
| 404 | `NOT_FOUND` | 页面、历史修订、文献、摘录、资产、导出或文件不存在 |
| 409 | `REVISION_CONFLICT` | `expected_revision` 过期 |
| 409 | `REQUEST_ID_REUSE` | 相同 request_id 换了操作或载荷 |
| 413 | `FILE_TOO_LARGE` | 图片超过 20 MiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 图片内容不是真实 PNG/JPEG/WebP/GIF |
| 422 | `NOTE_BLOCK_INVALID` | 块结构/数量/深度/大小/URL 不合法 |
| 422 | `NOTE_REFERENCE_INVALID` | 引用目标不存在 |
| 422 | `NOTE_ASSET_INVALID` | 空图片或其他资产校验失败 |
| 422 | `VALIDATION_ERROR` | 请求体字段或分页参数不合法 |

因修订冲突前端必须保留草稿、拉取最新页面后重试；不能静默覆盖。

## 8. 笔记图片

- 只接受真实内容为 PNG / JPEG / WebP / GIF 的图片；不信任文件名或上传 MIME。
- 单文件最大 20 MiB。
- 按 SHA-256 内容哈希保存为不可变对象，数据库只存相对路径和归属。
- 相同内容重复上传复用同一资产记录。
- 下载需认证；响应只给资产 ID 与 `/api/v1/note-assets/{id}`，不暴露本机绝对路径。
- 图片块必须引用当前存在的 `assetId`；缺失图片保存时拒绝。

## 9. 页面导出

所有格式都返回 ZIP 文件；`json` / `markdown` / `zip` 是包内视图，不是页面导入格式。

- `page.json`：`kind = note_page_export`，含页面快照、`links.page_import = not_supported` 和限制。
- `page.md`：受控 Markdown；正文按纯文本转义，不执行 HTML；inline link 保留为安全 Markdown 链接；笔记图片使用 `assets/<sha256>.<ext>` 相对路径。
- `references.json`：稳定目标 ID、块 ID、类型、快照和 API/页面地址。
- `manifest.json`：页面 ID、revision、格式、引用数、资产数、资产清单、限制。
- `assets/`：导出时实际引用的笔记图片字节。
- `zip` 同时包含 `page.json` 与 `page.md`。

限制：不做页面导入；Markdown 不承诺无损往返；文献原件仍由资料库管理；导出包不包含工作区绝对路径、会话、令牌或密钥。

## 10. 兼容性与验收

- v10 迁移可在 v9 旧库上执行，保留旧 evidence/quote、units、revisions 数据与备份。
- 旧后端完整非 slow 测试、旧前端测试和构建必须保持通过。
- 关键验收命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_notes.py tests/test_note_blocks.py tests/web/test_notes_api.py tests/test_note_exports.py tests/test_integrity.py tests/test_evidence.py tests/web/test_migrations.py tests/web/test_evidence_api.py tests/web/test_exports_api.py -q -p no:cacheprovider
.venv/Scripts/python.exe -m pytest -m "not slow" -q -p no:cacheprovider
```

测试缓存目录必须放在源码目录之外，并使用 `-p no:cacheprovider`；不要修改旧 quality 测试的源码路径保护。

# 文献工作台网页版本 Implementation Plan

> **For agentic workers:** 用户执行方式优先：Codex 规划和独立验收，OpenCode 实施有明确边界的任务；不自动提交或推送。用户已批准设计并授权正式开发第一里程碑：真实 PDF 批量导入、OCR、检索与摘录。

**Goal:** 用网页完成批量文献识别、中文跨书检索、原页核对、证据整理和带出处的研究输出。

**Architecture:** React 前端通过同源 FastAPI 调用现有 Python 文献核心。SQLite 保存工作区、持久化任务和字符位置全文索引；受控子进程承担 OCR 与页面渲染，浏览器仅按需加载。启动器启动本地服务并打开浏览器。

**Tech Stack:** Python >=3.11（验收优先 3.12）、现有 pypdfium2/RapidOCR/python-docx/httpx、FastAPI/Uvicorn、SQLite FTS5；React/TypeScript/Vite；pytest 与 Playwright。实施时验证运行时要求并锁定依赖，终端用户不依赖 Node。

**Spec:** `../specs/2026-10-15-web-workbench-design.md`。

**执行状态（2026-10-15）：** 第一里程碑任务 1–5 与本机启动入口已完成独立验收：当时后端 255 passed / 2 skipped，前端 61 项测试与生产构建通过；真实浏览器完成 6 份 PDF、91 页的导入/OCR/检索/原页/摘录闭环。历史记录见 [网页验收](../../web-acceptance.md)。

**Task 6 本轮已验收：** 集合、批量整理及四格式资料下载完成。最终后端 322 passed / 2 skipped，前端 104 项及生产构建通过；真实浏览器下载、文件解析、Word 渲染、预览变动和重启保留均已核对。详见 [Task 6 验收](../../web-task6-acceptance.md) 和 [细化计划](2026-10-15-excerpt-collections-exports.md)。浏览器验证使用 CUA，未交付自动化 Playwright E2E 套件。下列原实施步骤保留作计划记录，未据后验结果伪造逐步先红后绿执行记录。任务 7、任务 8 的完整发布及 1000 页性能仍待实施与验收。

## Global Constraints

2026-10-16 下一阶段规划补充：优先进行 [OCR 质量基线与候选评估](2026-10-16-ocr-quality-evaluation.md)，再根据实测决定接入改进；Task 7 的网页 API 与 Agent 继续沿用原设计。1000 页真实长书尚缺，其验收不以当前 91 页结果替代。

- 第一里程碑执行任务 1–5，并补上启动入口和真实数据验收；本轮继续完成任务 6。任务 7 的 AI 研究、任务 8 的完整发布和 1000 页性能验收后续推进；不得用草图数据冒充正式功能。
- 默认仅监听 `127.0.0.1`，同源接口 `/api/v1`，写操作校验本地会话与 Origin。
- 用户已明确验收规模为 3–5 份 PDF 合计 1000 页，不再要求每份 300 页；目前真实节选共 91 页，不能冒充达标。
- 默认 1 个 OCR 工作进程；多个 PDFium 调用不得在不同线程同时执行。
- 默认查询分页 30 条；基础精确匹配只去除空白，Unicode 字符及标点均走索引路径。
- 默认每文件上传上限 1GiB，可调整；流式暂存，未选中的本机路径不由浏览器任意读取。
- 原件不改写；校订保留历史；摘录指向所选版本与准确偏移；失效证据显式提醒。
- 日常 UI 使用中文、文件页序和印刷页码分开；置信度与人工核验状态分开。
- API 密钥默认仅存服务内存，浏览器不写 localStorage，TOML/日志/任务/导出不存密钥；系统凭据保存为可选能力。
- 保留 CLI 公共接口；旧工作区升级先备份，迁移失败不能损坏现有证据。

## Review Focus

1. 同名不同版本、重复文件与目录重叠导入：必须用哈希和稳定 ID，不能修改错书（任务 1、2、4）。
2. OCR/索引半完成、重启和旧版索引：搜索覆盖真实，失效文本不冒充当前命中（任务 2、3、5）。
3. 中文短词、标点、双栏、重复引文与印刷页码：既能检索又能定位到正确原件（任务 3、5）。
4. 旧请求晚返回、页面关闭和后台暂停：前台与实际任务状态一致，不误换书页（任务 4、5）。
5. 网页配置密钥、模型动作越过文献范围、原文恶意 HTML：不泄露凭据、不越界、不执行资料内容（任务 1、5、7）。

## 交付顺序

任务 1 → 任务 2/3 → 任务 4 → 任务 5 是第一里程碑。随后任务 6、7，最后任务 8。任务 2/3 可独立实施但修改 store/migrations 时串行整合；Codex 集中验收。先完成真实数据闭环，再补齐研究辅助能力。

## 文件与接口地图

- `src/litworkbench/web/app.py`：`create_app(workspace_root: Path) -> FastAPI`，依赖与同源服务装配。
- `src/litworkbench/web/schemas.py`：请求/响应模型与中文错误消息映射。
- `src/litworkbench/web/{documents,jobs,search,evidence,research,settings}.py`：按用户功能组织 API，不从路由直接执行长 OCR。
- `src/litworkbench/migrations.py`：工作区结构版本与备份、事务迁移。
- `src/litworkbench/jobs.py`：`JobStore(workspace)`，入队、租约、检查点、暂停与恢复。
- `src/litworkbench/worker.py`：`run_worker(workspace_root: Path, stop_event) -> None`，受控子进程入口。
- `src/litworkbench/search_index.py`：`encode_characters(text: str) -> str`、`index_unit(workspace, unit_id: str) -> None`、`search_indexed(workspace, query: str, *, mode: str='phrase', doc_ids: list[str] | None=None, page: int=1, page_size: int=30) -> dict`、`coverage(workspace, doc_ids=None) -> dict`。
- `src/litworkbench/credentials.py`：`SessionCredentials.set(profile_id: str, secret: str) -> None`、`resolve(profile_id: str) -> str`、`clear(profile_id: str) -> None`；不持久化值。
- `src/litworkbench/launcher.py`：`launch(workspace_root: Path, open_browser: bool=True) -> int`，端口选择、会话建立、打开浏览器与关闭服务。
- `web-client/src/{app,api,features,components,styles}`：应用壳、契约类型、按功能页面与公共阅读组件。
- `tests/web/`：服务和持久化集成；`web-client/e2e/`：浏览器真实闭环。

公共响应约定：`{items, total, page, page_size}`；搜索另含 `coverage` 与索引代次 `index_revision`。错误为 `{error:{code,message,details}}`；原文冲突 HTTP 409；上传入队 HTTP 202；不存在 HTTP 404；不支持文件/无效参数 HTTP 400/422。API 不用 200 包装失败。

### Task 1: 本地服务、工作区契约与迁移

**Files:** Create `web/app.py`、`web/schemas.py`、`web/documents.py`、`migrations.py`；Modify `store.py`、`pyproject.toml`；Test `tests/web/test_app.py`、`test_migrations.py`。

**Interfaces:** 提供 `create_app`；`GET /api/v1/workspace` 返回名称、文献数、版本和覆盖；`GET /api/v1/documents?page=1&page_size=30` 返回列表；`GET /api/v1/documents/{id}` 返回书目与处理摘要；`PATCH` 同址修改书目。文件与输出只按稳定 ID 访问。

- [ ] 写服务测试：中文目录打开、分页与同名文献区分、越界文件路径拒绝、缺失/错误本地会话与 Origin 写入拒绝。
- [ ] 运行 `python -m pytest tests/web/test_app.py tests/web/test_migrations.py -q`，确认尚未实现的契约失败。
- [ ] 实现事务迁移与备份，明确新增 jobs/imports/index/collections 表的所有权；旧版本记录只升级结构，不重写引用内容。失败恢复旧库；重复启动幂等。
- [ ] 实现服务依赖与路由、中文错误响应，静态资源同源；重跑上述测试以及 `python -m pytest tests/test_integrity.py -q`。

**交付验收：**旧库可以从 HTTP 查看和编辑书目；迁移回滚、路径与会话边界有证据。

### Task 2: 持久化批量导入与 OCR 队列

**Files:** Create `jobs.py`、`worker.py`、`web/jobs.py`；Modify `web/documents.py`、`ingest.py`、`ocr.py`；Test `tests/web/test_jobs.py`、`test_imports.py`。

**Interfaces:** `POST /api/v1/imports` 多文件 multipart，接收结束后返回 `202 {job_ids}`；`GET /api/v1/jobs` 和 `GET /jobs/{id}` 返回阶段、状态、已处理/成功/失败/待处理计数与可检索页数；`POST /jobs/{id}/pause|resume|retry-failed` 幂等。`JobStore.enqueue_import(staging_id, filename, ocr_mode)->job_id` 与 `claim_next(owner_id)->job|None` 通过事务租约保证一个执行者。

`POST /api/v1/documents/{id}/reprocess` 接收 `{unit_ids,mode:'force',keep_corrections:true}`，unit_ids 来自用户选定页/范围/整本展开；此专用任务重做 completed 页，不能调用只重试 pending 的旧路径后宣称完成。重做前保留旧结果，提交新版本时保留历史、人工校订及证据关系。

- [ ] 写集成测试：重复导入去重；单页 OCR 报错后其余页可继续；页结果提交前后中断；暂停请求在安全点生效；重启恢复；同任务两次 claim 只有一次成功。
- [ ] 写强制重识别测试：已有长坏文本且 status=completed 的页仍真实执行 OCR；范围外成功页不重做；人工校订保留；原始版证据标需复核，失败后原可用结果仍在。
- [ ] 运行 `python -m pytest tests/web/test_jobs.py tests/web/test_imports.py -q` 先确认失败。
- [ ] 流式暂存与异步哈希、统计页数；先返回任务，之后关联稳定文献 ID。处理模式支持 auto/off/force；已完成页复用，失败页不计作成功。
- [ ] 将逐页检查点接入现有 `import_document/_save_extraction`，每页提交后索引入队。单进程 OCR 默认资源上限；渲染隔离，任务恢复不读取浏览器缓存。
- [ ] 实现无文字待确认与人工空白确认接口；未知页数不生成假百分比。重跑测试并保留现有导入回归。

**交付验收：**关闭网页、服务异常退出与重启后有真实任务状态，成功页不重复识别。

### Task 3: 中文索引、分页与覆盖率

**Files:** Create `search_index.py`、`web/search.py`；Modify `ingest.py`、`evidence.py` 以提交 dirty/reindex 状态；Test `tests/web/test_search_index.py`、`test_coverage.py`。

**Interfaces:** `POST /api/v1/search` 接收 `{query,mode,doc_ids,page,page_size,expected_index_revision?}`，mode 为 phrase/all_terms/any_terms，返回命中 unit_id、doc_id、书名、文件页序、印刷页码、片段与原文偏移/哈希、覆盖和代次。分页代次不符返回 `409 {error:{code:'SEARCH_INDEX_CHANGED',details:{current_revision,coverage}}}`；前端条件不变，重查第一页并提示更新。`GET /api/v1/coverage` 返回 `known_pdf_pages,covered_pdf_pages,pending_pdf_pages,failed_pdf_pages,unknown_documents,indexed_docx,total_docx`，DOCX 不伪造页数。

- [ ] 写测试：`灯`、`灯会`、跨换行短语、`%`、`_`、双引号及组合检索；限定文献范围；大于 30 条命中能翻页取全；同页重复命中不丢定位；空查询明确报错。
- [ ] 写覆盖测试：一页校订后旧索引不对应当前版本；识别成功但尚未索引；人工确认空白；坏文件页数未知；Word 与 PDF 分别计数。
- [ ] 写代次测试：校订进入 dirty 即在同事务增加代次；索引完成再次增加代次；第二页请求旧代次得到 409；结果、计数和覆盖在同一读取事务，不混用不同代次。
- [ ] 运行 `python -m pytest tests/web/test_search_index.py tests/web/test_coverage.py -q` 确认失败。
- [ ] 按 spec 码点 token 与 FTS5 位置短语实现候选检索，回查当前原文获取偏移；分别计算命中页数/文献数，不以 limit 后的数量冒充总数。稳定排序后分页。
- [ ] 索引代次与文字版本提交遵循明确事务边界，dirty 页面显示未覆盖；重建成功后原子换代。测试断点、失败重试及跨代分页提示；重跑上述测试。

**交付验收：**中文短词走索引、有完整分页、覆盖统计可核对、修改后不会继续命中旧字。

### Task 4: 网页应用壳、文献库与识别队列

**Files:** Create `web-client/package.json`、`vite.config.ts`、`src/app/{App,router}.tsx`、`src/api/{client,types}.ts`、`src/styles/tokens.css`、`src/features/library/{LibraryPage,ImportDialog}.tsx`、`src/features/jobs/JobsPage.tsx`；Test `web-client/e2e/library.spec.ts`、`jobs.spec.ts`。

**Interfaces:** 前端只通过 `src/api/client.ts` 调用任务 1/2 契约；任务详情前台每 2 秒轮询，后台标签页每 10 秒，页面卸载取消请求。路由为 `/search`、`/library`、`/jobs`、`/excerpts`、`/projects`、`/settings`。导航只保存本地 UI 偏好，不保存密钥。

- [ ] 写浏览器集成测试：拖入中文文件、导入期间切换页面、重载后任务仍存在、重复文件提示关联已有文献、暂停/恢复与服务状态一致。
- [ ] 配置开发代理与生产同源路径，锁定依赖；执行 `npm --prefix web-client run test:e2e -- library jobs` 确认核心流程失败。
- [ ] 按用户指定的 Notion 样式实现浅灰侧栏、白色主区、面包屑、大标题、数据库视图栏与紧凑表格，取消暖白松绿主题和序号导航。实现导入阶段反馈、任务详情；空、加载、失败、部分完成有具体动作，长文件名和小屏不撑破布局。
- [ ] 接真实 API，移除演示进度；重跑测试与 `npm --prefix web-client run build`。

**交付验收：**用户可用鼠标真实批量入库、查看后台进度与控制任务，无需终端操作。

### Task 5: 搜索、阅读器与摘录闭环

**Files:** Create `web-client/src/features/search/{SearchPage,ResultList}.tsx`、`src/components/reader/{DocumentReader,TextView,CompareView,PageControls}.tsx`、`src/features/excerpts/ExcerptDialog.tsx`、`src/litworkbench/web/evidence.py`；Modify `web/documents.py`、`evidence.py`；Test `web-client/e2e/search-reader.spec.ts`、`tests/web/test_evidence_api.py`。

**Interfaces:** `GET /api/v1/units/{id}` 返回原始/校订文字、当前版本哈希、真实 OCR 框和渲染尺寸；`GET /units/{id}/render` 以受控离散倍率返回当前页图片；`PATCH /units/{id}/correction` 接收 `{text,reason,expected_hash}`；`PATCH /units/{id}/printed-page` 接收候选/已确认页码；`POST /api/v1/evidence` 接收 `{unit_id,quote,start_offset,expected_hash,category,note,collection_id?}`。保存前核对版本和精确偏移。

- [ ] 写测试：单/双字与标点检索、清空结果、过期查询响应、快速换页不闪错书；原件/文字/对照切换；文件页序与印刷页码分别显示；两次相同引文选择后一次时偏移准确；选中文本 HTML 不执行。
- [ ] 写 DOCX 测试：命中后按标题路径和段落/表格单元打开，不调用 PDF 渲染接口；原提取/校订对照可读；原件可下载且无伪造页码。
- [ ] 写证据 API 测试：旧版本提交 409、保留草稿；伪造引文拒绝；校订后已接受证据变需复核；文字 PDF 无坐标时使用页级回退。
- [ ] 运行上述 e2e 与 `python -m pytest tests/web/test_evidence_api.py -q` 确认失败。
- [ ] 实现查询状态、可拖分栏、受控页图加载、OCR 真实坐标映射与页级回退；阅读区只保留当前及前后相邻页。键盘输入页序、缩放、返回结果均保持查询位置。
- [ ] DOCX 阅读器按原顺序加载当前与相邻文本单元，使用文档文字/校订对照/书目与原件视图，不引入格式转换依赖。
- [ ] 实现摘录与校订面板，选择和保存使用同一次出现的准确偏移；重跑上述测试并录制第一里程碑真实流程。

**交付验收：**从全库搜索打开正确原页，核对后保存可追溯证据；刷新后记录仍在。

### Task 6: 摘录集、审核与导出

**Files:** Create `web-client/src/features/excerpts/{ExcerptsPage,ExcerptDetail,ExportDialog}.tsx`、`src/litworkbench/web/exports.py`；Modify `web/evidence.py`、`migrations.py`；Test `tests/web/test_exports_api.py`、`web-client/e2e/excerpts.spec.ts`。

**Interfaces:** `GET /api/v1/evidence` 按文献/分类/状态/摘录集分页；`PATCH /evidence/{id}` 修改备注或审核状态；`GET/POST /api/v1/collections` 管理集合；`POST /api/v1/exports` 接收格式和范围，返回可下载的稳定 export_id；`GET /exports/{id}/download` 返回文件，不接受任意服务器路径。

- [ ] 写测试：批量分类、跨页多条证据归集合、需复核状态与原文修订联动、全量导出不受列表分页截断、CSV 公式保护、报告缺失/被拒/跨任务引用不显示已核验。
- [ ] 运行 `python -m pytest tests/web/test_exports_api.py -q` 和对应 e2e 确认失败。
- [ ] 复用现有证据/报告导出函数，实现集合关系与 UI；明确人工接受摘录和结论确认的区别；下载前展示格式、范围和待核数量。
- [ ] 重跑上述测试及现有 `tests/test_export*.py` 验收。

**交付验收：**网页可整理、审核并下载带完整出处的材料，不依赖手写导出命令。

### Task 7: 研究项目、受限 Agent 与网页模型设置

**Files:** Create `credentials.py`、`web/research.py`、`web/settings.py`、`web-client/src/features/projects/{ProjectsPage,ProjectDetail,ResearchDrawer}.tsx`、`src/features/settings/SettingsPage.tsx`；Modify `agent.py`、`provider.py`、`tasks.py`；Test `tests/web/test_research_scope.py`、`test_credentials.py`、`web-client/e2e/research.spec.ts`。

**Interfaces:** `POST /api/v1/research` 接收 `{question,doc_ids,profile_id,max_steps}` 并返回研究 job_id/task_id；`POST /research/{task_id}/pause|resume`；`GET /research/{task_id}` 返回已提交动作、证据与报告；`PUT /settings/provider` 接收模型配置与可选会话密钥，GET 仅返回 configured 布尔；`DELETE /settings/provider/secret` 清除内存密钥。`ChatProvider(config, api_key_resolver=None)` 与 `run_research(..., provider_factory=None, scope_doc_ids=None)` 为新增可选注入点，CLI 默认行为保留。

- [ ] 写真实本机模拟 HTTP 测试：模型猜到范围外 unit_id 仍不能 read/excerpt；finish 不接受范围外证据；恢复使用原范围快照；HTTP 故障与已提交 finish 恢复；同任务不能两个执行者。
- [ ] 写密钥测试：网页提交后配置、数据库、错误、日志、导出与 GET 无密钥；服务重启缺密钥时保留任务并提示重填；前端刷新不从 localStorage 恢复密钥。
- [ ] 先运行相关 pytest/e2e 确认失败，再实现内存凭据、项目范围约束、队列控制与实际动作展示。不展示无法观测的模型思考，不编造预计完成时间。
- [ ] 重跑相关测试、原有 Agent/Provider 回归。真实供应商验证需要用户选择服务与合法凭据，不用其他应用凭据代替。

**交付验收：**用户可在网页配置自己的 API 并运行/恢复受限研究；人工流程不依赖 API。

### Task 8: 启动交付、长书和前端验收

**Files:** Create `launcher.py`、`scripts/benchmark_library.py`、`docs/web-user-guide.md`、`web-client/e2e/acceptance.spec.ts`；Modify `pyproject.toml`、`README.md`、发布打包配置；Test `tests/web/test_launcher.py`。

**Interfaces:** 双击启动器启动本地服务并打开工作区；端口占用选择其他端口且打开正确地址；已运行同工作区复用实例。生产前端资产随 wheel/sdist 分发，缺失时中文报错，用户无需 Node。

- [ ] 写启动与打包测试：安装包包含前端产物；不同 cwd、中文与空格路径可启动；端口占用、再次启动和服务退出状态正确。
- [ ] 浏览器在 1440×900、1024×768、390×844 核对导航、阅读器、对话框、键盘焦点、长标题和错误状态，无整体横向溢出。
- [ ] 先用真实 91 页节选做识别/检索质量基线，再用 3–5 份真实 PDF 合计 1000 页验收，不限制每份必须 300 页。若缺真实长书，记录阻塞在“性能验收待补”，不得把重复页拼接称作真实长书通过。
- [ ] `scripts/benchmark_library.py` 输出硬件、页数、OCR耗时、峰值内存、索引大小和固定查询集冷/热 p50/p95；记录字错误率与检索漏检，不以 OCR 置信度代替准确率。目标：静止 p95≤1秒、OCR同时运行 p95≤2秒。
- [ ] 最终运行 `python -m pytest -q`、`npm --prefix web-client run build`、`npm --prefix web-client run test:e2e`、`uv build`；对安装产物从独立目录完成真实网页导入→检索→摘录→导出。
- [ ] Codex 阅读实际变更并独立验收；更新用户手册和未验证事项，交付启动器、源码及安装包。

**交付验收：**用户可以直接打开网页工作，真实能力与尚未验证的规模/供应商边界有完整记录。

## 自查与执行说明

本计划覆盖 spec 六个页面、核心能力、状态与验收。原有 CLI 代码保留；网页草图不计作功能完成。API、任务与前端契约先固定，任务 4/5 接真实接口后才称第一里程碑完成。

已完成独立 builder check；审阅提出的 completed 页强制 OCR、索引更新期间分页一致性、DOCX 阅读器三项问题已在 spec 与对应任务中作出明确决定，均由 Codex 作为实施建议补齐。

用户已指定 OpenCode 实施、Codex 验收；无需再选择执行方式。第一里程碑和 Task 6 现已分别验收；下一步先评估代表页识别质量，再推进 Task 7 的网页 API 配置与受限研究。第一里程碑的精确协作接口保留在 `docs/web-milestone-1-contract.md`。

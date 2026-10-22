# 网页摘录整理与资料导出 Implementation Plan

> **For agentic workers:** 按用户规则由 OpenCode 实施、Codex 规划审查与独立验收。无 Git 仓库，不初始化、提交或重置。原有已批准设计 Task 6 的细化；用户最新指令授权继续推进。

**Goal:** 用户在网页中将摘录归入专题、批量分类，并下载保留完整出处和核验状态的资料集。

**Architecture:** 复用 React/FastAPI/SQLite 与现有证据展开、CSV 防公式转义及 python-docx。增加集合与下载记录的事务迁移；预览与导出通过内容摘要检测变化，下载仅按稳定 ID 解析受控路径。

**Tech Stack:** 现有 Python 3.12、FastAPI、SQLite、python-docx、React/TypeScript/Vite；不增加运行依赖。

**Spec:** `../specs/2026-10-15-web-workbench-design.md` 的摘录集与导出部分；取舍依据 `../../github-reuse-research-2026-10-15.md`。

**最终状态：本轮已完成并独立验收。** 后端 322 passed / 2 skipped；前端 104 passed，生产构建成功。真实浏览器完成集合整理、四格式下载、预览变动与重启保留，Word 已渲染检查。详细证据和未完成范围见 [Task 6 验收](../../web-task6-acceptance.md)。下列原执行步骤保留，不把后验通过回填成未经证实的先红后绿记录；独立验收新增的发布、XML 和 UI 回归确实先复现失败，再验证修正。

## Global Constraints

- 用户使用网页；保留 Notion 样式、现有导航、CLI 公共接口、原件和所有已有摘录。
- 当前真实库 `outputs/library` 不作测试夹具；测试用临时工作区。本机源 PDF 只读。
- 升级先用 SQLite 备份，禁止 `INSERT OR REPLACE` 更新证据所属父记录。
- 所有写操作复用会话/CSRF/Origin 校验；未知请求字段拒绝，前端不得提交服务器文件路径。
- 资料导出支持 csv/json/markdown/docx；不生成研究结论，不将摘录已接受等同于分析结论已确认。
- 默认导出只含 accepted；用户可明确改为全部状态，导出必须逐条保留核验状态，stale/rejected 显示不可直接引用的提示。
- 一次请求最多操作 500 个显式摘录 ID；列表筛选的全部导出不受 30 条分页限制。
- 不增加候选 OCR、向量库、Agent 框架或外部服务依赖，不读取其他应用凭据。

## Review Focus

1. 选择跨页摘录、切换筛选后，导出范围不能悄悄变大或带入旧选择。
2. 预览与导出之间发生校订/审核/书目变化，必须返回 409 并重新预览，不能输出旧的已核验判断。
3. 集合、批量分类和迁移失败必须原子回滚，不损坏原摘录和版本。
4. 中文页码、控制符、CSV 公式、Markdown/HTML 和下载 ID 作为数据处理，不执行内容或访问任意路径。
5. 导出的引文对应保存版本，书目与当前核验状态取自一致快照；不得把当前全文替换历史引文。

## Task 1：集合、筛选与批量整理

**Files:** 新增 `src/litworkbench/collections.py`、`web/collections.py`；修改 `migrations.py`、`web/app.py`、`web/evidence.py`、`web/schemas.py`；测试 `tests/web/test_collections_api.py`。

**Interfaces:**

- `GET /api/v1/collections`：返回 `{items:[{id,name,description,evidence_count,created_at,updated_at}]}`。
- `POST /api/v1/collections`：`{name,description}`，201；名称 trim 后 1–80 字符、全库唯一；重复 409。
- `PATCH /collections/{id}`：改名称/说明，不删除集合；说明最多 1000 字符。
- `GET /evidence`：在现有文献/状态筛选上增加 `category`、`collection_id`，各条件取交集；继续分页。
- `POST /evidence/batch`：`{evidence_ids,category?,add_to_collection_id?,remove_from_collection_id?}`；至少一种操作，分类允许空串清除分类。验证全部 ID 后单事务处理，不提供批量接受核验功能；重复加入幂等。
- 显式 ID 数组不能为空；请求数组最多 500 项，重复 ID 按第一次出现去重，不扩大范围。
- 增量创建 `collections`、`collection_evidence`、`exports` 表。集合成员关系可移除，摘录本身不删除。

- [ ] 先写并运行失败测试：集合校验、65 条跨页数据、组合过滤、批量幂等、未知 ID 全回滚、原摘录/校订保留和旧库迁移。
- [ ] 实现上述事务与接口；重跑 `python -m pytest tests/web/test_collections_api.py tests/web/test_evidence_api.py tests/web/test_migrations.py -q`。

## Task 2：一致预览与四种格式下载

**Files:** 新增 `src/litworkbench/evidence_exports.py`、`web/exports.py`；按需小改 `export.py` 公开复用证据展开；修改 schemas/app；测试 `tests/web/test_exports_api.py`。

**Interfaces:**

- `POST /api/v1/exports/preview` 接收 `{format,filters:{doc_id?,category?,review_status?,collection_id?},evidence_ids?}`。未指定 review_status 时仅 accepted，显式 `all` 表示全部。显式 ID 与筛选交集，未知 ID 拒绝而非静默忽略。
- `evidence_ids` 缺省表示全部筛选结果；显式 `[]` 必须拒绝，不能以 falsy 判断回退到全库。请求数组上限和去重规则同批量操作。
- 返回 `{count,review_counts,snapshot_token,filename}`。内容摘要覆盖格式、完整导出范围、摘录字段、书目与页码；不包含每次变化的生成时间。
- `POST /api/v1/exports` 接收同样范围及必填 `snapshot_token`，内容变更 409；空结果拒绝。201 返回 `{id,format,filename,count,review_counts,download_url,sha256}`。
- `GET /api/v1/exports/{id}/download` 只从持久化记录解析 `artifacts/exports` 之内文件；正确附件名、媒体类型与会话校验。无任意路径参数。
- 一次事务快照捕获所需证据/文献/单元数据，输出快照再登记不可变文件。旧下载在书目后改时保持当时内容。
- 现有 `export._Repository`、`evidence_view` 与 `locator` 会重新开连接，不能原样调用后声称同一快照。必须在显式 BEGIN 后同一连接抓取所有字段；token 校验和四种序列化共用该对象。增加摘要计算后、序列化前发生校订/书目更新的竞争测试，输出不得混合新旧字段。
- 四种格式均包含 evidence_id、unit_id/doc_id、书名/作者/年份/版本卷次、原件哈希、文件页、印刷页及其确认状态、文字版本/哈希/偏移、引文、分类、备注、核验状态及核验记录。无需披露原件完整本机路径。
- JSON 为结构化资料；CSV UTF-8 BOM 并防公式；Markdown 按文献组织、转义 HTML/Markdown；DOCX 使用正常标题和段落逐条呈现，无虚构总结。文件中明确标注资料集与待核验状态。

- [ ] 先写并运行失败测试：65 条全量导出、默认 accepted、全部状态、显式选择/组合过滤、空结果、预览变更冲突、旧引用版本、CSV 公式/中文、Markdown 转义、DOCX 引文与出处、文件 ID 越界、迁移与服务重启后下载。
- [ ] 实现复用与路由，运行 `python -m pytest tests/web/test_exports_api.py tests/test_export.py tests/test_export_integrity.py -q`（先核对现有测试文件名，缺少的不要臆造执行通过）。

## Task 3：网页整理、导出与独立验收

**Files:** 修改 `web-client/src/pages/ExcerptsPage.tsx`，拆出集合/导出对话框；修改 API types/endpoints、hook 与局部 CSS；增加有意义的前端范围/选择/冲突回归测试；更新版本范围文案、README 与使用说明。

- [ ] 新增类别和集合筛选、集合新建/改名、已选条数、本页全选；跨分页保留选择，筛选改变清除选择并解释，不能把“本页”当“全部结果”。
- [ ] 现有查询 hook 保留旧 data；筛选切换及分页加载期间必须按数据所属查询 key 失效/禁用旧行选择和批量操作。预览请求需序号或 AbortController；范围/格式/状态改变立即清除旧 token，迟到预览不得恢复旧 token 或启用生成。
- [ ] 批量操作只处理显式选中 ID；创建后可加入集合，支持从当前集合移除；原有单条备注、状态与返回来源保留。
- [ ] 导出对话框明确“当前筛选的全部结果”或“已选 X 条”，默认 accepted，可选全部核验状态；预览显示实际数量和状态统计，再生成并下载。409 保留设置并重取预览。加载/错误/空结果可理解，键盘可操作。
- [ ] `npm.cmd test` 与 `npm.cmd run build` 通过。后端全套 `python -m pytest -q --tb=short` 通过，允许既有按环境条件跳过。
- [ ] Codex 阅读 changed_files，独立运行相关及全量检查；使用真实网页将已有安康摘录归入“文物建筑资料”集合，预览并下载，独立解读文件确认文件第 7 页/印刷页 775 与引文未变，源 PDF 哈希不变。服务重启后集合、摘录与下载仍可用。
- [ ] 保存实际网页截图及新验收记录。未取得真实模型或 1000 页结果，不改写其未验收状态。

执行前快照：`outputs/litworkbench-before-task6-20261015.zip`；工作目录清单 `work/task6-baseline-manifest.json`。本计划完成 Task 6 的资料整理与导出；AI 研究报告的网页生成继续由 Task 7 承担。

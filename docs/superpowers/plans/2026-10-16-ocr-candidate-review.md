# OCR 候选对照与采用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans；沿用用户指定的 Codex 规划/审查/独立验收、OpenCode 实现。用户已审阅完整设计并于本轮明确“开始吧”，不重复请求批准。目录没有 Git 仓库，不初始化、不提交、不清理；以来源快照和 ledger 记录变更。

**Goal:** 网页能对 PDF 指定页生成真实的省资源 OCR 候选，对照后逐页采用或保留现用，并保全校订、出处、摘录及检索一致性。

**Architecture:** SQLite 独立候选/尝试/底稿历史，页内容修订号与事务采用；持久化任务由带代次令牌的 worker 执行，工作区 OCR 闸门统一串行既有及新任务。受控子进程逐页识别，网页只排队、看结果和提交决定。

**Tech Stack:** 现有 Python 3.12、SQLite、FastAPI、RapidOCR/ONNX、React/TypeScript/Vite；资源保护明确使用锁定的 psutil。第一批只有 compatible-v1 / small-lowmem-v1 与 original 布局。

**Spec:** ../specs/2026-10-16-ocr-candidate-review-design.md（含独立施工审查修订）。

## Global Constraints

- 原件、OCR、人工校订与模型解释分层；来源文件页与印刷页确认分开。禁止 INSERT OR REPLACE。
- 不默认改新导入的识别配置；既有直接 reprocess API 保持语义，网页入口改为候选流程。
- 新候选 first batch 不安装 Docling/medium、不启用双栏，不访问远程 OCR/AI API；编程模型逐任务显式用免费 Flash，不改全局默认或回退付费模型。
- 输入只能同一 PDF 的显式 unit_ids，去重后 1–1000 页；profile/layout 白名单，不收任意路径、脚本或模型地址。
- small-lowmem-v1：render_scale=2，Rec/Cls 批次 1，ONNX intra/inter 各 2；初始化 120 秒、识别 60 秒，RSS/private 上限 2.5 GiB。
- 资源保护不可用则明确失败；时间/内存缺失是 null + 原因，不是假零。只终止自己启动的进程树。
- 真实库先快照、无活动任务后停旧 worker；开发测试及 12 页回归使用独立克隆。正式库仅在验收通过后备份迁移并重启，不自动重跑资料。
- 保留用户已有变更。新测试保存 RED/GREEN 输出，Codex 独立执行每单元验收，最终集中跑全套；不每次小改重复慢套件。

## Review Focus

1. 校订/确认为空白/元数据并发不能回退正文；修订号涵盖原文、校订、OCR 来源和状态，页码只更新字段（Task A）。
2. 采用或生成失败不能改旧摘录/集合；已保留校订的有效哈希不变时不无谓废弃该版证据；索引失败仍标 dirty（Task A/D）。
3. 重复采用、同页多候选、迟到执行者、服务重启不能二次提交；全部执行写入需要 owner+generation（Task A/B）。
4. 多 worker 分领不同任务也不能并行耗尽 RAM；丢失租约必须先收回受控页进程再让出 OCR 闸门（Task B）。
5. 网页翻页/切换文献时晚响应不能串页；失败/冲突/运行中禁用采用，候选不能直接摘录（Task C）。

## 文件责任与接口

| 文件 | 责任 |
| --- | --- |
| src/litworkbench/ocr_candidates.py（新） | CandidateStore：候选、尝试、原始底稿快照和采用事务 |
| src/litworkbench/ocr_profiles.py（新） | 两种固定配置、能力检查、模型/参数指纹 |
| src/litworkbench/ocr_process.py（新） | 子进程协议、初始化/单页看门狗、进程树资源采样与终止 |
| src/litworkbench/ocr_page_worker.py（新） | 实际 PDFium + RapidOCR 单页入口，输出真实框与来源 |
| src/litworkbench/migrations.py、store.py、evidence.py | 迁移 7、内容修订触发器、按字段元数据更新、共事务复核 |
| src/litworkbench/jobs.py、worker.py、ingest.py | 候选 kind、执行代次、检查点、OCR 闸门及既有识别路径保护 |
| src/litworkbench/web/ocr_candidates.py（新）、schemas.py、app.py、documents.py | 六个新接口、content_revision 与安全/校验 |
| web-client/src/pages/OcrCandidatesPanel.tsx（新）、ReprocessDialog.tsx、Reader.tsx | 原页/现用/候选对照、范围/配置弹窗和决定 |
| web-client/src/api/{types,endpoints}.ts、pages/JobsPage.tsx、styles/pages.css | 类型、请求、任务计数、窄屏与现有样式 |

`CandidateStore(workspace)` 的固定接口：`create_job(doc_id, unit_ids, *, profile_id, layout='original') -> dict`、`list_for_unit(unit_id, *, page=1, page_size=30, job_id=None) -> dict`、`get(candidate_id) -> dict`、`begin_attempt(job_id, unit_id, *, owner, generation) -> dict`、`complete_attempt(candidate_id, result, *, owner, generation) -> dict`、`fail_attempt(candidate_id, error, *, owner, generation) -> dict`、`adopt(candidate_id, *, expected_content_revision) -> dict`、`reject(candidate_id, *, reason='') -> dict`。

候选详情至少返回 `id,job_id,attempt_id,unit_id,doc_id,file_page,state,base_content_revision,profile_id,layout,result,error,created_at,decided_at`。`result` 含 text、lines、model_fingerprints、config、timing、memory、coordinate_system、render_width/height。`adopt` 返回候选及当前页；幂等重复返回原决定，不重写正文。

每页最多一个 queued/running 尝试；范围与配置入队时固定。首次候选/重试开始时，在同一事务读取当时页状态并保存基础修订快照；其后任何相关变动使该候选陈旧。重试新增 attempt/candidate，失败及中断记录不覆盖。该取快照时间是施工决定，避免长期排队页无意义地引用尚未开始处理的旧状态。

Web API 路径严格沿用 spec §7。分页、会话/CSRF、未知字段错误沿用既有实现。新 profile 清单 A 可先返回配置，实际模型/资源可用性由 B 补齐；A 不在 HTTP 内跑 OCR。

## Task A：候选、修订与采用事务

**Files:** 新增 ocr_candidates.py、web/ocr_candidates.py、tests/test_ocr_candidates.py、tests/web/test_ocr_candidates.py；修改 migrations.py、store.py、schemas.py、app.py、documents.py，必要时 evidence.py/index 服务的事务接缝。

- [ ] 写 RED 测试：v6→v7 保全既有字段/证据/集合；迁移幂等；content_revision 对直接 SQL 校订/空白/OCR 来源变更推进、纯页码/书目不推进；set_printed_page 仅字段写不回退新正文。
- [ ] 写 RED 测试：创建候选 job 不改 units/evidence/FTS；跨文献/Word/空集合/1001 页/未知配置拒绝；去重范围真实计数；尝试只有持有有效 owner/generation 可完成。
- [ ] 写 RED 测试：采用 CAS 冲突、幂等、同页其他 ready→stale、人工校订与印刷页保留、旧完整 OCR 快照、按所用版本复核、事务回滚不半提交；候选未采用不可检索。
- [ ] 运行定向命令确认缺少能力导致 RED，保留日志；实现服务及迁移 7。revision 用内容字段 WHEN 触发器集中保护，不让客户端写修订号；同文本但来源变化也推进。
- [ ] 旧 OCR 快照独立表存 JSON（含 unknown），不只保留 old_text；result 与决定记录不可被 API 改写。新增 claim_generation 字段供 B 使用；A 的尝试提交先核对 jobs.owner + claim_generation + 活动状态。
- [ ] 运行验收并由 Codex 检查变更：`python -B -m pytest tests/test_ocr_candidates.py tests/web/test_ocr_candidates.py tests/web/test_migrations.py tests/test_evidence.py tests/test_integrity.py tests/web/test_evidence_api.py -q -p no:cacheprovider`。

## Task B：真实省资源 OCR、执行令牌与恢复

**Files:** 新增 ocr_profiles.py、ocr_process.py、ocr_page_worker.py、tests/test_ocr_process.py、tests/web/test_candidate_jobs.py；修改 jobs.py、worker.py、ingest.py、ocr.py、pyproject.toml、uv.lock 与既有任务测试调用。

**Interfaces:** `get_profiles() -> list[dict]`、`run_page(workspace, unit, profile_id, *, checkpoint=None) -> dict`（结果或结构化异常）；worker 消费 Task A 的 CandidateStore。执行写入统一传 owner/generation；管理性 pause/resume 与执行写入分开。

- [ ] 写 RED 测试：配置传到实际子进程，不污染 _ENGINE 单例；stdout 协议有 init/page 阶段，分别 120/60 秒；采样缺失/超限/进程异常/空输出明示失败，保留旧正文。协议测试使用真正短子进程，不冒充真实 OCR。
- [ ] 写 RED 测试：所有执行期续租/阶段/检查点/完成操作拒绝过时代次；候选与 job_units 完成原子；中断尝试记 failed/interrupted 再创建新尝试；ready/adopted/rejected/stale 不重跑。
- [ ] 写 RED 测试：两个 Worker/不同任务争用同一工作区 OCR 闸门；旧 import/reprocess OCR 也占闸门；租约过期且旧页进程仍活时不能直接放行第二页；失去租约终止自有子树后释放，owner+generation 保护释放。
- [ ] 实现最小受控推理与固定模型指纹。raw 结果仅用 ID 派生路径、原子写入；拒绝链接/覆盖源输入；模型路径由后端解析而非请求指定。产品不 import 评测脚本。
- [ ] 处理 spawn 与登记 PID 之间的崩溃窗口：子进程先等待父进程 GO 握手，父进程将 PID + 创建时间 + 执行代次登记至闸门后才放行；登记前父进程退出则 stdin EOF，子进程不得启动 OCR。恢复时按 PID + 创建时间确认旧自有进程并收回，确认结束后才转交闸门。
- [ ] 既有后台 import/reprocess 不仅“登记闸门”，也要经可终止页进程执行 OCR，避免旧 worker 留驻的 RapidOCR 单例在候选运行时额外占用提交额度；可通过明确的 OCR 回调参数接缝保留 CLI/同步测试行为，不使用测试专用生产开关。
- [ ] psutil 加入 ocr extra，固定经验证版本并更新锁；用现有 uv 的绝对路径解析/安装，禁止另建无关依赖。主环境没有 pip；工具路径先核对，不试错污染环境。
- [ ] A 的计数扩展 candidate kind：运行完成数与待核 ready 数分开，纯索引任务不占 OCR 闸门。旧直接 reprocess 仍提交正式结果但同样保留校订并受执行权保护。
- [ ] 运行：`python -B -m pytest tests/test_ocr_candidates.py tests/test_ocr_process.py tests/web/test_candidate_jobs.py tests/web/test_jobs.py tests/web/test_reprocess.py tests/web/test_imports.py -q -p no:cacheprovider`。Codex 再在独立库真实识别至少一页，确认模型哈希、资源和不自动采用。

## Task C：网页候选对照与待核导航

**Files:** 新增 OcrCandidatesPanel.tsx 和对应测试；修改 ReprocessDialog.tsx、Reader.tsx、JobsPage.tsx、API 类型/请求与必要 CSS，保留既有摘录/原图/校订交互。

- [ ] 写 RED 交互测试：范围解析文件页、两种 profile 可用性、请求错误保留输入；new job 返回后显示待处理；生成不改现用。
- [ ] 写 RED 交互测试：翻页晚结果不串页；ready 可采用/保留、其他状态禁用；409 保留对照；有校订时明确只更新底稿；候选文字无正式摘录入口。
- [ ] 实现对照：原图占主要空间、现用/候选切换与差异开关，使用通用插入/删除/替换差异（不能只硬编码“媒妁”）；原始内容安全渲染，不用 innerHTML；大文本有界，不能冻结网页。
- [ ] 加任务待核入口与同任务下一页；文件页/印刷页状态分开；详情折叠展示真实配置/来源/耗时，均非准确率。弹窗 Esc/焦点、窄屏保留。
- [ ] `npm.cmd --prefix web-client test -- --run`；`npm.cmd --prefix web-client run build`。Codex 独立检查构建输出及浏览器真实 API 流程。

## Task D：集中验收、交付与正式库升级

**Files:** 新增 scripts/accept_ocr_candidates.py（内部验收）与 docs/ocr-candidate-acceptance.md；更新必要用户指南及本计划状态。

- [ ] 在独立克隆的 6 PDF / 91 页工作区生成固定 12 页真实候选，记录 source/page/model/config 指纹及资源，不回写正式库；12 页均计成功/失败，不剔难页。
- [ ] 建立带人工校订/摘录/集合的实际验收案例，证明生成不变现用；采用正确字、FTS 更新、旧引用需复核且快照不改；有人工校订的页采用只更新底稿；并发变更 409；重复采用幂等。
- [ ] `python -B -m pytest -q -p no:cacheprovider`，完整前端测试/构建只在最终代码集中执行；慢模型测试与真实 12 页证据分开报告。
- [ ] 独立代码审查（请求 fresh reviewer 只读，不再做第二套实现），按实际风险修复并以回归测试验收；不提交没有复验的 worker 自报。
- [ ] 浏览器验证真实生成/等待/对照/采用/保留/409/失败说明，保存截图；新旧 API 错误不被静态回退吞掉。
- [ ] 正式库仅在以上通过后 SQLite backup + 增量迁移；旧字段逐表比对相等，新增 revision 初值与 schema 更新可解释。重启自有应用/worker，确认用户仍能检索既有摘录并生成候选；不自动重 OCR 全库。
- [ ] 按实际结果交付。medium、双栏、人工金标准、坏文本层/表格、1000 页仍明确待验收，不扩成此轮已完成能力。

## 执行记录

详见项目上级 `work/ocr-candidates-2026-10-16/ledger.md`；保存每单元 worker session、变更文件、RED/GREEN、Codex 独立验证与 ruling。用户已明确启动，不在单元间重复征询。

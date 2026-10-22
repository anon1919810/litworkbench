# OCR 质量基线与候选解析器评估 Implementation Plan

> **For agentic workers:** 沿用用户指定方式：Codex 规划、审查和独立验收，OpenCode 实现可独立交付的任务。本轮已执行评估工具与真实样本对照，结果见 ../../ocr-quality-evaluation.md；产品运行代码仍未修改。目录没有 Git 仓库，不初始化、提交或重置。执行时逐项核对本计划及原设计。

**Goal:** 用真实县志样本测清错字、串栏、漏检和资源成本，再决定需要改渲染参数、识字模型还是版面解析，产出可复查的评估报告。

**Architecture:** 在独立评估目录运行现有 RapidOCR 与可选候选，输入来自源文件的只读派生页。结果保留原始输出、配置、模型指纹和页序，指标与网页所用 FTS 检索路径对应；真实文献库不参与评测写入。

**Tech Stack:** 现有 Python 3.12、pypdfium2、RapidOCR、ONNX Runtime、SQLite FTS5；候选 Docling 在单独环境运行。评分及报告工具仅使用标准库和已有依赖；内存采样依赖作为独立评测环境依赖记录。

**Spec:** [已批准的网页设计](../specs/2026-10-15-web-workbench-design.md) 中的真实资料、双栏顺序、坏文本层、页码与 1000 页验收要求；[候选调研](../../github-reuse-research-2026-10-15.md)。以下采样、超时与选型闸门是 Codex 提出的实施决定，不冒充用户指定指标。

## 现状核对：2026-10-16

- 真实库共 6 份 PDF、91 页；只读 SQL 统计为 **84 页 method=ocr、7 页 method=text**，91 页 extraction_status=completed。完成表示流程完成，不是准确率合格。
- 当前 RapidOCR 3.10.0、ONNX Runtime 1.31.0、pypdfium2 5.14.0；`ocr.get_engine()` 调用包默认 `RapidOCR()`，未显式固定识字模型文件。
- 自动模式按文本层有效字符阈值决定是否 OCR；当前默认 min_text_chars=20，渲染倍率 2.0（约 144 DPI）。扫描件经 PDFium 渲染，再由 RapidOCR 在本机 CPU 识别。
- `ocr.recognize()` 已返回文字、真实行框和置信度；当前文字按识别返回的行顺序拼接，未另做专门的双栏阅读顺序解析。
- 低平均置信度（低于 0.80）写提示；它不是准确率，也不能保证其他页没有错字。文本层字数够但内容错误的页目前依靠用户强制重识别。
- 网页已有后台队列、暂停/继续、失败重试、单页/整本强制识别、原页对照和人工校订。重新识别保留人工校订、版本历史及旧引文。

## Global Constraints

- 用户的主要入口继续是网页；下面的脚本属于内部验收工具，不把它们变成用户日常操作要求。
- 源目录和 `outputs/library` 只读。评测输出统一到项目上级 `work/ocr-quality/2026-10-16`，新建临时检索库也只能在其中。
- 原件、OCR 原文、人工校订、摘录、模型分析保持分层；评测不回写任何识别结果，不自动重跑整库。
- CPU 串行运行，PDFium 不跨线程并发；不同方案使用同一批页及明确记录的输入。
- 保存包版本、实际模型文件 SHA-256、配置和输入哈希；旧 OCR 未记录的模型指纹标为 unknown，不能用当前模型信息补写历史。
- 仅比较本地方案；下载官方模型是入站传输，不将县志发送至在线演示或远程识别服务。
- 文件页序、印刷页及确认状态分开；模型不能自动将印刷页候选改为已确认。
- 只有对照核稿后的参考文本才能评分；代理核稿标记 agent_checked，人审核稿标记 human_checked。前者只能称暂定参考，不能称人工金标准。
- 12 页样本只支持局部质量判断，不外推为 1000 页已达标。真实长书不足继续标为待验收。

## Review Focus

1. 文字数量足够但文本层错误：比较直取与强制 OCR，不把“有文本层”当作质量合格（任务 1、2）。
2. 两栏顺序及页眉页码混入正文：记录逐页顺序检查，不能只报平均置信度或全文字符错误率（任务 1、2、4）。
3. 稀有汉字、标点、数字和专名：评分只去空白，不做同音替换、繁简转换或大小写折叠；保留逐项漏检（任务 2）。
4. 引擎超时、没有框/置信度或参考文本未核稿：记失败或 unavailable，不伪造 0 错误或 100% 召回；报告所有缺失分母（任务 2、3、4）。
5. 评测误写真实库或候选污染既有环境：输入只读、显式独立输出目录、不同解释器启动候选；前后核对原件与证据字段（任务 1、3）。

## 文件与数据边界

新增内部工具包 `scripts/ocr_quality/`：

| 文件 | 责任 |
| --- | --- |
| `prepare.py` | 只读清点、导出派生单页 PDF/PNG、校验样本与参考稿 |
| `metrics.py` | 字符编辑距离、检索测试与汇总，不加载 OCR 引擎 |
| `run.py` | 调度当前环境的基线识别、计时及受控子进程 |
| `docling_adapter.py` | 单独解释器运行候选，保留原始结果和真实坐标 |
| `report.py` | 输出本地 HTML 对照报告及 JSON/CSV 结果 |

新增 `tests/quality/test_dataset.py`、`test_metrics.py`、`test_runner.py`、`test_report.py`。候选依赖锁及安装记录放在评测输出目录；本阶段不改 `src/litworkbench`、工作区 schema 或主环境依赖锁。

样本 JSON 顶层含 schema_version、source_manifest、samples、query_cases；每个 sample 至少保存 sample_id、source_sha256、source_filename、doc_id、file_page、printed_page、printed_page_status、tags、page_pdf、image_paths、reference_path。运行时实际源路径另存本机配置，报告用相对路径。

参考稿保存 text、review_level（draft/agent_checked/human_checked）、reviewer、uncertain_spans 和阅读顺序说明；存在未决字而无法形成完整参考的页不输出全文 CER，报告缺失原因，仍可做已核对关键词的局部检索检查。

统一结果至少有 sample_id、input_sha256、engine、engine_version、model_fingerprints、config、text、regions、coordinate_system、granularity、raw_output、error、timing、memory。没有真实置信度则为 null；只有块坐标时标 block，不能伪称行坐标。

## Task 1：准备 12 页与可追溯参考稿

**Files:** Create `scripts/ocr_quality/{__init__,prepare}.py`、`tests/quality/test_dataset.py`。

**Interfaces:** `prepare_dataset(library_root: Path, output_root: Path) -> Path` 只清点并输出 inventory.json 与 samples.draft.json，不自动挑页或 OCR；`materialize_dataset(manifest: Path) -> Path` 按已选定的 12 页输出派生单页 PDF、页图和参考稿；`validate_dataset(manifest: Path) -> dict` 校验选择、哈希、路径、参考稿状态，并返回覆盖与待核项。

- [x] 写失败测试：输入原件哈希不匹配、文件页越界、重复样本、输出覆盖源目录/真实库均拒绝；draft 参考不进入 CER 分母。
- [x] 运行 `python -m pytest tests/quality/test_dataset.py -q`，确认新契约失败后再实现只读清点及派生页输出。
- [x] 从 6 份真实 PDF **每份选 2 页，共 12 页**；固定包含《安康县志》文件第 7 页。选择其余页前看原图，尽量覆盖双栏、普通正文、淡字、专名数字和表格。某类不存在就记录缺项，不能用合成页冒充。
- [x] 以当前扫描/文本统计预期覆盖 10 页 OCR 来源和 2 页文本层来源；若逐页核对与此不同，以实测为准并解释。
- [x] 若发现“有效字数不少于 20，但文本层与原图明显不符”的真实页，优先纳入文本层样本；两页普通文本层不代表已覆盖坏文本层。样本库没有此类真实页时，报告明确标为“真实坏文本层场景未验收”，合成故障页仅能证明流程处理能力。
- [x] 参考稿可用已有文字预填，但须逐字对照原图，记录核稿身份与疑难字；每页至少 2 个确认存在的检索案例，共至少 24 个 query-page 阳性对，另设 6 个在所选样本中确认不存在的阴性对。
- [x] 输入、页图、单页 PDF 与参考稿各自记录 SHA-256；不将原有机器 OCR 直接当参考真值。相同书籍不同文件按内容哈希区分。
- [x] 重跑数据测试；核对原件哈希及真实库摘录/校订字段未变。

**验收命令（待实现后执行）：** `python -m scripts.ocr_quality.prepare --library ../library --out ../../work/ocr-quality/2026-10-16`；选页写入 samples.json 后运行 `python -m scripts.ocr_quality.prepare --materialize ../../work/ocr-quality/2026-10-16/samples.json`，再用 `--validate` 校验同一清单。输出须如实区分已核稿和待核稿，不能仅因凑齐 12 页返回“质量通过”。

## Task 2：现有 RapidOCR 基线与评分

**Files:** Create `scripts/ocr_quality/{metrics,run}.py`、`tests/quality/{test_metrics,test_runner}.py`。

**Interfaces:** `score_text(reference: str, hypothesis: str) -> dict` 返回 distance、reference_chars、cer；`evaluate_queries(dataset: dict, results: list[dict], scratch_root: Path) -> dict` 在临时工作区使用 `search_index.index_many` 和 `search_index.search_indexed(..., mode='phrase')` 核对预期页；`run_benchmark(manifest: Path, output_root: Path, profile: str) -> Path` 输出 results.jsonl 与运行指纹。

- [x] 写失败测试：一个字符替换/删除/插入的编辑距离正确；跨行词与生僻字按 Unicode 码点评分；空参考返回 unavailable；识别空串必须计漏检，不能把失败样本移出总体报告；超时结果持久化后其他页继续。
- [x] 执行 `python -m pytest tests/quality/test_metrics.py tests/quality/test_runner.py -q` 确认失败，再实现最小评分和受控运行。
- [ ] 直接复用 `ocr.recognize(image)` 得到真实识别结果；先测当前倍率 2.0。两张代表扫描页另测 3.0 和 4.0（约 216/288 DPI），再选一个有实测收益的倍率在其余样本测；模型内部缩放参数同时记录，不能假定放大必然提升识别。
- [x] 对文本层样本同时比较直取与强制 OCR；报告“按 auto 实际使用的流程”和“强制 OCR 对照”两条结果，避免把全扫描的 force 测试冒充 auto 流程。
- [x] CER = 去空白后字符编辑距离 / 参考字符数，合并指标按参考字符数加权；逐页展示，并区分 agent_checked 与 human_checked。报告检索阳性命中/总数、具体漏词、阴性异常命中，以及匹配到了哪一文件页。
- [x] 顺序另行逐页对照，结果为 unreviewed/ok/wrong/unavailable，记录核稿者与理由；不能用 CER 自动宣告两栏顺序正确。
- [ ] 模型初始化与单页渲染/识别分别计时，温运行至少 3 次，报告中位数、最大耗时和样本数；初次模型下载不混入温运行。
- [x] 内存记录受控进程树 RSS 的采样峰值、采样间隔及“非精确系统峰值”说明。采样依赖或权限缺失则标 unavailable；不能写 0。默认逐页执行超时 180 秒，初始化超时 600 秒，超时只终止本工具启动的进程。
- [x] 重跑测试；对暂定参考尚未核稿的页只报告运行结果与待核项，不生成误导性总准确率。

**验收命令（待实现后执行）：** `python -m scripts.ocr_quality.run --manifest ../../work/ocr-quality/2026-10-16/samples.json --profile rapidocr-current --out ../../work/ocr-quality/2026-10-16/runs/rapidocr-current`。原始结果和指标必须能从同一 run_id 追溯。

## Task 3：候选单页可行性，再决定扩大对比

**Files:** Create `scripts/ocr_quality/docling_adapter.py`；Modify `run.py`；Test `tests/quality/test_runner.py`。

**Interfaces:** 候选脚本接收 `--input <single-page.pdf> --output <result.json>`；调度器通过显式 `--candidate-python <absolute-python-path>` 启动，输出遵循统一结果字段，保存原始 Docling 结果。不在主环境 import 或安装 Docling。

- [x] 先记录官方已发布版本、Python/CPU 支持、模型来源及锁定版本；将所需模型下载至评测缓存，保留文件指纹。不得把示例中的远程 PDF URL 当作用户输入来源。
- [x] 使用 Docling 官方 RapidOCR 接口，优先评估“现有识字路线 + 版面/顺序解析”。现有 ONNX 文件是否兼容由实际加载验证；不默认兼容，也不将新模型收益归因于版面算法。
- [x] 写失败测试：候选解释器不存在、缺包、超时、无结果、缺坐标、缺置信度均产生明确结果；未安装候选不影响 RapidOCR 基线。纯协议用临时假子进程测试，真实模型另作集成验收。
- [x] `python -m pytest tests/quality/test_runner.py -q` 先确认新契约失败，再增加子进程适配；单页先验证成功及资源占用，随后才扩至 12 页。
- [x] 候选进程树采样 RSS 超过 8 GiB 时终止本次评测并记录超限。安装后的完整依赖与缓存体积单列；这是本机筛选闸门，不是所有电脑的资源承诺。
- [x] 可复用同一模型时固定模型做顺序对照；无法复用时明确作为整套管线比较。Docling 单页不可用或成本过高时保留失败记录，不无限修环境；PP-StructureV3 只作为下一候选，另开评估轮次。
- [x] 重跑协议测试；真实候选结果不能以模拟子进程测试代替；确认主环境锁及真实库未被修改。

**验收命令（待实现后执行）：** `python -m scripts.ocr_quality.run --manifest ../../work/ocr-quality/2026-10-16/samples.json --profile docling-rapidocr --candidate-python <独立环境的绝对Python路径> --out ../../work/ocr-quality/2026-10-16/runs/docling-rapidocr`。无模型环境时应给出明确不可用状态，不返回通过。

## Task 4：可阅读对照报告与选型决定

**Files:** Create `scripts/ocr_quality/report.py`、`tests/quality/test_report.py`；Create `docs/ocr-quality-evaluation.md`（实际评测后）。

**Interfaces:** `build_report(manifest: Path, run_paths: list[Path], output_root: Path) -> Path` 产出 report.html、summary.json、metrics.csv；HTML 使用相对资源、无外链脚本，提供原图/参考稿/各方案结果与差异、漏词及阅读顺序核对表。

- [x] 写失败测试：报告转义文献中的 HTML、区分未经核稿/失败/未提供指标、所有页都有结果或缺失原因；不混用不同输入哈希或参考版本的指标。
- [x] `python -m pytest tests/quality/test_report.py -q` 确认失败，再实现本地对照报告；修改参考稿后旧评分需标过期或重新计算。
- [ ] 用真实 12 页检查原图、中文、生僻字、数字、两栏与表格结果；报告不将表格转成 Markdown 就称结构正确，仍需对照原图。
- [x] 选型优先保留已正确的关键专名/数字与页定位；按单页 CER、漏检、顺序、时间及资源给出决定。不预定 Docling 胜出。
- [x] 建议采用闸门：候选需修复至少 2 页明确顺序错误，或在同等核稿级别样本上加权 CER 相对下降至少 20%；同时已正确的关键检索案例不得新增漏检，资源在上述闸门内。没有可评分基线就不能用这些数字判定胜出。
- [x] 比较固定相同输入、参考稿版本和核稿级别的共同集合，并单列全部 12 页的成功/失败/未评分数。候选比基线新增失败页时不得自动判为可采用；不能靠排除失败或难页使平均 CER 看起来下降。
- [x] 顺序改善不能抵消未披露的识字退化：任一共同可评分页 CER 上升，或原本正确的顺序变错，均列出具体差异，不能自动推荐为默认方案。只在问题页有收益的方案可进入“局部可选增强”接入评审，不据汇总收益覆盖原默认；全体 CER 改善但个别页退化同样遵守此规则。
- [x] 无明显收益则保留当前方案并记录具体缺口；有收益才为“产品接入、页级质量提示与增强重识别”另写有真实接口和回归测试的落地计划。本评估阶段不预埋多引擎产品架构。
- [x] Codex 独立运行 `python -m pytest tests/quality -q`，再检查 HTML、原始结果、文件哈希及只读边界。只有新增模块可能影响全局时才扩大回归；本轮纯计划不运行测试冒充实现验证。

**验收命令（待实现后执行）：** `python -m scripts.ocr_quality.report --manifest ../../work/ocr-quality/2026-10-16/samples.json --runs ../../work/ocr-quality/2026-10-16/runs --out ../../work/ocr-quality/2026-10-16/report`。

## 后续推进顺序

1. **现在优先：本计划的 12 页质量基线与候选单页验证。** 交付看得见的错字、串栏、漏检和成本结果。
2. **依据评估接入有效改进。** 网页展示需要核对的页及原因，保留当前页/页范围/整本重识别和旧版本，低置信提示不伪装成准确率。具体字段与迁移在选型后设计。
3. **网页自有 API 与受限 Agent。** 沿用原 Task 7 的范围约束、会话密钥和可恢复动作；人工导入、OCR、检索和摘录不依赖模型 API。先做好本机协议模拟器验证，真实供应商仍需用户选择和提供凭据。
4. **真实长书到位后补 1000 页容量与检索验收。** 可与第 3 项并行，目标仍是 3–5 份 PDF 合计 1000 页；当前样本不足，不能以复制页或线性时长估算替代。资料不足不阻止独立的网页 API 任务推进，但最终发布必须如实保留规模未验收状态。

本计划已按现有代码核对接口并自查；独立可执行性检查指出的“其他页识字退化”和“真实坏文本层覆盖”两处边界已补入任务。状态为评估里程碑完成，保留三项未完全满足的检查：倍率 3/4 全量对比、严格预热计时及真实表格检查；不将这些缺项宣告通过。实施方式已由用户确定，不再重复询问由谁实现。

## 官方参考（2026-10-16 复核）

- [Docling：RapidOCR 自定义 ONNX 模型示例](https://docling-project.github.io/docling/_generated/examples/rapidocr_with_custom_models/)。接口可用不代表本机既有权重已兼容。
- [PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md)：官方说明包括多栏顺序、表格及结构化结果，仍须县志实测。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：现有本地识字组件；本项目实际配置和代码是当前行为的依据。

## 执行结项：2026-10-16

真实 12 页在当前默认、small 低批次、medium 低批次、Docling 四套实际推理中全部完成。Docling 全层导出另记为同一原始结果的回放。共同 CER 集合是 5 页代理暂定参考、4291 字；7 页未决字不评分，仍参与已确认关键词检查。28 个阳性与 19 个阴性案例均保留。

同批次控制后 medium 字符错误相对下降 17.39%，未达到 20% 闸门；GapTree 修复两页双栏但出现一页标题退化；Docling 全层导出仍有顺序退化。保留当前默认，只将小批次、局部多栏排序及 medium 送入接入验证，不自动全库替换。

执行调整：本机 RAM/Windows 提交额度紧张，超时改为初始化 120 秒、单页 60 秒，RSS 与 private commit 闸门收紧为 2.5/3 GiB。倍率 3/4 仅派生图已完成，完整识别比较因资源失败未完成，不能断言最佳 DPI。三次计时包含首次推理的潜在冷成本，不称纯温运行。没有真实表格、坏文本层与 1000 页长书覆盖。

独立验收：109 passed、3 skipped；最后的报告过期指标回归修复后相关 38 项通过。CLI 与编译检查通过；数据集 0 errors、7 个未决字警告；网页 12 张原图加载成功，五套结果哈希有效且使用相同可评分页。六张真实表及六份 PDF 哈希前后保持，旧产品源码与依赖锁保持。详见 ../../ocr-quality-evaluation.md，产物在项目上级 work/ocr-quality/2026-10-16。

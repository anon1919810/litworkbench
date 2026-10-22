# 可复用项目调研与推进决策

核对日期：2026-10-15。依据 GitHub 官方仓库、许可证与官方文档；本轮未安装或在真实县志上运行这些候选项目。功能描述不等于本机性能或识别质量验收。

2026-10-16 状态更新：摘录集合与资料导出已通过 [Task 6 验收](web-task6-acceptance.md)。下一阶段优先执行 [OCR 质量评估计划](superpowers/plans/2026-10-16-ocr-quality-evaluation.md)，下方分期中的“本轮实施”保留为 10 月 15 日决策记录；候选解析器仍未接入产品。

## 筛选结论

| 项目与官方来源 | 对本工作台的用途 | 取舍 |
| --- | --- | --- |
| [Docling](https://github.com/docling-project/docling) | PDF 版面、阅读顺序、表格结构，统一文档表示，支持本地执行及 Windows | 首个可选解析器候选；代码 MIT，所用模型许可证需单独核对；与 RapidOCR 比较后再决定接入 |
| [PaddleOCR / PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md) | 版面区域、表格、公式与中文文档结构提取 | 第二候选；[代码 Apache-2.0](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE)。需另装 PaddlePaddle 等运行依赖，先测本机成本 |
| [MinerU](https://github.com/opendatalab/MinerU) | 结构化解析、文档库与稳定页/块定位 | 保留候选；当前仓库为自定义 MinerU 许可（Apache-2.0 基础加附加条款），不是无附加条件的 Apache-2.0。见[许可原文](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)。当前文档也提供 CPU 模式，不能沿用旧印象断言必须大显卡 |
| [kotaemon](https://github.com/Cinnamon/kotaemon) | 混合检索、重排、模型配置、带原页预览的引用 | 参考研究交互与管线边界；Apache-2.0。它使用 Gradio，与现有 React/FastAPI 页面并不直接兼容，不整站移植 |
| [PDF.js](https://github.com/mozilla/pdf.js) | 浏览器 PDF 阅读、缩放和文本层 | Apache-2.0；先保留已验收的 PDFium 页图与 OCR 框，待原页选字成为明确需求再接入 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 有状态 Agent、持久化执行与人工介入 | MIT；现有四动作 Agent 已有检查点，当前先完善范围隔离和网页配置，后续出现复杂分支再评估框架 |

这些项目对当前目标的优先级是 Docling/PP-StructureV3 的识别对比，其次 kotaemon 的带出处研究交互。PDF.js 与 LangGraph 暂不增加为依赖。没有按星数决定采用，也没有把演示效果当作县志质量证据。

## 本次核对的仓库版本

使用 GitHub 公开 API 另行核对默认分支提交；以下均未归档。下次实际安装时应重新选择并固定版本，不直接安装浮动分支。

| 项目 | 本次默认分支提交 |
| --- | --- |
| Docling | [6fadbae317b0](https://github.com/docling-project/docling/commit/6fadbae317b09c4dd4051c0f1fc1746c6baf04bc) |
| PaddleOCR | [dab3fe353790](https://github.com/PaddlePaddle/PaddleOCR/commit/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf) |
| MinerU | [b7f3dd0c5f6e](https://github.com/opendatalab/MinerU/commit/b7f3dd0c5f6e6d66a298239ffd02b328b873918e) |
| kotaemon | [9ad3e4e49aa3](https://github.com/Cinnamon/kotaemon/commit/9ad3e4e49aa35b8acddd235918a5d9753c1cfdf9) |
| PDF.js | [0252c1f6c97d](https://github.com/mozilla/pdf.js/commit/0252c1f6c97df44361646a8158e06dcb8bf9aa33) |
| LangGraph | [5eb60aef6f38](https://github.com/langchain-ai/langgraph/commit/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b) |

本机只读检测为 Ryzen 7 8845H、系统可见物理内存约 27.8 GiB、Radeon 780M。当前 OCR 仍以 CPU 运行；候选解析器的安装体积、首次模型下载和实际耗时需单独测量，不能由硬件名称推断达标。原始记录在工作目录 `work/github-reuse-20261015.json` 与 `work/task6-hardware.json`。

## 可直接沿用的现有组件

现有 RapidOCR/ONNX Runtime、pypdfium2、SQLite FTS5 和 python-docx 已承担真实工作。摘录导出将继续复用本项目的 `export.evidence_view`、CSV 转义与 python-docx；不为单机 1000 页目标新增向量数据库或常驻 Docker 服务。

Docling 的[官方 RapidOCR 示例](https://docling-project.github.io/docling/_generated/examples/rapidocr_with_custom_models/)支持指定 ONNX 检测、识别与方向分类模型。因此后续应优先比较“保留 RapidOCR、增加版面和阅读顺序解析”的组合，而不是先更换所有识字模型。其[离线与高级配置](https://docling-project.github.io/docling/usage/advanced_options/)也明确区分模型下载与向外部服务发送资料；本地方案保持远程服务关闭。尚未验证现有模型文件能否不经转换直接复用。

本轮不复制候选仓库源代码，也不将用户资料传到其在线演示站。后续若引入代码或模型，需记录固定版本、来源、许可证、磁盘占用和实际识别结果。

## 分期任务

1. **本轮实施：网页摘录整理与资料导出。** 完成专题集合、跨页多选、批量分类、筛选、预览与 CSV/JSON/Markdown/DOCX 下载。沿用已批准设计 Task 6，输出保留逐条原文、核验状态、文件页与印刷页，资料集不冒充 AI 分析报告。
2. **识别质量对比。** 从已有 91 页选择约 12 个代表页，含双栏、淡字、专名与数字。人工标注正确文字及阅读顺序；对比当前 RapidOCR 和候选解析器的字符错误率、顺序错误、指定词召回、耗时及内存。先验证安装和一页输出，再决定扩大测试；不自动替换已有文字版本。
3. **网页自有 API 与受限研究。** 使用已有 Task 7：会话内密钥、文献范围快照、search/read/excerpt/finish、暂停恢复、逐条证据与报告。先用本机协议模拟器验收，真实供应商需用户自行提供服务与凭据。借鉴 kotaemon 的引用预览，保留精确字面出处。
4. **合计 1000 页与交付。** 真正长书尚缺；准备基准脚本，采集硬件、内存、OCR 时间及检索 p50/p95。不得拼接重复页冒充 1000 页真实验收。再补安装包和启动器复用已运行服务。

排序由 Codex 根据现有代码与用户目标提出；用户已要求继续设计并推进，当前执行第 1 项，不把后续候选写成已安装或已实现。

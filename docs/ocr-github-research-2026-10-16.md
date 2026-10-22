# 面向县志的 OCR 复用调研与实际取舍

2026-10-16。用户已批准集中推进 OCR。本记录区分“读过官方资料/代码”“已下载核验”“实际运行”“可进入产品”，不把项目介绍或排行榜当作县志质量证据。

## 本机基线与资源

现有 RapidOCR 3.10.0 实际配置是 **PP-OCRv6 small 检测 + small 识别**，不是旧版 v4；ONNX Runtime 1.31.0，CPU 推理。三个现有模型指纹已与本机文件及官方模型清单核对：

| 文件 | SHA-256 |
| --- | --- |
| `PP-OCRv6_det_small.onnx` | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| `PP-OCRv6_rec_small.onnx` | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` |
| `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |

当前渲染倍率 2.0、Global.text_score=0.5、Det.limit_side_len=736、limit_type=min；模型输入缩放也会影响实际识别尺寸。高倍率的收益必须实测，不能仅靠把 PDF 渲染得更大来推断。

开始时 C 盘约剩 16 GiB；评估优先使用现有模型与小型后处理。候选环境单独放在工作目录，保留至少 8 GiB 可用空间作为本轮操作预算，不删除用户文件腾空间。

## 可复用能力

| 项目 | 读到的具体帮助 | 当前决定 |
| --- | --- | --- |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | [官方模型清单](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml)有 v6 tiny/small/medium，支持显式模型路径、参数与真实框 | 同一推理路线比较 small 与 medium，以及倍率和检测参数；保留原始输出和模型指纹 |
| [GapTree](https://github.com/hiroi-sora/GapTree_Sort_Algorithm) / [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) | 多栏间隙树排序，使用已有 OCR 框；有跨列标题、块排序和段落处理代码 | 优先小样实验：不需要另装识字模型。先只排序，不默认删页眉页码或合并改写引文；若复制代码进入交付工具需保留 MIT 许可与作者声明 |
| [Docling](https://github.com/docling-project/docling) | [RapidOCR 自定义模型接口](https://docling-project.github.io/docling/_generated/examples/rapidocr_with_custom_models/)、版面与结构化文档输出；[OCR 适配代码](https://github.com/docling-project/docling/blob/main/docling/models/stages/ocr/rapid_ocr_model.py) | 独立环境单页验证后再扩大；尽量固定同一 OCR 模型，区分版面收益与识字模型收益 |
| [PaddleOCR / PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md) | 结构、阅读顺序、表格和 OCR 管线，当前文档也列有多种推理后端 | 保留复杂版面对照路线；先验证现有 ONNX 路线收益，按样本缺口决定是否增加完整管线 |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | [旋转、纠偏、清理流程](https://github.com/ocrmypdf/OCRmyPDF/blob/main/docs/cookbook.md)及可检索 PDF 输出；[双页扫描处理](https://github.com/ocrmypdf/OCRmyPDF/blob/main/docs/advanced.md) | 借鉴预处理顺序与可回溯输出思路；当前工作台索引不需要先改写 PDF。Tesseract/Ghostscript 等依赖另算，暂不整套安装 |
| [OmniDocBench](https://github.com/opendatalab/OmniDocBench) | 分开评估文字、表格、阅读顺序，按版面/语言属性分析，参考真值保持固定 | 借鉴分类和匹配原则，先用本地 12 页；不安装含公式 TeX 环境的全套评测，也不把本项目的简化指标称作完整 OmniDocBench 分数 |
| [OpenOCR](https://github.com/Topdu/OpenOCR) | 识字模型、训练和评测的研究实现 | 用作专用模型路线的后续参考；当前先判断检测、识字或顺序哪一项导致漏检 |
| [Surya](https://github.com/datalab-to/surya) | 当前介绍为 650M OCR，兼顾版面和顺序，代码与模型许可口径分开 | 本轮 CPU/磁盘预算下不作为首个安装候选；官方 GPU 吞吐不外推为本机速度，模型条件需按实际权重另核对 |
| [MinerU](https://github.com/opendatalab/MinerU) | 本地结构化解析和多档 CPU 路线 | 保留候选；版本和模型许可、运行体积另测，不能套用旧的“必须大显卡”印象 |

调研包括仓库代码及问题记录。[Docling 多栏问题](https://github.com/docling-project/docling/issues/2067)和[空间排序需求](https://github.com/docling-project/docling/issues/2683)说明需要实测具体版面；问题报告是个案，不等于当前版本仍必然存在同一缺陷。

## 已核验的具体动作

- 从六份 PDF 各选两页，共 12 页：10 页扫描来源、2 页文本层来源；固定包含《安康县志》文件第 7 页。选择依据是原图，原 OCR 只用于参考稿预填。
- 从官方 v3.10.0 模型地址下载并核验 medium 检测、识别模型，合计约 132 MiB：
  - det：`92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2`
  - rec：`eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b`
- 获取并审查了 Umi-OCR 固定提交中的纯 Python GapTree，在真实行框上完成实验：两页强制 OCR 双栏顺序恢复，但一页单栏标题退化。行对象完整保留；未接入产品默认流程。
- Docling 选择固定版本 2.139.0 的 slim 组件：convert-core、format-pdf、feat-ocr-rapidocr-onnx、models-local，显式使用 CPU PyTorch，不拉取 VLM/音频功能。83 个已解析依赖按实际下载 URL 和 SHA-256 锁定至独立环境。两页单页闸门通过后完成 12 页对照；采用 PDFium 后端，固定本机 ONNX 与离线布局权重。
- 实际看到了可固定的识字错误：安康扫描页中“高1.5米”的数字被识别为“高！.5米”。未因平均置信度或 completed 状态而认定它准确。

## 固定版本与证据

GitHub 默认分支提交已由公开 API 核对；未按星数排名。完整 SHA、API 许可字段、包元数据、模型下载地址与校验记录在项目上级 `work/ocr-quality/2026-10-16/research/`。

| 仓库 | 本次核对提交 |
| --- | --- |
| RapidAI/RapidOCR | `7cfd3d9ef786a2e538ca30df700325e72e85b5f5` |
| docling-project/docling | `6fadbae317b09c4dd4051c0f1fc1746c6baf04bc` |
| PaddlePaddle/PaddleOCR | `dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf` |
| hiroi-sora/Umi-OCR | `83173efc4f4453b41223ea96d63d44534bf0505c` |
| ocrmypdf/OCRmyPDF | `cf0df3da06aef04fe9a5bf91c6feff36370bb3eb` |
| opendatalab/OmniDocBench | `f133a71e9e91c3621c7ce8994200a7b394a06eb3` |
| Topdu/OpenOCR | `1ccfc6ee6161f7133b192e16af3a9265273997ba` |
| datalab-to/surya | `a2363d3311773a2b6145a40458043211c50c52f4` |
| opendatalab/MinerU | `b7f3dd0c5f6e6d66a298239ffd02b328b873918e` |

## 实测后的决定

当前默认在 5 页暂定参考上的字符距离为 24/4291、关键词命中 24/28。small 模型批次 1 控制组为 23/4291、25/28；相同批次与线程下 medium 识字为 19/4291、25/28，字符错误下降 17.39%，关键词没有进一步收益。所有最终方案阴性误命中均为 2/19，尚有专名与数字错误。

Docling 原始默认导出为 242/4291；保留全部内容层重新导出同一结构化结果后为 204/4291。后者是导出回放，没有新模型推理。标题被移到页末、引号变化等仍影响文献保真，暂不替换当前默认。更大的框架不自动意味着更适合逐字摘录。

结论：优先验证小批次配置接入；GapTree 只作为明确多栏页的局部候选；medium 保留精细识字候选。PaddleOCR、OCRmyPDF 等尚未在本机实际集成；未因读过资料而宣称可用。完整逐页结果、局限和复现见 [OCR 实测报告](ocr-quality-evaluation.md)。

执行计划见 [OCR 质量基线与候选评估](superpowers/plans/2026-10-16-ocr-quality-evaluation.md)。本轮不代表 1000 页规模、真实表格、坏文本层或人工金标准准确率已经验收。

# 本地文献工作台（litworkbench）

本地优先的文献工作台。网页版提供扫描 PDF / 文本 PDF / DOCX 的批量导入、后台 OCR、全文检索、原页核对、摘录、专题归类与 CSV/JSON/Markdown/DOCX 资料集导出；原件、数据库和摘录保存在本机，基础流程不需要 AI API。原有 CLI 与其 Agent 研究、报告导出功能保留；网页 AI 研究与团队协作尚未接入。

## 在这台电脑上打开网页

双击项目目录中的 **`启动文献工作台.cmd`**，浏览器会打开本机服务。默认数据目录为旁边的 `outputs/library`，日常使用无需输入命令，也无需运行 Node。

先在“文献库”批量选择文件，再到“识别任务”查看进度；识别完成后进入“全库检索”，打开命中原页，选中文字保存到“摘录集”。PDF 文件页序和书上印刷页码分别记录。

详细操作见 [网页版使用说明](docs/web-user-guide.md)。当前为本机开发版；完整跨电脑安装包及合计 1000 页的真实容量验收后续提供。

重新识别使用「生成候选 → 对照原页 → 手动采用或保留」流程。生成不会覆盖现用文字，
采用保留已有校订及旧 OCR 快照。网页 OCR 逐页运行，具有超时、内存监控与中断恢复；
仅使用本机已有模型，不会在网页请求中下载权重。详见使用说明中的「重新识别」。

## 命令行入口（可选）

在本文件所在目录打开 PowerShell。已准备好的本地运行环境可以通过 `lit.cmd` 使用：

```powershell
.\lit.cmd --help
.\lit.cmd --workspace ".\我的文献库" init
.\lit.cmd --workspace ".\我的文献库" import "D:\文献\某县志.pdf"
.\lit.cmd --workspace ".\我的文献库" search "灯会"
```

把路径换成真实资料路径。所有命令都将结构化 JSON 写到标准输出，错误和进度写到标准错误；可供其他 Agent 或脚本调用。`--workspace` 放在子命令前。默认扫描件在本地 CPU 上 OCR，无需部署大语言模型或购买显卡。

## 在其他电脑安装

建议 Python 3.12；本项目要求 Python 3.11 及以上。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在项目目录运行：

```powershell
uv sync --python 3.12 --extra ocr
uv run --extra ocr lit --help
uv run --extra ocr lit --workspace ".\我的文献库" init
```

也可用 pip：`python -m pip install ".[ocr]"`，安装后命令为 `lit`。只处理文字 PDF / DOCX 可不安装 `ocr` extra。`uv.lock` 固定依赖版本；首次安装需要网络，OCR 模型首次初始化也可能需要下载，准备完成后识别在本地进行。

## 导入、定位与摘录

```powershell
.\lit.cmd --workspace ".\我的文献库" import "D:\文献" --ocr auto
.\lit.cmd --workspace ".\我的文献库" list
.\lit.cmd --workspace ".\我的文献库" search "灯会"
.\lit.cmd --workspace ".\我的文献库" read "<搜索结果的unit_id>"
.\lit.cmd --workspace ".\我的文献库" render "<PDF的unit_id>"
.\lit.cmd --workspace ".\我的文献库" metadata "<doc_id>" --set "title=某县志" "author=某县志编纂委员会" "year=1998" "edition=第一版" "volume=卷二"
.\lit.cmd --workspace ".\我的文献库" label "<unit_id>" "317"
.\lit.cmd --workspace ".\我的文献库" excerpt save "<unit_id>" --quote "从read结果复制原文" --category "民俗" --note "待核对原件"
.\lit.cmd --workspace ".\我的文献库" excerpt review "<evidence_id>" --status accepted --note "已对照扫描页"
.\lit.cmd --workspace ".\我的文献库" export evidence --format csv --out ".\摘录.csv"
```

`<...>` 是说明用占位符，需要替换为实际 JSON 结果中的 ID。

- `auto` 按页判断文字量；`off` 只读文本层；`force` 对页面强制 OCR。未识别页保留 pending/failed 状态，重复导入同一原件可补做；哈希相同的文件不会重复入库。
- PDF 文件页序从 1 开始，与印刷页码分别保存；DOCX 用按文档顺序排列的段落/表格单元定位，不编造 Word 页码。
- `correct <unit_id> --text-file 校订.txt --reason 原因` 保存整单元校订；`correct <unit_id> --revert --reason 原因` 撤销。原识别结果与历史保留，相关旧摘录标为 stale。
- 摘录要求来自原文，允许跨换行匹配，保存的是原文精确切片及偏移。accepted 表示人工接受摘录；pending 是待核验；rejected 是拒绝；stale 是文本变化后待复核。
- 为准确操作多版本同名文献，优先使用完整 doc_id，不要仅使用模糊文件名。

## 使用自己的 AI API

编辑文献库内的 `config.toml`，设置兼容 `/chat/completions` 的服务。示例地址不是可用供应商：

```toml
[provider]
base_url = "https://your-provider.example/v1"
model = "your-model-name"
api_key_env = "LITWORKBENCH_API_KEY"
timeout = 60.0
temperature = 0.0
max_output_tokens = 2048
```

在当前 PowerShell 设置环境变量，密钥不要写进 TOML 或发给本项目作者：

```powershell
$env:LITWORKBENCH_API_KEY = "你的密钥"
.\lit.cmd --workspace ".\我的文献库" research "查找灯会记载，按活动形式与历史沿革整理出处" --profile heritage --max-steps 16
.\lit.cmd --workspace ".\我的文献库" tasks
.\lit.cmd --workspace ".\我的文献库" resume "<task_id>" --max-steps 8
.\lit.cmd --workspace ".\我的文献库" export report "<task_id>" --format markdown --out ".\研究报告.md"
.\lit.cmd --workspace ".\我的文献库" export report "<task_id>" --format docx --out ".\研究报告.docx"
.\lit.cmd --workspace ".\我的文献库" export report "<task_id>" --format json --out ".\研究报告.json"
```

Agent 动态调用 search / read / excerpt / finish，长原文可分段读取；每次运行有步骤上限，暂停或调用失败后可恢复。原文校订后须重新读取，才可摘录新版。报告结论必须关联当前任务的证据，无法用不存在、其他任务、已拒绝或待复核的证据提交结论。程序验证出处关系，研究结论是否成立仍需人工判断。

远程模型会收到研究问题、研究方案、检索片段、所读文字及任务记录。只有显式运行 research / resume 才调用模型；导入不将整本资料上传。使用本地兼容服务时，例如 `base_url = "http://127.0.0.1:11434/v1"`，填写本机已部署的模型名，并按服务要求设置密钥；不要求鉴权的服务可设环境变量值为 `local`。普通电脑也可以本地 OCR、远程 API 推理。

任务保存启动时的模型地址、模型名、方案和工具配置快照。改变 config 不会改变旧任务；密钥值每次从同名环境变量读取，可以更新。需要更换模型服务时启动新任务。`--max-steps` 表示本次新增运行步数，步骤编号累计。

## 调整研究方案

内置 `general`、`heritage`。复制 `examples/custom-profile.toml`，修改分类、提示词、术语别名和报告章节：

```powershell
.\lit.cmd --workspace ".\我的文献库" research "你的问题" --profile ".\examples\custom-profile.toml"
```

这是工作流与提示词配置，不训练模型权重。文献库配置还可以设置 `agent.allowed_tools`、`search_limit` 和 `max_steps`。Agent 不能执行 shell、任意读文件或访问任意网址；文献正文作为待分析资料处理。

## 环境检查与验证

```powershell
.\lit.cmd --workspace ".\我的文献库" doctor
uv sync --python 3.12 --extra dev --extra ocr
uv run --extra dev --extra ocr python scripts/make_demo.py --out .\demo-input
uv run --extra dev --extra ocr python scripts/smoke_ocr.py --workdir .\ocr-smoke
uv run --extra dev --extra ocr python -m pytest -q
```

演示资料均为软件测试用合成数据，不是真实县志。OCR 缺依赖时安装 `ocr` extra；合成图片缺中文字体时使用 `--font` 指定 TTF/TTC。`.doc` 请先用 Word/WPS 另存为 `.docx`。API 错误可通过 tasks 查看状态，修复网络或环境变量后 resume。

备份时停止写入操作，复制整个文献库目录，保留数据库、config.toml、sources 原件与 artifacts。首版按单用户串行操作设计，不要同时恢复同一个任务。

## 当前边界

支持现代横排中文资料；本地检索是忽略空白的字面匹配，尚无向量检索。扫描倾斜、模糊、复杂分栏、表格结构及跨页文本仍需核查；自动 OCR 判定使用文字量阈值，可手动 force。书目信息与印刷页码需要人工补充，Word 创建年份不会当作出版年。

已做合成资料及本地模拟 HTTP 服务测试，并在真实浏览器完成 6 份 PDF、91 页的导入、识别、检索和摘录闭环。专题集合及四种资料导出已通过独立验收：实际下载并解析四种文件，Word 另逐页渲染检查，服务重启后可继续下载。见 [本轮验收记录](docs/web-task6-acceptance.md)。

真实县志的量化识别准确率、真实供应商兼容性和合计 1000 页的长书性能尚未验收。未实现多人协作、训练型微调或复杂版面还原；网页端的 AI 研究与报告生成仍未接入。

设计见 `docs/design.md`，验收记录见 `docs/verification.md`。

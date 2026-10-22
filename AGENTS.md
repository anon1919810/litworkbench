# 交给 AI Agent 的作业说明（litworkbench）

> 这份文档是写给**使用者的 AI Agent**的。人只需要把仓库地址给它，让它先读完本文件再动手。

## 0. 这个工具是什么，你不是什么

`litworkbench` 是一个**本地优先的中文文献工作台**：把 PDF/DOCX 地方志导入本机库，做 OCR、全文检索、原页核对、精确摘录，最后导出结构化表格。

你的角色：**操作者 + 抽取者**。你通过 CLI 操作文献库，按人给的格式把原文变成结构化数据行。

你不是：
- 不是文献库本身 —— 数据在 `--workspace` 指定的本地目录里（SQLite + 原件），**不要**直接读写 `litworkbench.sqlite3`，一切走 CLI。
- 不是引用来源 —— 你写进表格的每一句引文，都必须来自 `lit read` 返回的原文，或人来确认过的外部著作。

## 1. 部署（一次性）

```bash
git clone https://github.com/anon1919810/litworkbench.git
cd litworkbench
uv sync --python 3.12 --extra ocr --extra web --extra xlsx
uv run --extra ocr --extra web --extra xlsx lit --help   # 看到中文帮助即成功
```

只跑命令行可以去掉 `--extra web`，也不需要 Node 与前端构建。

自检：`lit --workspace <库目录> doctor`，必须 exit 0。

## 2. 一切操作都是 CLI + JSON

- 结构化的结果走 **stdout（JSON）**，进度与错误走 **stderr**；`--workspace` 放在子命令前。
- 退出码非 0 就是失败，**不要**当成功继续写表格。
- 命令清单：`init / import / list / search / read / render / metadata / label / correct / doctor / make-demo / excerpt / research / resume / tasks / export / proofread`。

## 3. 标准作业流程（把一批县志变成一张文化要素表）

```bash
lit --workspace ./库 init
lit --workspace ./库 import "D:\资料\某县志.pdf"        # 扫描件自动走 OCR，逐页可恢复
lit --workspace ./库 list --units --status pending        # 还有哪些页没搞定
lit --workspace ./库 search "灯会"                        # 字面匹配，不做语义猜词
lit --workspace ./库 read "<unit_id>"                     # 拿原文（引文的唯一合法来源）
lit --workspace ./库 render "<unit_id>"                   # 需要时出页图，供人核对
lit --workspace ./库 excerpt save "<unit_id>" --quote "从 read 结果原样复制的片段" --category "民俗"
lit --workspace ./库 proofread --doc <doc_id>            # 让模型提校订候选（不覆盖原文）
lit --workspace ./库 proofread --list --status pending
lit --workspace ./库 proofread --accept <pf_xxx>         # 人来定，不要替他点
lit --workspace ./库 export xlsx --out .\exports\证据表.xlsx
lit --workspace ./库 export xlsx --schemas               # 机器可读列契约
```

`proofread` 的候选**只在人确认后**才写入校订，原始识别结果与历史永远保留。**不要批量自行 accept。**

## 4. 你要产出的表：文化要素（8 列）

| 列 | 类型 | 必填 | 规则 |
|---|---|---|---|
| 序号 | 数值 | 是 | 1..n 连续，不留空 |
| 名称 | 文本 | 是 | 要素名，可带括注（例：夹纻胎（脱胎/重布胎）） |
| 类别 | 枚举 | 是 | 只能取 `物质` / `精神` / `行为` / `制度` |
| 时间 | 文本 | 是 | `朝代（纪年区间）`，例 `战国（BC475-221）`；不确定写 `不详` |
| 空间 | 文本 | 是 | 省市级别地名，例 `湖北省荆州市`；跨区写 `湖北省及全国` |
| 流域 | 文本 | 是 | 多值用**中文分号** `；` 连接，例 `长江中游；汉江中游` |
| 基础信息 | 长文本 | 是 | 该要素的解释，几十到数百字，自己组织语言，不得编造事实 |
| 历史文献 | 文本 | 是 | 见下节，**一格只写一个最优来源** |

### 历史文献：四取一，按优先级

格式**只有一种**：`《书名·卷次》：“原文”` 或 `人名《篇名》：“原文”`。

1. **人** —— 有明确责任人：`黄宗羲《南雷文定·王征南墓志铭》：“有所谓内家者，以静制动…”`
2. **著作** —— 有署名著作：`《中国地域文化通览·湖北卷》：“春秋时期，楚国漆器的胎骨只有厚木胎一种。”`
3. **古籍** —— 古籍带卷次：`《清史稿·艺文志》：“…”`
4. **本志** —— 上面都找不到，直接引本书，**照样写书名+卷次**：`《十堰市志·卷二十文化》：“回龙寺背倚秀丽的青山…”`

**禁止**：写网址；写 `〔原文〕` 之类标记；只有引号没有书名；书名号里缺卷次；一格塞两个来源。

你可以先联网检索更权威的来源（人名/著作/古籍），但**落进表格的是文字证据**，不是链接；不得编造书名、篇名、卷次或引文。

## 5. 硬规则（违反即作废）

1. **引文只来自读过的东西**：`lit read` 的 `original_text` 或人明确给的外部引文；不许根据常识补写。
2. **偏移必须对得上**：保存摘录时给的字符区间与原文切片必须逐字一致，不一致就重读，不要猜。
3. **不替人做决定**：`proofread --accept` / 摘录核验状态 / 删除，都要人点头。
4. **不改库**：不直接 UPDATE/DELETE SQLite，不改 `config.toml` 里的密钥；密钥只从环境变量读。
5. **不确定就留空并进待核**：缺口写进交付说明，不要用看起来对的内容填满。

## 6. 交付时要给人什么

- 一张 `文化要素` 表（xlsx，8 列，冻结首行，类别列可下拉），或 CSV。
- 一段简报：处理了几本文献、几页、产出几行、**待核清单**（哪几条引文/时间/空间存疑）、哪些页 OCR 失败。
- 复核入口：每行都能回到 `书名 + 文件页 + 印刷页`，人能自己 `lit render` 出原页比对。

## 7. 常见坑

- Windows 控制台乱码是显示问题，文件与 JSON 都是 UTF-8。
- `export xlsx` 报缺 openpyxl ⇒ `uv sync --extra xlsx` 或 `uv pip install openpyxl`。
- 扫描件识别慢属正常（单页秒级到数秒）；中断后用 `lit resume` 接着跑，不会重头再来。
- 检索是**字面匹配**：生僻异体字搜不到时，换常见写法或搜片段，不要断定「书里没有」。
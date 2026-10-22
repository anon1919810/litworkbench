# OCR 候选采用验收（Task D 内部验收）

日期：2026-10-16。**状态：草稿，尚未执行真实 12 页验收。**

本文件描述 OCR 候选**采用**路径的内部验收方式与判定标准。对应的可运行脚本是
`scripts/accept_ocr_candidates.py`。本文与计划
`docs/superpowers/plans/2026-10-16-ocr-candidate-review.md` 的 Task D 对应，接口与
状态语义以 `docs/web-ocr-candidate-contract.md` 为准。

> 尚未进行的部分：真实 RapidOCR 生成 12 页候选、采用与并发冲突的**执行**结果。
> 这些必须在 B1（真实 OCR profile / 逐页 worker / 租约队列）集成且 A/B0 验收之后，
> 由 root 在冻结克隆上用 `--execute` 运行。在那之前，脚本只通过了 `--help`、
> dry-run 与安全守卫单测。

## 目标与边界

验收要在**独立克隆**上证明：真实候选能生成、能采用、能保留人工校订、并发变更会
409、重复采用幂等，且整个过程**不改动现用正文与既有记录**，最后给出机器可读的
JSON 报告与简明 Markdown 报告。

冻结输入：

- 克隆：`work/ocr-candidates-2026-10-16/acceptance-library`（`acceptance-library` 的前置
  快照在 `work/ocr-candidates-2026-10-16/before/`）。
- 样本：`work/ocr-quality/2026-10-16/samples.json`，固定 12 页 / 6 篇 PDF / 91 个既有
  单元 / 6 个来源 PDF（来源 SHA-256 内嵌于样本）。

未纳入本脚本、必须另行验证的边界：

| 层面 | 说明 |
| --- | --- |
| 测试夹具 | `tests/` 用 `litworkbench.demo` 合成 PDF 与注入/伪 OCR，只证明编排与契约，**不**证明真实 RapidOCR/模型。 |
| 真实边界 | 本脚本在冻结克隆上跑真实 `CandidateStore` + `Worker` + 本机已装 RapidOCR 权重；不注入 executor、不伪造结果。 |
| 浏览器 | 网页交互（生成/等待/对照/采用/保留/409/失败说明、截图）由 root 用真实 API + 浏览器另行验证，本脚本不覆盖。 |
| 补充场景 | 为覆盖校订/空白分支而额外生成的候选属**补充场景**，计数与冻结 12 页分开报告。 |
| 范围外 | medium、双栏、人工金标准、坏文本层/表格、1000 页容量仍明确待验收，本脚本不扩成已通过能力。 |

## 运行方式

脚本只使用与产品相同的 API（`CandidateStore` / `Worker` / `evidence` / `collections`
/ `search_index` / `ingest.search`）与标准库，不 import 评测脚本，不启服务器、不装
依赖、不下模型。

默认 dry-run（只读，不 `Workspace.open`、不迁移、不写克隆、不生成候选）：

```
python -B scripts/accept_ocr_candidates.py \
    --workspace  <...>/work/ocr-candidates-2026-10-16/acceptance-library \
    --samples    <...>/work/ocr-quality/2026-10-16/samples.json
```

真实执行（**仅 B1 集成完成、A/B0 验收后由 root 运行**）：

```
python -B scripts/accept_ocr_candidates.py \
    --workspace  <...>/work/ocr-candidates-2026-10-16/acceptance-library \
    --samples    <...>/work/ocr-quality/2026-10-16/samples.json \
    --execute
```

参数：

- `--workspace`（必填）：冻结克隆路径（必须等于唯一批准克隆）。
- `--samples`（必填）：冻结 `samples.json`。
- `--output`（可选）：输出目录，须在批准目录（克隆父目录）之下；默认克隆父目录。
  `--execute` 时写出 `taskd-acceptance.json` 与 `taskd-acceptance.md`；dry-run 只打印到 stdout。
- `--execute`（可选）：真正运行写入路径；缺省即 dry-run。

## 安全守卫（fail-closed）

**写前守卫**在 `Workspace.open`（迁移/写入）之前逐一校验；任一条不满足即拒绝运行
（退出码 2，不做任何写入）。也就是说，来源/结构任何失败都不会触发迁移或入队。

- 路径必须**精确等于**唯一批准克隆：脚本按自身位置推导出
  `<仓库旁>/work/ocr-candidates-2026-10-16/acceptance-library`，`--workspace` 与之不等即
  拒绝——即使目录名形状相同但位于别处也拒绝；且目录内必须有 `config.toml` 与 SQLite。
- `config.toml` 的 `db_file` 必须解析到克隆目录之内（拒绝绝对路径与 `../` 逃逸）。
- 拒绝真实库：路径含 `/outputs/library`，或位于 `samples.json` 的
  `local_config.library_root` 之内，一律拒绝；也拒绝任何原始书籍目录。
- 每个 `source_relative_path` 必须落在克隆 `sources/` 内且不是符号链接。
- 输出目录必须在批准目录（克隆父目录）之下，且不落在克隆/sources 内、路径不含符号链接。
- 6 个来源 PDF 的 SHA-256 必须与 `samples.json` 内嵌哈希逐一相符；来源缺失或对不上
  即拒绝，绝不插入伪造 OCR/历史文本自圆其说。
- 模型指纹：独立重算候选所声明模型文件的 SHA-256；缺指纹、缺路径或对不上都记为
  问题（fail-closed），不接受未经验证的自报。

## 验收阶段

`--execute` 依次执行并记录到报告；每个阶段用 try/except 包裹，意外异常会作为结构化
失败写入报告而不会吞掉整轮（仍给出报告并返回非零）：

1. **stage1_clone**：来源哈希、文献数（6）、单元数（91）、样本数（12）、样本页在克隆
   中可解析。**任何失败都在打开克隆前终止**。dry-run 只做本阶段。
2. **stage2_generation**：对 12 个样本页按文献建 `ocr_candidate` 作业，驱动 `Worker.tick()`
   至终态；记录每个候选的 source/page/model/config 指纹与资源，核对 `ready` 非空、
   `failed` 有真实错误类型、生成期间修订号未变，并验证生成**未改动**现用
   units/evidence/collections/documents/index_revision。
3. **stage3_adopt**：选一个「文字确有变化且无人工校订」的冻结候选，先存精确切片摘录
   并建立集合，再按当前修订号采用；核对底稿更新、修订号推进、留下采用前完整历史与
   旧 OCR 快照、摘录版本快照不变且因原文变化转为待复核；重建索引后 FTS 与旧检索均
   命中新文字、不再命中被替换的旧文字；重复采用幂等（不额外写历史/快照）。
4. **stage4_correction**：**补充场景**。先写人工校订再生成补充候选并采用：
   - 校订保持：采用只更新机器底稿，`corrected_text` 与有效 `text` 不变，校订版摘录
     仍为 accepted；
   - 空白校订：采用后空校订保留、有效文字为空、页面保持待定（`status=pending`）。
5. **stage5_cas_reject**：构造并发校订后再采用，要求 `EditConflictError`（409）且候选
   状态、快照、采用历史均未被写入；丢弃（reject）候选不得改动正文或修订号。另逐项
   核对生成失败页的现用 status/底稿/校订与基线**一致**（不要求旧页处于 completed，
   待定旧页同样可安全失败；无失败则记为 observational/unavailable）。
6. **stage6_integrity**：既有 9 个终态任务、documents、既有集合及成员关系保持不变；
   root 进程不残留 RapidOCR 引擎单例。

**场景状态由该阶段实际失败推导**（有失败即 fail），不再无条件记 pass；必需场景若证据
不足（无匹配候选、样本页无法入队等）一律记**失败**并返回非零，绝不补造数据；仅
「本次没有生成失败页可核对」视为 observational，不判通过也不判失败。

## 退出码与报告

| 退出码 | 含义 |
| --- | --- |
| 0 | 所需验收全部通过（dry-run 下为只读结构校验通过）。 |
| 1 | 必需的验收项失败（fail-closed 已记入 `failures`）。 |
| 2 | 安全守卫不满足，拒绝运行。 |

`--execute` 输出（默认在克隆父目录）：`taskd-acceptance.json`（机器可读，含期望来源
哈希、逐项 source hash 校验、场景、阶段明细、失败/警告清单、`required_acceptance_passed`
与 fixture 边界）与 `taskd-acceptance.md`（简明人读版）。

## 安全守卫单测

`tests/test_accept_ocr_candidates_guard.py` 以 importlib 从 `scripts/` 载入脚本，仅验证
只读守卫（不触发真实 OCR/迁移/写入）：

- `resolve_clone` 接受注入的批准根；拒绝 `None`、缺 `config.toml`/SQLite，以及
  **形状相同但非批准路径**的克隆（防止任意同形目录）。
- `db_path_for` 拒绝绝对路径与 `../` 逃逸；`validate_source_paths` 拒绝逃出 `sources/`
  或为符号链接的来源。
- `assert_not_real_library` 拒绝 `.../outputs/library`、拒绝落在 `library_root` 之内的路径。
- `resolve_output` 接受批准目录及其子目录；拒绝批准目录之外、克隆之内或 `sources` 之内。
- `expected_source_hashes` 接受 6 个唯一来源，拒绝：数量不是 6、哈希重复、哈希长度非法、
  来源路径缺失。
- **写前守卫回归**：`run_execute` 在来源不匹配时**不调用** `Workspace.open`（monkeypatch
  记录调用）；`main` 在输出目录越界时返回 2 且不进入任何阶段。
- `verify_clone_sources` 区分命中与缺失；`sha256_file` 与 `hashlib` 一致。

运行：

```
python -B -m pytest -q -p no:cacheprovider tests/test_accept_ocr_candidates_guard.py
```

## 待办（尚未完成）

- [ ] B1 集成真实 OCR profile / 逐页 worker / 租约队列后，在冻结克隆上运行
      `--execute`，产出真实 `taskd-acceptance.json` / `taskd-acceptance.md`。
- [ ] 记录 12 页的 source/page/model/config 指纹与资源到报告，12 页均计成功/失败。
- [ ] root 独立复核采用、校订保留、空白校订、并发 409、重复幂等与失败说明。
- [ ] 浏览器端真实生成/等待/对照/采用/保留/409/失败截图（另案）。
- [ ] 仅在以上通过后，正式库 SQLite backup + 增量迁移；旧字段逐表比对相等。

本文件在真实 12 页验收完成前不构成通过结论。

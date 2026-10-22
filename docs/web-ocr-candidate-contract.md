# OCR 候选网页接口契约

2026-10-16。供本轮前后端独立施工，优先于执行者自行猜测的响应包裹。设计见 superpowers/specs/2026-10-16-ocr-candidate-review-design.md。全部接口前缀 `/api/v1`，沿用既有会话、Origin、CSRF、错误信封及分页。

## 配置与创建

`GET /ocr/profiles` → `{ "items": [{ "id": "small-lowmem-v1", "label": "省资源配置", "description": "逐页处理、减小单次识字批次", "available": true, "reason": null }, ...] }`。

仅 compatible-v1、small-lowmem-v1；后端真实检测本机依赖/模型/资源保护。available=false 时 reason 可读，网页禁用该项，不回传绝对模型路径。第一阶段 A 可先提供静态能力占位，B 必须补真实检查。

`POST /documents/{doc_id}/ocr-candidate-jobs` 请求 `{ "unit_ids": ["..."], "profile_id": "small-lowmem-v1", "layout": "original" }` → HTTP 202，**直接返回既有 JobItem 信封** `{ "id": "job_...", "kind": "ocr_candidate", ... }`，不使用 job_ids 数组或嵌套 job。一份文献一个任务，重复 unit_id 去重后 1–1000 页。

JobItem 的新可选字段 `review_pending_pages: number` 只计 ready 候选；completed_pages 是推理完成页数，不能当作已人工采用。`GET /jobs/{id}/units` 沿用旧分页，每项可增加 `candidate_id`、`candidate_state` 供待核导航。既有 pause/resume/retry-failed 接口复用。

## 候选与决定

`GET /units/{unit_id}/ocr-candidates?page=1&page_size=30&job_id=...` → 标准分页 `{ items, total, page, page_size }`，可附 total_pages；默认最新在前。job_id 可省略。

列表每项是候选摘要，可省略 `result` 和 `error`；选中后通过详情接口获取完整结果。若响应已含完整结果可直接使用。未取得详情时不能把空白占位当作完整候选供采用。

`GET /ocr-candidates/{id}` → 单条详情：

```json
{
  "id": "candidate_...", "job_id": "job_...", "attempt_id": "attempt_...",
  "unit_id": "...", "doc_id": "...", "file_page": 7,
  "state": "ready", "base_content_revision": 0,
  "profile_id": "small-lowmem-v1", "layout": "original",
  "result": {
    "text": "候选识别文字", "lines": [], "engine": "rapidocr",
    "model_fingerprints": [], "config": {}, "timing": {}, "memory": {},
    "coordinate_system": "render_pixels", "render_width": 1000, "render_height": 1400
  },
  "error": null, "created_at": "...", "decided_at": null
}
```

以上数值仅为协议示意，不作为真实识别结果。无结果时 result=null；error 是 `{kind,message}` 或 null。state 为 queued/running/ready/failed/adopted/rejected/stale。真实框/置信度不可用使用 null，不能伪造。

`POST /ocr-candidates/{id}/adopt` 请求 `{ "expected_content_revision": 0 }` → `{ "candidate": CandidateDetail, "unit": UnitDetail }`。expected_content_revision 必填，非负整数；取当前页修订号。后端还要与候选基础修订号比对。409 保留对照与输入，不自动重试覆盖。重复采用同一候选幂等，不重复写历史。

`POST /ocr-candidates/{id}/reject` 请求 `{ "reason": "" }` → CandidateDetail；空原因可接受，重复拒绝幂等，记录不物理删除。

UnitDetail 新增 `content_revision: number`，不改变 text/version/text_sha256 的现用版本语义。有人工校订时采用只更新 original_text/ocr，不清除 corrected_text；返回 unit.text 仍是人工校订。

## 前端行为

- 只从 unit.text 保存正式摘录。候选文字单独渲染，不复用现用 textRef，不能把选择偏移交给正式摘录。
- Reader 的全局“保存摘录”在显示候选/旧 OCR 底稿时同样禁用，并提示切回现用文字；候选面板需把当前文字视图告知 Reader。不能仅隐藏面板内按钮却让顶部按钮对候选选区继续工作。
- 请求按 unit_id 与请求代次隔离；切换页立即清除候选详情/按钮。操作期间可禁用自身按钮，但不能锁整站；晚结果不串页。
- 首次提交后候选列表可能还是空的：Reader 必须知道返回的 job_id，按该任务状态持续刷新，不能只在列表里已有 running 候选时才轮询。ReprocessDialog 的 onQueued 可增加可选的第二参数 JobItem，保持旧单参数调用方兼容；任务完成/失败、翻页或卸载后清理轮询。
- ready 且 base_content_revision=当前页修订号才允许采用；服务端最终裁决。其他状态可查阅，采用禁用。
- 候选页查询支持 job_id；任务页可从已完成 checkpoint 的 candidate_id 导向现有 openReader(unit_id)，读者再展示该页候选列表。第一阶段可按当前页候选最新记录选择，不新增路由系统。
- 差异比较默认现用文字，允许切换旧 OCR；差异不代表纠错成功。前端大文本差异计算必须有界，超过预算退回并排全文并说明，不能截断待采用的真实正文。
- 文件页范围由全部 unit 摘要解析成显式 unit_ids，不把印刷页解释为文件页。提交前展示真实去重页数；超出已知页/Word/空范围在前端解释，后端仍验证。
- 所有 profile、state、中文文案按数据/已知映射显示；无模型不显示假 ready，无自动倒计时或准确率承诺。

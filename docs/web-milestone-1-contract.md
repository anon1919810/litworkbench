# 网页第一里程碑接口契约

2026-10-15，用户已授权。正式 React/TypeScript/Vite 前端与 FastAPI 后端，沿用已批准的 Notion 样式。此文档固定前后端协作接口；实现不使用草图数据。代码未完成前本文件不是成功声明。

## 范围与所有权

- 后端实现者只编辑 Python、tests/web、pyproject/uv.lock、启动器与启动说明；不编辑 web-client 或 design。
- 前端实现者只编辑 web-client 内文件，独立 npm 包和 lock；只读 design/web-prototype/index.html 和本契约；不编辑 Python。
- Codex 管理本契约、集成、独立验证。当前目录非 Git 仓库，开发前快照已保存到 outputs/litworkbench-before-web-20261015.zip。不初始化仓库或提交。
- 先完成真实 PDF 的批量导入、后台逐页 OCR、索引、跨文献检索、原页/文字/对照、选择文字保存摘录与摘录列表。也兼容核心已有 DOCX 文本单元。AI 项目/设置页不显示假数据，说明后续实现即可。

## 公共约定

API 前缀 `/api/v1`。JSON UTF-8；分页 `{items,total,page,page_size}`，默认 30，最大 100。错误 `{error:{code,message,details?}}`，真实 HTTP 状态。ID 为不可解读字符串，前端 encodeURIComponent；不传服务器路径。

`GET /workspace` 建立 HttpOnly/SameSite=Strict 会话 cookie，返回 `{name,root,csrf_token,ocr:{available,detail},coverage}`。前端只在内存保留 csrf_token；写请求含 `X-CSRF-Token`，同源 Origin 校验。其他 API 要求会话 cookie；同源 fetch credentials。`GET /health` 可公开返回 `{status:'ok'}`。同源托管编译文件，开发代理也用同源。

覆盖对象：`{known_pdf_pages,covered_pdf_pages,pending_pdf_pages,failed_pdf_pages,unknown_documents,indexed_docx,total_docx}`。covered 是当前版已索引文字页或人工确认空白页；识别为空先 pending，不能算入覆盖。未知页数不生成假百分比。

## 文献与任务

- `GET /documents?page=1&page_size=30`。item 至少 `{id,title,filename,filetype,page_count,unit_count,status,pending_count,searchable_pages,author,year,edition,volume}`，page_count 可 null。
- `GET /documents/{id}` 同一 item；`PATCH /documents/{id}` 仅可编辑书目字段。
- `GET /documents/{id}/units?page=1&page_size=30` 返回简要单元分页，含 `{id,doc_id,seq,kind,file_page,printed_page,status,method,title_path}`，供逐页跳转；DOCX file_page 为 null。
- `GET /documents/{id}/source` 下载不可变原件。
- `POST /imports`，multipart 重复字段 `files`、字符串 `ocr_mode=auto|off|force`，默认 auto。流式暂存，每文件最多 1GiB。返回 `202 {job_ids,rejected:[{filename,error}]}`；全被拒绝返回 400。路径/文件名按数据处理，basename 同时去除 Windows/Unix 路径。内容哈希去重，不依赖文件名。
- `GET /jobs?page=1&page_size=30`；`GET /jobs/{id}`。item `{id,filename,doc_id,kind,state,stage,total_pages,processed_pages,completed_pages,failed_pages,pending_pages,searchable_pages,duplicate,error,pause_requested,created_at,updated_at}`。doc_id/total_pages 可 null。state 为 queued/preparing/running/pausing/paused/partial/completed/failed；stage 为可读中文。
- `POST /jobs/{id}/pause`、`resume`、`retry-failed`，无 body，幂等返回 job。暂停在页提交安全点生效，关闭浏览器不中止服务内任务，服务重启恢复；重试不重做成功页。
- `POST /documents/{id}/reprocess` body `{unit_ids:[...],mode:'force',keep_corrections:true}`，返回 `202 {job_ids}`。允许已 completed 页强制 OCR，范围外页不动；保留旧结果直到新版本提交，保留人工校订/历史/证据。失败不能毁掉旧可用结果。

## 检索与阅读

- `POST /search` body `{query,mode:'phrase'|'all_terms'|'any_terms',doc_ids?:[],page:1,page_size:30,expected_index_revision?:number}`。返回分页与 `{coverage,index_revision,total_documents}`。item 至少 `{unit_id,doc_id,title,filename,kind,file_page,printed_page,title_path,method,status,snippet,start_offset,end_offset,text_sha256,version}`。别名可后续增加，首版不放虚假扩展按钮。
- 全文索引使用按 Unicode 码点编码的 FTS5 token 和位置短语，保留原文和偏移映射；中文单字、双字、跨行短语及标点均可查。去空白，不改大小写。索引更新事务增加 index_revision；分页代次冲突返回 409/SEARCH_INDEX_CHANGED，前端保留条件重查第一页。
- `GET /coverage` 返回覆盖对象。
- `GET /units/{id}` 返回 `{unit_id,doc_id,title,filename,kind,seq,file_page,printed_page,printed_page_status,title_path,method,status,notes,text,original_text,corrected_text,version,text_sha256,ocr,revisions}`。ocr 为核心真实结构或 null，包含 lines/mean_confidence/render_width/render_height。
- `GET /units/{id}/render?scale=1.5` 返回 PNG，只允许 1/1.5/2/3 四档；PDFium 渲染进程隔离，不能与 OCR 在多个线程同时调用。DOCX 不请求渲染。
- `PATCH /units/{id}/correction` body `{text,reason,expected_hash}`，版本冲突 409，返回更新 unit；当前版索引失效/更新与修订同步。
- `PATCH /units/{id}/printed-page` body `{printed_page,status:'candidate'|'confirmed'}`，返回 unit。不凭文件页自动推算印刷页码。
- `POST /units/{id}/confirm-blank` 显式确认空白，只有无文字待确认页可接受；返回 unit，更新覆盖。

## 摘录

- `POST /evidence` body `{unit_id,quote,start_offset,expected_hash,category,note}`。quote 必须为该版本从该偏移选中的精确切片（同句第二次出现不能保存第一次）。JS UTF-16 选择偏移须转换为 Unicode 码点偏移再发送；409 保留编辑内容。返回 evidence。
- `GET /evidence?page=1&page_size=30&doc_id=&review_status=`。item `{id,unit_id,doc_id,quote,start_offset,end_offset,version,category,note,review_status,review_note,created_at,source}`，source 至少 `{filename,title,file_page,printed_page,title_path}`，可补充已有核心字段。
- `PATCH /evidence/{id}` body `{review_status?:'pending'|'accepted'|'rejected'|'stale',note?,review_note?}` 返回 evidence；列表可返回来源。无永久删除接口。

## 运行与验证

Python 入口 `python -m litworkbench.web --workspace <path> --port 8765 --no-browser`，只监听 127.0.0.1。默认启动器打开浏览器。静态目录解析优先 web-client/dist，再包内 web/static；缺编译产物明确报错。提供可双击的中文命名 .cmd 启动器，默认数据目录 outputs/library（不在代码目录）。用户日常无需 Node；开发时 npm install/build。

本机已有 Python 环境：`C:/Users/杨睿/Documents/Codex/2026-10-11/w-2/work/litworkbench-venv/Scripts/python.exe`。pytest 新测试必须覆盖持久化恢复、两次 claim、不把空/失败页算成功、重复导入不删证据、校订后索引一致、409 与错误范围、含重复引文的精确偏移、中文路径/文件名。

Codex 最后从用户已授权目录 `C:/Users/杨睿/Desktop/个人图书馆/文献提取/书籍` 选择真实扫描/文本 PDF 批量入库，浏览器完成检索与摘录，并重启验证。原目录只读；验收数据库保留在独立 outputs/library。合计1000页为后续容量目标，本轮没有足够真实长书时明确未验证。

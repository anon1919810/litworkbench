# 个人图书馆 · 网页前端（web-client）

本地文献工作台的正式前端：批量导入 → 后台逐页 OCR → 全库检索 → 原页核对 → 保存摘录。
只与本机 Python 服务（`http://127.0.0.1:8765`）的 `/api/v1` 通信，不访问任何外部资源。

## 命令

```powershell
npm.cmd install     # 安装依赖（生成/更新 package-lock.json）
npm.cmd run build   # tsc -b + vite build → dist/（后端同源托管）
npm.cmd run test    # vitest：纯逻辑与钩子单元测试（102 个）
npm.cmd run dev     # 本地开发，/api 代理到 127.0.0.1:8765
npm.cmd run typecheck
```

日常使用不需要 Node：`dist/` 编译产物随 Python 包分发，由启动器同源提供。

## 结构

```
src/
  api/          client.ts（会话握手 / csrf / 401 重新握手 / 错误封装）、endpoints.ts（契约端点）、types.ts
  lib/          unicode.ts（UTF-16 ↔ 码点偏移）、selection.ts（DOM 选区偏移）、excerpt.ts（摘录草稿校验）、
                conflict.ts（409 解释）、coverage.ts（覆盖口径）、highlight.ts（片段标词）、format.ts、ocrOverlay.ts、
                evidenceSelection.ts（跨页选择与筛选范围）、exportScope.ts（导出范围与预览令牌状态机）
  hooks/        useApiQuery、usePolling（前台 2s / 后台 10s）、useSearchResults、useLibraryData、useSidebarStats
  state/        AppState.tsx：工作区信息、覆盖、导航目标、提示条、数据版本号
  components/   外壳（Sidebar / TopBar / Modal / Tag / Toast）、检索结果列表、原件视图、纯文本阅读元素、图标
  pages/        SearchPage、Reader（原件/识别文字/对照 + 校订 + 摘录）、LibraryPage、ImportDialog、JobsPage、
                ExcerptsPage（筛选/跨页选择/批量/导出）、CollectionsDialog、ExportDialog、ProjectsPage、
                SettingsPage 及各对话框
  styles/       tokens / base / shell / ui / search / pages（Notion 工作区风格，无外部字体）
```

## 关键实现约定

- **会话**：`GET /api/v1/workspace` 建立 HttpOnly 会话 cookie；`csrf_token` 只存模块内存，写请求带 `X-CSRF-Token`。
  服务重启导致 401 时会丢弃旧令牌并重新握手，再重试一次（`client.test.ts` 覆盖该路径）。
  **不使用 localStorage**：筛选、选择与对话框状态都是组件内存状态，刷新即回到默认值。
- **选择与范围**：`lib/evidenceSelection.ts` 是纯逻辑。翻页**保留**选择（范围键不含页码），
  换筛选（文献/分类/集合/核验状态）**清空**选择并说明清了多少条。
  `useApiQuery` 在 key 变化、旧行还在显示的窗口里返回 `stale=true`，
  此时勾选框与批量按钮禁用——旧行不对应当前条件，不能拿去批量改或导出。
- **导出对话框**：`lib/exportScope.ts` 是纯状态机。范围/格式/核验口径任一变化 → 预览令牌立即作废，
  「生成」回到禁用；**迟到的预览响应会被丢弃**，不会把按钮重新点亮；
  409 时保留设置、清空令牌并立刻重新预览。默认只导出 accepted，
  显式选「全部核验状态」才含待核验/需复核/已拒绝。请求体只有格式、范围与令牌，
  没有任何服务器路径；下载用服务端返回的固定 URL + `<a download>`。
- **摘录偏移**：点击「保存摘录」的那一刻读取 DOM Selection（模态框会清空选区，必须提前取），UTF-16 偏移先换算为
  Unicode 码点偏移，quote 是该偏移处的精确切片；同一句多次出现时用下拉框指定第几次，绝不用 `indexOf` 的首次命中代替。
- **检索分页**：结果、总数、命中文献数与覆盖来自同一次响应；翻页携带 `expected_index_revision`，
  收到 409 `SEARCH_INDEX_CHANGED` 时保留条件、清空代次、回到第一页重查；查询词、检索方式或来源范围一变就回到第一页；
  旧请求被取消并丢弃，清空查询后迟到的响应也不会把旧结果写回。
- **轮询**：只有相关页面才轮询（识别任务 2s/10s、展开的任务逐页检查点、文献库有待处理页时），卸载时清理定时器、页面隐藏时降频。
  侧栏在存在活动任务时以 5s/15s 同步任务状态、文献计数与 `/api/v1/coverage`，并在最后一个任务结束时收尾刷新一次；
  顶栏覆盖、侧栏徽标与文献库计数因此不会停在旧值上，覆盖数值一律来自服务端，前端不合成。
- **逐页检查点**：`GET /jobs/{id}/units` 的 `position` 是 0 起序号，按文献类型显示“文件第 N 页”或“文档单元 N”
  （Word 的段落/表格单元不冒充物理页码）；`work` 表示尚未处理、可能还没有单元行，因此不允许打开阅读区。
- **上传**：用 XMLHttpRequest 取得真实的上传字节进度；识别进度另由服务端任务状态给出，两条进度不混用。
  文件只发往同源 `/api/v1/imports`。
- **原件**：按当前单元 + 所选比例（1 / 1.5 / 2 / 3）懒加载 PNG，一次只加载一页；
  OCR 行框用服务端返回的 `box` 多边形换算，缺坐标、越界或文字已校订时一律不画，并说明原因。

## 视觉

沿用已批准的 Notion 风格草图（`design/web-prototype/index.html`）：240px `#F7F7F5` 侧栏、白色画布、`#37352F` 文字、
`#E9E9E7` 分隔线、`#2383E2` 主操作、系统无衬线字；45px 面包屑顶栏、28–32px 页面标题、310px 结果列表 + 右侧阅读区；
≤850px 侧栏变抽屉、结果与详情单栏切换；页面整体无横向溢出，表格内部可滚动。

矮窗口（1024×768、871×764 等）下的阅读下限：结果/详情分区 `min-height: 340px`、正文 `.panel` `min-height: 180px`，
页头、标签页与来源条都固定高度不参与压缩；空间仍不够时由工作区整体纵向滚动，而不是把正文压成 0 高。
长文件名在标题里最多两行（完整文件名在属性栏与悬停提示），阅读区窄于 560px 时自动压缩页头与来源条并隐藏页码计数。

## 已知边界（本版本不做）

- 研究项目与 AI 能力：只有说明，没有假数据、假运行或密钥输入框。
- 网页端的 AI 研究报告生成尚未实现；命令行原有的 `lit` Agent 报告导出保持不变。
- 摘录资料导出已实际浏览器下载并独立解析核对，Word 另逐页渲染检查；
  真实集合只有 1 条摘录，跨页选择由自动测试覆盖，1000 页真实长书性能仍未验收。
- 分类筛选是精确匹配的文本输入，没有后端的「分类字典」接口，因此不提供分类下拉的完整枚举。
- 别名扩展与语义检索：界面不提供按钮，也不把语义相近当作命中。
- 检索的来源筛选只列出第一页文献；文献库与摘录集的来源筛选同理。
- 单元级筛选（按状态过滤命中）后端尚未提供，界面不伪造该筛选。
- 双栏阅读顺序的还原与标注由后端核心负责，前端只如实显示返回的文字。

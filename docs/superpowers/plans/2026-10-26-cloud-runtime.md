# 云端个人运行基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans。用户已批准总体设计与 OpenCode 执行方式，Codex 审查与独立验收，不新增逐任务确认。

**Goal:** 同一文献工作区以显式 cloud 模式运行，所有新旧内容入口均需身份，具备可撤销网页会话、Agent令牌、持久化与恢复流程。

**Architecture:** 保留 local 行为；cloud 使用 OIDC 网页登录和 PAT MCP授权，统一为同一 owner。浏览器会话与PAT分别验证，内容业务复用 notes / evidence / Workspace。前端和API由单实例Python进程同源提供，后台OCR仍受既有闸门控制。

**Tech Stack:** FastAPI / SQLite / Authlib（锁定兼容版本）/ uv / Docker（用于Linux部署），现有React。

**Spec:** `docs/superpowers/specs/2026-10-26-cloud-workspace-codex-design.md` 第8–12节；依赖阶段A内容契约。

## Global Constraints

- 已探测的目标为腾讯云 Ubuntu22.04、Docker29，约2GiB内存、39GiB可用盘；已有Plimsoll容器和Nginx80/443，不覆盖其目录、容器、配置或数据库。
- 本计划的代码与测试先本地/隔离Linux验证；生产安装、DNS、外部OIDC真实注册和用户库迁移由Codex在适当部署条件具备后单独执行。
- 配置不完整的cloud模式启动失败，绝不退回local免认证；现有local测试与启动器继续可用。
- 所有API、原件、渲染、资产、导出和MCP都纳入授权；仅极简health、登录流程及公共静态壳允许匿名。
- Owner用固定issuer+subject，OIDC state/nonce/PKCE校验；会话服务端可撤销，cookie HttpOnly/Secure/SameSite=Lax，写入保留CSRF和同源。
- PAT至少256位随机、默认90天、只存HMAC摘要；只读/编辑两类能力，显示一次、可撤销；PAT不登录网页、cookie不授权MCP。
- 不购买资源，不探查/输出现有生产密钥，不使用用户真实文献作测试。

## Review Focus

1. 未登录通过旧source/render/export或SPA回退绕过认证。
2. OIDC回调伪造state/nonce/issuer/subject、重复回调或外站return_to。
3. 恢复后旧会话/PAT仍能访问，或PAT被错误接受为网页cookie。
4. cloud缺配置或挂错空卷时自动创建免认证/空资料库。
5. 服务重启或清理进程时重复worker与长OCR挤占现有应用。

## Task B1: 模式与统一身份

**Files:** Create `src/litworkbench/runtime.py`、`cloud_auth.py`、`web/auth.py`；Modify `web/app.py`、`web/security.py`、`web/statics.py`、`web/__main__.py` 与增量迁移；Create `tests/web/test_cloud_auth.py`、`tests/test_runtime.py`。

**Interfaces:**
- `RuntimeSettings.from_env()`：mode/local默认，cloud必需public_base_url、allowed_hosts、OIDC issuer/client配置、owner subject和session/token摘要密钥；secret从env读取，不回显。
- `Principal(owner_id, actor_id, origin, capabilities)`：由传输层验证后创建，写请求不能通过body伪造来源。
- `cloud_auth`提供持久可撤销session和PAT发行/验证/撤销；不影响notes内容数据事务。
- `/auth/login`、`/auth/callback`、`/auth/logout`；同源return_to；`/api/v1/agent-tokens`创建/列出和按ID撤销。token明文只在创建结果中返回一次，列表不回显。

- [ ] 写失败测试：local兼容；cloud缺配置/错误Host/伪造代理头失败；对旧新每类内容路由匿名401/403；bootstrap不能免登录发会话；cookie/PAT不能互换。
- [ ] 用官方Authlib接入OIDC，测试使用受控本地签名OIDC fixture及HTTP mock，不把mock当供应商实测；验证subject allowlist、state/nonce/PKCE、issuer/audience/时效，回调参数错误不生成会话。
- [ ] 跨请求数据库会话、吊销、重启保持、CSRF与同源规则；新增Authlib依赖和锁条目，不用自写JWT校验器。
- [ ] cloud启动要求库已明确初始化；绝对路径不出现在云端workspace payload。SPA排除`/mcp`、`/auth/`、`/.well-known/`。
- [ ] `.venv/Scripts/python.exe -m pytest tests/test_runtime.py tests/web/test_cloud_auth.py tests/web/test_app.py tests/web/test_launcher.py -q -p no:cacheprovider --basetemp <新建的源码外临时目录>`。

## Task B2: 登录与令牌UI、容器运行

**Files:** Modify `App.tsx`、`state/AppState.tsx`、`api/client.ts`、`pages/SettingsPage.tsx`、`vite.config.ts`；Create auth/agentToken小组件及测试、`Dockerfile`、`deploy/compose.yml`、`deploy/nginx-site.conf.example`、`deploy/.env.example`、`docs/cloud-deployment.md`。

- [ ] 401显示登录动作并保留当前标签页草稿；通过同源顶层导航登录并返回原深链接。local保持重连行为。
- [ ] 令牌管理仅已认证设置页：名称、权限、到期、创建后一次明文、撤销和最小Codex配置说明；不把密钥放localStorage/URL/日志。
- [ ] 多阶段镜像：Node构建dist，Python3.12+uv安装锁定依赖；同源单ASGI worker，独立持久卷，不运行Vite开发服务器。
- [ ] compose只绑定127.0.0.1独立端口；非root应用用户、明确卷权限、healthcheck、日志轮转。生产证书与OIDC值通过部署配置提供，不硬编码已有域名。
- [ ] 2GiB主机先交付关闭长批次OCR的受控部署配置和资源说明；OCR资源上限由实测确定，不能套用超过整机内存的2.5GiB阈值。保留启用已有OCR功能的路径，不伪造“已识别”。
- [ ] 前端测试和build，隔离Linux实际启动、匿名/认证访问、挂载卷持久化与重启验证；Windows构建通过不能替代镜像验收。

## Task B3: 一致备份、迁移与恢复工具

**Files:** Create `src/litworkbench/backup.py`、`scripts/migrate_cloud_workspace.py`、相关tests；复用已有migrations.backup_database，必要时最小提取共用helper。

- [ ] backup API快照+引用不可变文件manifest/SHA256、数量与ID。输出目标不得覆盖已有库、不得落在源资料树或源码树内；不携带认证密钥和可复用会话/PAT。
- [ ] 初次迁移停本地写入/worker，复制原件、staging与任务依赖，保留校订/摘录/候选。迁入非终态任务统一暂停，记录迁移前状态，清理旧主机租约/PID元数据后人工预检续跑；不能恢复的保持暂停并列出缺失项。
- [ ] restore到全新目录，校验哈希/数据库完整性/外键/计数，撤销旧会话/PAT，明确镜像与schema匹配；不直接覆盖生产或自动删除旧数据。
- [ ] 写实际合成库备份恢复测试、缺文件/篡改哈希/错误目标/迁入不自动跑任务测试。
- [ ] 首次真实迁移目录尚未指定，不扫描或导入用户日常资料。

## Self-review与来源

本计划实现cloud身份、运行与备份，远程MCP工具由阶段C复用。旧接口保护和恢复授权在TaskB1/B3都有测试；真实OIDC账号、域名与第二设备验收是部署外部条件，不以fixture冒充。参考 [Authlib Web Clients](https://docs.authlib.org/en/latest/oauth2/client/web/) 与设计列出的SQLite来源；精确库版本安装时验证。

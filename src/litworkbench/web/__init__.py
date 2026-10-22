"""网页工作台的本地 HTTP 服务（第一里程碑）。

模块划分：

* :mod:`litworkbench.web.app`：装配（``create_app``）
* :mod:`litworkbench.web.documents`：文献、单元、上传导入、重新识别
* :mod:`litworkbench.web.jobs`：识别任务状态与控制
* :mod:`litworkbench.web.search`：中文检索与覆盖
* :mod:`litworkbench.web.evidence`：摘录
* :mod:`litworkbench.web.security`：会话、CSRF 与同源校验
* :mod:`litworkbench.web.statics`：同源静态资源与 SPA 回退
* :mod:`litworkbench.web.errors` / ``schemas`` / ``pagination``：中文错误与请求模型
"""

from __future__ import annotations

from litworkbench.web.app import create_app

__all__ = ["create_app"]
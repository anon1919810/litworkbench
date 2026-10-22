"""统一的用户可读错误。

CLI 捕获 :class:`LitError` 之后在 stderr 打印中文提示并返回非零退出码。
错误信息中不允许包含 API 密钥、请求头或服务端原始响应体。
"""

from __future__ import annotations


class LitError(Exception):
    """可以安全展示给用户的错误。"""


class WorkspaceError(LitError):
    """工作区缺失或损坏。"""


class UnsupportedFileError(LitError):
    """不支持的文件类型。"""


class QuotationError(LitError):
    """引文不属于所选版本的原文。"""


class EditConflictError(LitError):
    """乐观并发校验失败：调用方给的 ``expected_hash`` 与当前文字不一致。

    网页服务在同一写事务里「读取当前文字 -> 校验 -> 写入」，因此这个错误
    出现时一定没有产生任何写入；命令行不使用该参数，行为保持不变。
    """


class ValidationError(LitError):
    """请求参数不合法。"""


class NotFoundError(LitError):
    """引用的对象不存在。"""


class ProviderError(LitError):
    """模型服务调用失败（已净化，不含密钥与响应体）。"""


class ProviderTimeoutError(ProviderError):
    """模型服务超时。"""


class ProtocolError(LitError):
    """模型返回的内容不符合 JSON 动作协议。"""


class AgentError(LitError):
    """研究任务无法继续。"""
"""OCR 质量评测工具的共享常量、只读边界与 JSON 读写。

设计约束（对全部子模块生效）：

* 源目录与真实文献库**只读**：输出目录不得是源目录或真实库本身、其子目录，
  也不得是包住这些根目录的上层目录；符号链接按真实路径判定；
* 源文件路径只写在本机配置（``local_config``）里，清单与报告只用相对引用；
* 指标缺失一律写 ``None`` 并附原因，不写 0、不改分母。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
RECORD_VERSION = 1

#: 参考稿核稿级别：``draft`` 只是草稿，``agent_checked`` 是代理核稿，
#: 只有 ``human_checked`` 才是人工核稿；三者都必须如实记录，不能互相顶替。
REVIEW_LEVELS = ("draft", "agent_checked", "human_checked")
FULLPAGE_REVIEWED = ("agent_checked", "human_checked")

READING_ORDER_STATUSES = ("unreviewed", "ok", "wrong", "unavailable")

INVENTORY_FILENAME = "inventory.json"
DRAFT_MANIFEST_FILENAME = "samples.draft.json"
MANIFEST_FILENAME = "samples.json"
DERIVED_DIRNAME = "derived"

DEFAULT_SCALES = (2.0, 3.0, 4.0)


def default_scales() -> tuple[float, ...]:
    return tuple(DEFAULT_SCALES)


def relative_reference(path: Path | str) -> str:
    """清单/报告里的源引用一律用相对路径；不落在项目内则留空。"""
    try:
        return resolve(path).relative_to(project_root()).as_posix()
    except (ValueError, OSError):
        return ""

#: 计划中的样本规模（少于此数只能报告“局部判断”，不得声称质量通过）。
PLAN_SAMPLE_COUNT = 12
PLAN_POSITIVE_QUERIES = 24
PLAN_NEGATIVE_QUERIES = 6

PAGE_TIMEOUT_SECONDS = 180.0
INIT_TIMEOUT_SECONDS = 600.0

#: 候选进程树 RSS 闸门（仅本机筛选，不是所有电脑的资源承诺）。
MAX_CANDIDATE_RSS_BYTES = 8 * 1024 ** 3

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_RUNTIME = 4
EXIT_ERROR = 5


class EvaluationError(RuntimeError):
    """评测工具的错误基类。"""


class SafetyError(EvaluationError):
    """输出路径越界（会写进源目录或真实库）。"""


class ValidationFailure(EvaluationError):
    """清单、样本或参考稿不满足契约。"""


# ------------------------------------------------------------------ 路径


def project_root() -> Path:
    """项目源码根（``outputs/litworkbench``）。"""
    return Path(__file__).resolve().parents[2]


def script_path(name: str) -> Path:
    """本包内的脚本绝对路径（供子进程使用）。"""
    return Path(__file__).resolve().parent / name


def resolve(path: Path | str) -> Path:
    """展开并解析真实路径（跟随符号链接）。"""
    return Path(path).expanduser().resolve()


def path_is_within(path: Path | str, root: Path | str) -> bool:
    """``path`` 是否等于 ``root`` 或位于其下（按真实路径判定）。"""
    try:
        target, base = resolve(path), resolve(root)
    except OSError:  # pragma: no cover - 极端路径
        return False
    return target == base or base in target.parents


def validate_sample_id(value: Any) -> str:
    """A sample id is a filename component, never a path."""
    value = str(value or "")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    if (not value or len(value) > 128 or value.upper() in reserved
            or not all(ch.isalnum() or ch in "_-" for ch in value)):
        raise ValidationFailure(f"非法 sample_id（只允许字母、数字、下划线或连字符）：{value!r}")
    return value


def safe_target(root: Path | str, target: Path | str) -> Path:
    """Resolve the final destination before writing, including existing links."""
    base, destination = resolve(root), resolve(target)
    if destination == base or not path_is_within(destination, base):
        raise SafetyError(f"写入目标越出输出目录：{destination} <- {base}")
    return destination


def is_symlink(path: Path | str) -> bool:
    return Path(path).is_symlink()


def ensure_safe_output(output_root: Path | str, *,
                       library_root: Path | str | None = None,
                       source_root: Path | str | None = None,
                       protected: Iterable[tuple[str, Path | str]] = (),
                       label: str = "输出目录") -> Path:
    """确认输出目录不会覆盖源目录或真实库，否则拒绝。

    对源目录与真实库：拒绝等于它、位于它之下、以及**包住**它的上层目录；
    对 ``protected`` 里的附加保护目录（例如派生文件目录）：只拒绝等于它或位于它之下
    （输出根本来就应当包住自己的派生目录）。符号链接按真实路径判定。
    """
    raw = Path(output_root).expanduser()
    target = resolve(raw)
    roots: list[tuple[str, Path, bool]] = [
        ("源目录", resolve(source_root if source_root is not None else project_root()), True),
    ]
    if library_root is not None:
        roots.append(("真实库", resolve(library_root), True))
    roots.extend((name, resolve(item), False) for name, item in protected)

    for name, root, forbid_enclosing in roots:
        if target == root:
            raise SafetyError(f"{label}不能是{name}：{target}")
        if path_is_within(target, root):
            raise SafetyError(f"{label}不能位于{name}之下：{target} <- {root}")
        if forbid_enclosing and path_is_within(root, target):
            raise SafetyError(f"{label}不能是包住{name}的上层目录：{target} -> {root}")
    return target


# -------------------------------------------------------------- 哈希与 IO


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dump_json(path: Path | str, payload: Any) -> Path:
    target = safe_target(Path(path).parent, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def strip_spaces(text: str) -> str:
    """评分用归一化：**只去空白**，不做同音替换、繁简转换或大小写折叠。"""
    return "".join(ch for ch in text if not ch.isspace())


# ------------------------------------------------------------ 参考稿状态


def reference_review_level(reference: dict | None) -> str:
    level = (reference or {}).get("review_level") or "missing"
    return level if level in REVIEW_LEVELS else "draft"


def reference_is_scorable(reference: dict | None) -> tuple[bool, str]:
    """整页参考稿能否用于 CER；不能时给出可读原因（不生成假 0）。"""
    if not reference:
        return False, "缺少参考稿"
    if not (reference.get("text") or "").strip():
        return False, "参考稿没有文字"
    level = reference_review_level(reference)
    if level not in FULLPAGE_REVIEWED:
        return False, f"参考稿尚未核稿（review_level={level}），不输出整页 CER"
    if reference.get("uncertain_spans"):
        return False, "参考稿存在未决字（uncertain_spans），按整页不计 CER"
    return True, ""


def record_error(kind: str, message: str) -> dict[str, str]:
    return {"kind": kind, "message": message}


def sample_page_key(sample: dict) -> tuple[str, int]:
    return (str(sample.get("doc_id") or ""), int(sample.get("file_page") or 0))


def environment_versions(*packages: str) -> dict[str, str | None]:
    """记录**实际**装上的版本；取不到就写 ``None``，不猜测。"""
    import importlib.metadata as metadata
    import platform
    import sys

    versions: dict[str, str | None] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    for name in packages:
        try:
            versions[name] = metadata.version(name)
        except Exception:
            versions[name] = None
    return versions

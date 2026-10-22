"""允许 ``python -m litworkbench`` 启动 CLI。"""

from __future__ import annotations

import sys

from litworkbench.cli import main

if __name__ == "__main__":  # pragma: no cover - 由命令行触发
    sys.exit(main())
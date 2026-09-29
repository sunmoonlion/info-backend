"""工程结构的自动检查：分层的依赖方向。

契约写在 `pyproject.toml` 的 `[tool.importlinter]`。这里只负责把它接进 `pytest`，
这样不用多记一条命令：分层没守住，测试就不过。
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 另起一个进程跑：检查工具会重设日志配置，放在同一个进程里会让别的测试抓不到日志。
_RUN = (
    "import sys; from importlinter.cli import lint_imports; "
    "sys.exit(lint_imports(config_filename='pyproject.toml', no_cache=True))"
)


def test_layering_contracts_hold() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _RUN],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_four_contracts_are_all_present() -> None:
    """契约被删掉或改名，检查会悄悄变松；这里钉住四条都在。"""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"][
        "importlinter"
    ]
    assert sorted(contract["id"] for contract in config["contracts"]) == [
        "application-not-outer-layers",
        "domain-imports-nothing-outside",
        "inner-layers-no-io-libraries",
        "interface-surfaces-independent",
    ]
    assert config["include_external_packages"] is True

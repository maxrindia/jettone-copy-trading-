"""Each broker module must be importable FIRST in a fresh interpreter.

In-process tests cannot catch a circular import: by the time they run, conftest has
already imported the application in a safe order. This once broke
`import app.services.angel_one.client` (so scripts/angel_capture.py --execute and the
documented local import check failed) because alice_blue.manager imported the broker
builders at module level while those builders' clients import alice_blue.types, which
runs alice_blue/__init__ and therefore the manager.
"""
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

MODULES = [
    "app.services.angel_one.client",
    "app.services.angel_one.capture",
    "app.services.angel_one.instruments",
    "app.services.groww.client",
    "app.services.groww.instruments",
    "app.services.groww.contract",
    "app.services.brokers.registry",
    "app.services.brokers.catalog",
    "app.services.brokers.credential_vault",
    "app.services.brokers.angel_builder",
    "app.services.brokers.groww_builder",
    "app.services.brokers.guards",
    "app.services.brokers.login_guard",
    "app.services.brokers.strict_numbers",
    "app.services.brokers.transport",
    "app.services.brokers",
    "app.services.alice_blue.manager",
    "app.services.copy_engine.pipeline",
    "app.main",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module):
    code = f"import sys; sys.path.insert(0, {str(BACKEND)!r}); import {module}; print('ok')"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(BACKEND))
    assert proc.returncode == 0 and proc.stdout.strip().endswith("ok"), proc.stderr[-800:]

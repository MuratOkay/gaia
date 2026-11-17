"""Every command-line tool imports and builds its parser.

A syntax error in a tool that no test imports would reach the owner's host first
(it happened once, in `make clarifications`, and was caught by lint before delivery).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import gaia.tools

TOOLS = sorted(m.name for m in pkgutil.iter_modules(gaia.tools.__path__))


@pytest.mark.parametrize("name", TOOLS)
def test_the_tool_imports_and_offers_help(name, capsys):
    module = importlib.import_module(f"gaia.tools.{name}")
    main = getattr(module, "main", None)
    if main is None or not inspect.signature(main).parameters:
        return  # a helper module, or a live check with no arguments: importing is the test
    with pytest.raises(SystemExit) as exit_:
        main(["--help"])
    assert exit_.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()

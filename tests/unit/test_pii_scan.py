"""``scripts/pii_scan.py``: the code rule, and ``--include-src`` (``make export``).

The scan leaves ``src`` trees alone by default: code carries fixtures, not data.
A copy that will be handed over is different — nothing in it may hold a live
credential, so ``--include-src`` reads the code too, still with the code rule
(only a credential; a sample address there is a fixture).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pii_scan.py"


def _scan():
    spec = importlib.util.spec_from_file_location("pii_scan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def test_src_is_skipped_unless_asked_and_then_only_a_credential_is_listed(tmp_path, capsys):
    src = tmp_path / "backend" / "src"
    src.mkdir(parents=True)
    secret = "Kx7" + "RealLooking" + "9"  # no literal in this file reads as one
    (src / "mod.py").write_text(
        f'PASSWORD = "{secret}"\n'
        'MAIL = "jane@real.example.net"\n'
        "password: SecretStr\n"
        'credentials: "same-origin"\n', encoding="utf-8")
    scan = _scan()

    assert scan.main([str(tmp_path), "--fail-on-findings"]) == 0
    capsys.readouterr()

    assert scan.main([str(tmp_path), "--include-src", "--fail-on-findings"]) == 1
    captured = capsys.readouterr()
    assert "mod.py" in captured.out
    assert "password 1" in captured.err
    assert "email" not in captured.err  # a sample address in code is a fixture
    assert "secret 1" not in captured.err  # the annotation and fetch mode are not one


def test_the_scan_finds_nothing_in_a_clean_copy(tmp_path, capsys):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("built 2026-11-18; 71 objects, 607 columns\n",
                                               encoding="utf-8")
    scan = _scan()
    assert scan.main([str(tmp_path), "--include-src", "--fail-on-findings"]) == 0
    assert "0 of 1 file(s)" in capsys.readouterr().err


def test_a_masked_shape_is_a_drawing_and_a_prompt_is_code(tmp_path, capsys):
    """A profile's mask alphabet (``a``/``A``/``9``) is nobody's address."""
    (tmp_path / "guide.md").write_text("shapes look like `aaaa.aaaa99@aaaa.aaa`\n",
                                       encoding="utf-8")
    prompts = tmp_path / "backend" / "src" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "reason.md").write_text("guest jane@real.example.net\n", encoding="utf-8")
    scan = _scan()
    assert scan.main([str(tmp_path), "--include-src", "--fail-on-findings"]) == 0
    assert "0 of 2 file(s)" in capsys.readouterr().err


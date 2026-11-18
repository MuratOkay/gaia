"""Every ``make`` target the operator's documents name exists in the Makefile (D95).

D94 shipped ``make feedback`` in the guide, the README and the owner's run — and no such
target: the host's run stopped at "No rule to make target 'feedback'" (OWNER_VERIFICATION §35).
The tool's own test called it directly, so nothing noticed. This reads the documents an
operator follows today; PHASES and the verification history keep plans and past names."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = ("README.md", "USER_GUIDE.md")


def _targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-zA-Z][\w-]*):", text, flags=re.MULTILINE))


def test_every_documented_make_target_exists():
    targets = _targets()
    missing = {}
    for name in DOCUMENTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for target in set(re.findall(r"`make ([a-z][a-z0-9-]*)", text)):
            if target not in targets:
                missing.setdefault(target, []).append(name)
    assert not missing, f"named in the documents, absent from the Makefile: {missing}"


def test_the_feedback_target_runs_the_feedback_tool():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^feedback:.*\n\t.*-m gaia\.tools\.feedback", text, flags=re.MULTILINE)

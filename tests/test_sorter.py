"""Rule matching in examples/mail-sorter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mail-sorter"))

from sort_mail import classify, match, plan  # noqa: E402

RULES = [
    {"name": "both", "from_addr_contains": "github.com", "subject_contains": "security", "folder": "A"},
    {"name": "addr", "from_addr_contains": "github.com", "folder": "B"},
    {"name": "name", "from_name_contains": "ninova", "folder": "C"},
]


def mail(frm, subject=""):
    return {"uid": "1", "from": frm, "subject": subject}


def test_all_conditions_must_hold_and_first_rule_wins():
    assert classify(mail("GitHub <noreply@github.com>", "Security alert"), RULES)["folder"] == "A"
    assert classify(mail("GitHub <noreply@github.com>", "New issue"), RULES)["folder"] == "B"


def test_matching_is_case_insensitive_and_uses_display_name():
    assert classify(mail("NINOVA <ninova@example.edu>"), RULES)["folder"] == "C"
    assert classify(mail("Someone <ninova@example.edu>"), RULES) is None


def test_rule_without_conditions_never_matches():
    assert not match({"folder": "X"}, mail("a@b.c", "anything"))


def test_plan_groups_by_folder():
    groups = plan([mail("x <a@github.com>"), mail("y <b@github.com>"), mail("z <c@d.e>")], RULES)
    assert {k: len(v) for k, v in groups.items()} == {"B": 2}

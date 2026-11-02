import json
from dataclasses import replace
from datetime import datetime

import pytest

from recon.matching import match_documents
from recon.reconcile import reconcile_all
from recon.review import DecisionLog
from recon.rules import RuleConfig
from tests.conftest import mkdoc, mkline

AT = datetime(2026, 10, 30, 9, 0)


def result(qty=99):
    p = mkdoc("PDF", "I/1", [mkline("A", qty, 13900, 10)])
    e = mkdoc("ERP", "I/1", [mkline("A", 99, 13900, 10)], entry_id="E1")
    r, = reconcile_all(match_documents([p], [e]), RuleConfig())
    return r


def test_decision_is_appended_and_current(tmp_path):
    log = DecisionLog(tmp_path)
    r = result(120)
    log.record(r, "Hold — request credit note / corrected invoice", "Ana", at=AT)
    log.record(r, "Escalate", "Budi", "needs CFO", at=AT)
    assert len((tmp_path / "decisions.jsonl").read_text().splitlines()) == 2      # history kept
    st = log.state([r])[r.key]
    assert st.current and st.decision.decision == "Escalate" and st.decision.reviewer == "Budi"


def test_decision_goes_stale_when_the_facts_change(tmp_path):
    log = DecisionLog(tmp_path)
    log.record(result(120), "Approve as booked in ERP", "Ana", at=AT)
    rerun = result(130)                   # same document, different difference after a re-run
    st = log.state([rerun])[rerun.key]
    assert not st.current and "difference was" in st.stale


def test_new_blocking_code_makes_a_decision_stale(tmp_path):
    log = DecisionLog(tmp_path)
    r = result(120)
    log.record(r, "Approve as booked in ERP", "Ana", at=AT)
    st = log.state([replace(r, reasons=r.reasons + ["PO_MISMATCH"])])[r.key]
    assert "new: PO_MISMATCH" in st.stale


def test_legacy_decisions_json_is_read_and_carried_into_the_log(tmp_path):
    r = result(120)
    (tmp_path / "decisions.json").write_text(json.dumps({r.key: {
        "decision": "Escalate", "reviewer": "Ana", "note": "", "at": "2026-10-01 10:00",
        "status": r.status, "difference": r.difference}}))
    log = DecisionLog(tmp_path)
    assert log.state([r])[r.key].current
    log.record(r, "Approve as booked in ERP", "Budi", at=AT)
    assert [d.reviewer for d in log.records()] == ["Ana", "Budi"]


@pytest.mark.parametrize("decision, reviewer", [("Approve as booked in ERP", " "), ("Pay twice", "Ana")])
def test_invalid_decision_is_refused(tmp_path, decision, reviewer):
    with pytest.raises(ValueError):
        DecisionLog(tmp_path).record(result(), decision, reviewer)

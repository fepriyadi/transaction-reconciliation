"""The single entry point: `recon run | ui | check | eval`, and the run_batch.py alias."""
import shutil
import subprocess
import sys

import pytest

from recon import cli
from recon.config import PROJECT_ROOT

from .conftest import ROOT


def test_project_root_is_the_checkout():
    assert PROJECT_ROOT == ROOT and (PROJECT_ROOT / "config" / "rules.json").exists()


def test_check_valid_and_invalid_config(tmp_path, capsys):
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().out.startswith("OK ")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "rules.json").write_text('{"total_tolerance": -1}')
    assert cli.main(["check", str(tmp_path / "config")]) == 1
    assert "rules.json" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed (needed for scanned p.10)")
def test_run_reconciles_the_sample_and_writes_reports(tmp_path, capsys):
    code = cli.main(["run", "--no-llm", "--pdf", str(ROOT / "data/Supplier_Invoices_PT_Prima_Kertas_Sep2026.pdf"),
                     "--erp", str(ROOT / "data/ERP_AP_Export_Sep2026.xlsx"), "--out", str(tmp_path)])
    assert code == 0                                   # fully explained
    assert "unexplained Rp 0" in capsys.readouterr().out
    assert (tmp_path / "reconciliation_report.xlsx").exists()


def test_run_with_invalid_config_exits_3(tmp_path):
    (tmp_path / "rules.json").write_text("{not json")
    assert cli.main(["run", "--pdf", "x.pdf", "--erp", "y.xlsx", "--config", str(tmp_path)]) == 3


def test_unknown_arguments_are_rejected_outside_ui_and_eval():
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--bogus"])
    assert e.value.code == 2


@pytest.mark.parametrize("argv, expected", [
    (["ui", "--server.port=9999"], ["-m", "streamlit", "run", str(ROOT / "app.py"), "--server.port=9999"]),
    (["eval", "--quick"], [str(ROOT / "eval" / "evaluate.py"), "--quick"]),
])
def test_ui_and_eval_run_the_project_scripts(monkeypatch, argv, expected):
    seen = {}

    def fake_call(cmd, cwd):
        seen.update(cmd=cmd, cwd=cwd)
        return 0
    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    assert cli.main(argv) == 0
    assert seen["cmd"] == [sys.executable, *expected] and seen["cwd"] == ROOT


def test_run_batch_py_is_an_alias_of_recon_run():
    out = subprocess.run([sys.executable, str(ROOT / "run_batch.py"), "--help"], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and out.stdout.startswith("usage: recon run")

import json
import shutil

import pytest

from recon.config import Config, ConfigError, LLMConfig, load_profiles
from recon.extract import is_running_text
from recon.extract.llm_reader import parse_page_from_llm
from recon.extract.words import Word
from recon.llm import LLMPage


def test_shipped_config_is_valid(config_dir):
    cfg = Config.load(config_dir)
    assert cfg.profiles and cfg.rules.auto_approve_min_confidence == 0.9


def test_code_defaults_follow_llm_json(config_dir):
    shipped = json.loads((config_dir / "llm.json").read_text())
    assert (LLMConfig().text_model, LLMConfig().vision_model) == (shipped["text_model"], shipped["vision_model"])


def _profile_dir(tmp_path, config_dir, change):
    d = tmp_path / "config"
    shutil.copytree(config_dir, d)
    p = d / "suppliers" / "prima_kertas.json"
    data = json.loads(p.read_text())
    change(data)
    p.write_text(json.dumps(data))
    return d


@pytest.mark.parametrize("change, message", [
    (lambda d: d["layout"].update(foter_margin=45), "foter_margin"),                               # typo
    (lambda d: d["layout"]["header_fields"]["doc_no"].update(pattern="[A-Z"), "invalid regex"),
    (lambda d: d["layout"]["table"]["right_fields"].pop(), "amount"),
    (lambda d: d.update(detect=[]), "detect"),
])
def test_broken_profile_fails_at_load_naming_the_file(tmp_path, config_dir, change, message):
    with pytest.raises(ConfigError, match=message) as e:
        load_profiles(_profile_dir(tmp_path, config_dir, change))
    assert "prima_kertas.json" in str(e.value)


def test_unpriced_model_is_rejected():
    with pytest.raises(ValueError, match="no price"):
        LLMConfig(text_model="x", vision_model="y", prices_usd_per_million_tokens={"x": {"input": 1, "output": 1}})


def test_running_text_margins_follow_the_real_page_height(config_dir):
    prof = load_profiles(config_dir)[0]
    footer, body = Word("batch", 50, 985, 90, 994), Word("Subtotal", 50, 820, 90, 829)
    assert is_running_text(footer, 1008, prof) and not is_running_text(body, 1008, prof)   # taller than A4
    assert is_running_text(Word("x", 50, 811, 60, 820), 841.89, None)                      # A4 default


def test_llm_reply_outside_the_schema_is_not_read():
    page, problem = LLMPage.from_untrusted({"doc_no": "X/1", "lines": "two lines"})
    assert problem and page.doc_no is None
    pp = parse_page_from_llm({"doc_no": "X/1", "lines": "two lines"}, 1, "t.pdf", "llm_text")
    assert not pp.doc_no and pp.notes and "schema" in pp.notes[0]

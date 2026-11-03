"""Configuration schemas, validated once when a run starts.

Every file in config/ has a schema here. A typo in a new supplier profile or a
missing ERP column synonym fails at load time with the file and field named,
instead of deep inside parsing on page 7 of a batch.

    recon check [config_dir]     # validate a config directory (also: python -m recon.config)
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .onboarding import SupplierRegister
from .rules import RuleConfig


# src/recon/config.py -> the repository: config/, data/, app.py and eval/ live there (editable install)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config"


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _fraction(v: str) -> str:
    try:
        Fraction(v)
    except (ValueError, ZeroDivisionError) as e:
        raise ValueError(f"not a fraction: {v!r}") from e
    return v


# --------------------------------------------------------------------------- rules.json

class RulesConfig(_Strict):
    dpp_factor: str = "11/12"
    ppn_rate: str = "12/100"
    total_tolerance: int = Field(10, ge=0)
    tax_rounding_tolerance: int = Field(10, ge=0)
    auto_approve_min_confidence: float = Field(0.9, ge=0, le=1)
    new_supplier_review_days: int = Field(31, ge=0)

    check_fractions = field_validator("dpp_factor", "ppn_rate")(_fraction)

    def rule_config(self) -> RuleConfig:
        return RuleConfig(dpp_factor=Fraction(self.dpp_factor), ppn_rate=Fraction(self.ppn_rate),
                          total_tolerance=self.total_tolerance, tax_rounding_tolerance=self.tax_rounding_tolerance)


# --------------------------------------------------------------------------- erp_mapping.json

class ErpMapping(_Strict):
    header_fields: dict[str, list[str]]
    credit_note_markers: list[str] = []
    line_fields: dict[str, list[str]]
    decimal_separator: Literal[".", ","] = "."
    text_date_order: Literal["DMY", "MDY"] = "MDY"

    @model_validator(mode="after")
    def _required_columns(self):
        for where, have, need in (("header_fields", self.header_fields, {"doc_no", "total"}),
                                  ("line_fields", self.line_fields, {"entry_id", "sku", "qty", "amount"})):
            missing = need - {k for k, syns in have.items() if syns}
            if missing:
                raise ValueError(f"{where} needs synonyms for {sorted(missing)} (sheets are classified by them)")
        return self


# --------------------------------------------------------------------------- llm.json

class ModelPrice(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class LLMConfig(_Strict):
    """Defaults equal config/llm.json, so code and config can't drift apart."""
    text_model: str = "openai/gpt-oss-120b"
    vision_model: str = "meta-models/muse-spark-open"
    fallback_text_model: str = "openai/gpt-oss-20b"        # called only when the main model is rate-limited; "" = none
    fallback_vision_model: str = "qwen/qwen3.8-27b"
    # completion budget of one profile draft: the JSON is ~400-650 tokens, gpt-oss reasoning counts on top of it
    profile_max_completion_tokens: int = Field(default=4096, gt=0)
    prices_usd_per_million_tokens: dict[str, ModelPrice] = {}

    @property
    def models(self) -> list[str]:
        return [m for m in (self.text_model, self.vision_model, self.fallback_text_model, self.fallback_vision_model) if m]

    @model_validator(mode="after")
    def _priced(self):
        if self.prices_usd_per_million_tokens:
            missing = [m for m in self.models if m not in self.prices_usd_per_million_tokens]
            if missing:
                raise ValueError(f"no price for configured model(s) {missing}: the cost estimate would read $0")
        return self

    def price(self, model: str) -> Optional[ModelPrice]:
        return self.prices_usd_per_million_tokens.get(model)


# --------------------------------------------------------------------------- suppliers/*.json

class HeaderFieldSpec(_Strict):
    labels: list[str] = Field(min_length=1)
    pattern: str

    @field_validator("labels")
    @classmethod
    def _text(cls, v: list[str]) -> list[str]:
        if any(not re.search(r"[A-Za-z0-9]", label) for label in v):
            raise ValueError(f"every label needs printed text (an empty label matches every row): {v}")
        return v

    @field_validator("pattern")
    @classmethod
    def _regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
        return v


class RightField(_Strict):
    """One column of the row grammar, matched from the right end of a table row."""
    name: Literal["qty", "uom", "unit_price", "disc_pct", "amount"]
    type: Literal["number", "alpha", "percent"]
    optional: bool = False


class TableSpec(_Strict):
    header_keywords: list[str] = Field(min_length=1)
    sku_header: str
    stop_markers: list[str] = []
    row_reach: float = Field(14, gt=0)
    right_fields: list[RightField] = Field(min_length=1)

    @field_validator("sku_header")
    @classmethod
    def _one_word(cls, v: str) -> str:
        # the parser compares it with single words of the heading row; '#' heads the line numbers, not the items
        if not re.fullmatch(r"\S*[A-Za-z0-9]\S*", v.strip()):
            raise ValueError(f"sku_header must be the one word over the item-code column, e.g. 'Kode' or 'ITEM': {v!r}")
        return v

    @field_validator("right_fields")
    @classmethod
    def _grammar(cls, v: list[RightField]) -> list[RightField]:
        names = [f.name for f in v]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate column in row grammar: {names}")
        if "amount" not in names:
            raise ValueError("row grammar needs an 'amount' column")
        return v


class TotalSpec(_Strict):
    labels: list[str] = Field(min_length=1)


class Layout(_Strict):
    doc_types: dict[Literal["INVOICE", "CREDIT_NOTE"], list[str]]
    header_fields: dict[Literal["doc_no", "doc_date", "due_date", "po_no", "reference"], HeaderFieldSpec]
    table: TableSpec
    totals: dict[Literal["subtotal", "dpp", "ppn", "total"], TotalSpec]
    footer_margin: float = Field(60, ge=0)             # table body stops this far above the page bottom
    running_header_margin: float = Field(40, ge=0)     # text this close to the top edge is a running header
    running_footer_margin: float = Field(42, ge=0)     # ... and this close to the bottom edge a running footer

    @model_validator(mode="after")
    def _doc_no(self):
        if "doc_no" not in self.header_fields:
            raise ValueError("header_fields needs 'doc_no' (pages are stitched into documents by it)")
        if "total" not in self.totals:
            raise ValueError("totals needs 'total'")
        return self


class SupplierProfile(_Strict):
    name: str
    vendor_name: str
    erp_vendor_names: list[str] = []
    detect: list[str] = Field(min_length=1)
    decimal_separator: Literal[",", "."] = ","
    date_order: Literal["DMY", "MDY"] = "DMY"
    page_height: float = Field(842, gt=0)             # fallback when the real page height is not known
    layout: Layout
    file: str = ""                                    # set by the loader


# defaults for pages read before any profile is known
DEFAULT_LAYOUT_MARGINS = (Layout.model_fields["running_header_margin"].default,
                          Layout.model_fields["running_footer_margin"].default)


# --------------------------------------------------------------------------- supplier_register.json

class RegisteredSupplier(_Strict):
    onboarded: date


class RegisterFile(_Strict):
    suppliers: dict[str, RegisteredSupplier] = {}


# --------------------------------------------------------------------------- loading

M = TypeVar("M", bound=BaseModel)


def _read(path: Path, schema: type[M], required: bool = True, **extra) -> M:
    if not path.exists():
        if required:
            raise ConfigError(f"{path}: missing")
        return schema(**extra)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: not valid JSON ({e})") from e
    data = {k: v for k, v in data.items() if not k.startswith("_")}      # "_comment" keys
    try:
        return schema.model_validate({**data, **extra})
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_profiles(config_dir: Path) -> list[SupplierProfile]:
    return [_read(p, SupplierProfile, file=p.name) for p in sorted((Path(config_dir) / "suppliers").glob("*.json"))]


def load_rules(config_dir: Path) -> RulesConfig:
    return _read(Path(config_dir) / "rules.json", RulesConfig)


def load_erp_mapping(config_dir: Path) -> ErpMapping:
    return _read(Path(config_dir) / "erp_mapping.json", ErpMapping)


def load_llm_config(config_dir: Optional[Path]) -> LLMConfig:
    if config_dir is None:
        return LLMConfig()
    return _read(Path(config_dir) / "llm.json", LLMConfig, required=False)


def load_register(config_dir: Path, review_days: int = 31) -> SupplierRegister:
    reg = _read(Path(config_dir) / "supplier_register.json", RegisterFile, required=False)
    return SupplierRegister.from_dates({name: s.onboarded for name, s in reg.suppliers.items()}, review_days)


@dataclass(frozen=True)
class Config:
    dir: Path
    rules: RulesConfig
    erp: ErpMapping
    llm: LLMConfig
    profiles: tuple
    register: SupplierRegister

    @classmethod
    def load(cls, config_dir: Path) -> "Config":
        config_dir = Path(config_dir)
        rules = load_rules(config_dir)
        return cls(dir=config_dir, rules=rules, erp=load_erp_mapping(config_dir), llm=load_llm_config(config_dir),
                   profiles=tuple(load_profiles(config_dir)),
                   register=load_register(config_dir, rules.new_supplier_review_days))

    @property
    def rule_config(self) -> RuleConfig:
        return self.rules.rule_config()


def main(argv=None) -> int:
    """`recon check [dir]`: validate a config directory; exit 1 if it is invalid."""
    args = sys.argv[1:] if argv is None else argv
    d = Path(args[0]) if args else DEFAULT_CONFIG
    try:
        cfg = Config.load(d)
    except ConfigError as e:
        print(f"INVALID {e}", file=sys.stderr)
        return 1
    print(f"OK {d}: {len(cfg.profiles)} supplier profile(s) ({', '.join(p.file for p in cfg.profiles)}), "
          f"{len(cfg.register.onboarded)} registered supplier(s), models {cfg.llm.text_model} / {cfg.llm.vision_model} "
          f"(fallback {cfg.llm.fallback_text_model or 'none'} / {cfg.llm.fallback_vision_model or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

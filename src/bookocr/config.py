"""Configuration loading: default.yaml deep-merged with an optional override file
and CLI --set overrides. Nothing in the pipeline should hard-code a path,
threshold, or engine choice outside of this module's defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(d: dict, dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class RasterizeConfig(BaseModel):
    dpi: int = 300
    image_format: str = "png"


class TriageConfig(BaseModel):
    blank_page_ink_ratio_threshold: float = 0.002
    orientation_check: bool = True


class PreprocessConfig(BaseModel):
    deskew: dict = Field(default_factory=dict)
    denoise: dict = Field(default_factory=dict)
    binarize: dict = Field(default_factory=dict)
    upscale: dict = Field(default_factory=dict)


class LayoutConfig(BaseModel):
    engine: str = "none"


class OcrConfig(BaseModel):
    primary_engine: str = "paddleocr"
    primary: dict = Field(default_factory=dict)
    escalation: dict = Field(default_factory=dict)
    cloud_fallback: dict = Field(default_factory=dict)


class QualityConfig(BaseModel):
    weights: dict
    thresholds: dict


class WatermarkFilterConfig(BaseModel):
    enabled: bool = True
    bottom_band_fraction: float = 0.15
    min_page_occurrences: int = 3
    min_page_fraction: float = 0.3


class ConcurrencyConfig(BaseModel):
    cpu_workers: int = 6
    gpu_stage_batch_size: int = 4


class OutputConfig(BaseModel):
    internal_dirname: str = ".ocr_internal"
    txt_page_marker: str = "===== PAGE {page} ====="
    md_page_heading: str = "## Page {page}"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Config(BaseModel):
    rasterize: RasterizeConfig
    triage: TriageConfig
    preprocess: PreprocessConfig
    layout: LayoutConfig
    ocr: OcrConfig
    quality: QualityConfig
    watermark_filter: WatermarkFilterConfig
    concurrency: ConcurrencyConfig
    output: OutputConfig
    logging: LoggingConfig


def load_config(
    override_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        merged = yaml.safe_load(f)

    if override_path is not None:
        with open(override_path, encoding="utf-8") as f:
            merged = _deep_merge(merged, yaml.safe_load(f) or {})

    for dotted_key, value in (cli_overrides or {}).items():
        _set_path(merged, dotted_key, value)

    return Config.model_validate(merged)

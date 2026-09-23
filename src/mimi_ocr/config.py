"""Configuration loading: default.yaml deep-merged with an optional override file
and CLI --set overrides. Nothing in the pipeline should hard-code a path,
threshold, or engine choice outside of this module's defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr

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


class PathsConfig(BaseModel):
    source_dir: str
    processed_dir: str
    work_dir: str
    state_db: str


class RasterizeConfig(BaseModel):
    dpi: int = 300
    image_format: str = "png"
    keep_rendered_pages: bool = False


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


class ConcurrencyConfig(BaseModel):
    cpu_workers: int = 6
    gpu_stage_batch_size: int = 4


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str


class Config(BaseModel):
    paths: PathsConfig
    rasterize: RasterizeConfig
    triage: TriageConfig
    preprocess: PreprocessConfig
    layout: LayoutConfig
    ocr: OcrConfig
    quality: QualityConfig
    concurrency: ConcurrencyConfig
    logging: LoggingConfig

    # Resolved base dir for turning relative config paths into absolute ones,
    # filled in by load_config().
    _config_dir: Path = PrivateAttr(default=None)  # type: ignore[assignment]

    def resolve_path(self, raw: str) -> Path:
        p = Path(raw)
        if p.is_absolute():
            return p
        return (self._config_dir / p).resolve()


def load_config(
    override_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        merged = yaml.safe_load(f)
    config_dir = DEFAULT_CONFIG_PATH.parent

    if override_path is not None:
        override_path = Path(override_path)
        with open(override_path, encoding="utf-8") as f:
            merged = _deep_merge(merged, yaml.safe_load(f) or {})
        config_dir = override_path.parent

    for dotted_key, value in (cli_overrides or {}).items():
        _set_path(merged, dotted_key, value)

    cfg = Config.model_validate(merged)
    cfg._config_dir = config_dir
    return cfg

"""Adaptive preprocessing: analyze() decides what a page needs, apply() only
does that. Section 9 of the spec is explicit that nothing should be applied
blindly — a clean, born-digital-looking raster (which turned out to be most
of the Phase 0 benchmark sample; see docs/phase0_findings.md) should pass
through nearly untouched, while a genuinely noisy photographic scan should
get the full treatment.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from bookocr.core.interfaces import Preprocessor
from bookocr.core.types import PageImage


class AdaptivePreprocessor(Preprocessor):
    def __init__(self, config: dict):
        self.cfg = config

    def analyze(self, page: PageImage) -> dict:
        img = cv2.imread(page.path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return {"blank": True, "readable": False}

        ink_ratio = float(np.mean(img < 200))
        contrast_std = float(np.std(img))
        skew_deg = self._estimate_skew(img)
        noise_score = self._estimate_noise(img)

        blank_threshold = self.cfg.get("triage", {}).get("blank_page_ink_ratio_threshold", 0.002)

        return {
            "readable": True,
            "blank": ink_ratio < blank_threshold,
            "ink_ratio": ink_ratio,
            "contrast_std": contrast_std,
            "skew_deg": skew_deg,
            "noise_score": noise_score,
            "effective_dpi": page.dpi,
        }

    def apply(self, page: PageImage, analysis: dict) -> PageImage:
        if analysis.get("blank") or not analysis.get("readable", True):
            return page

        img = cv2.imread(page.path, cv2.IMREAD_GRAYSCALE)
        changed = False

        deskew_cfg = self.cfg.get("preprocess", {}).get("deskew", {})
        if deskew_cfg.get("enabled", True) and abs(analysis.get("skew_deg", 0)) >= deskew_cfg.get("min_angle_deg", 0.3):
            img = self._rotate(img, analysis["skew_deg"])
            changed = True

        denoise_cfg = self.cfg.get("preprocess", {}).get("denoise", {})
        if denoise_cfg.get("enabled", True) and analysis.get("noise_score", 0) > denoise_cfg.get("noise_score_threshold", 0.15):
            img = cv2.fastNlMeansDenoising(img, h=10)
            changed = True

        binarize_cfg = self.cfg.get("preprocess", {}).get("binarize", {})
        if binarize_cfg.get("enabled", True) and analysis.get("contrast_std", 999) < binarize_cfg.get("low_contrast_threshold", 40):
            img = cv2.adaptiveThreshold(
                img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
            )
            changed = True

        if not changed:
            return page

        out_path = Path(page.path).with_suffix("")
        out_path = Path(f"{out_path}_pp.png")
        cv2.imwrite(str(out_path), img)
        return PageImage(
            book_id=page.book_id,
            page_number=page.page_number,
            path=str(out_path),
            width=img.shape[1],
            height=img.shape[0],
            dpi=page.dpi,
        )

    @staticmethod
    def _estimate_skew(img: np.ndarray) -> float:
        # Coarse projection-profile skew estimate: fine enough to catch pages
        # that are meaningfully rotated, not meant to be sub-degree precise.
        thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        coords = cv2.findNonZero(thresh)
        if coords is None or len(coords) < 50:
            return 0.0
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        # minAreaRect on a full page of text lines is noisy for small angles;
        # clamp to a plausible scan-skew range.
        return float(angle) if abs(angle) < 15 else 0.0

    @staticmethod
    def _estimate_noise(img: np.ndarray) -> float:
        # High-frequency energy via Laplacian, normalized. Cheap proxy for
        # "is this a speckled/noisy scan" without a reference image.
        lap = cv2.Laplacian(img, cv2.CV_64F)
        return float(np.var(lap)) / 10000.0

    @staticmethod
    def _rotate(img: np.ndarray, angle_deg: float) -> np.ndarray:
        h, w = img.shape[:2]
        center = (w // 2, h // 2)
        m = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

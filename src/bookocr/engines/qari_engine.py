"""QARI-OCR escalation engine (Pass 2).

Not the primary engine -- PaddleOCR is far cheaper and, per the Phase 0
benchmark (docs/phase0_findings.md), already scores HIGH on the large
majority of this library's pages. This engine exists for the minority that
score LOW/CRITICAL: decorative fonts, heavy diacritics, degraded scans --
exactly the failure mode PaddleOCR's classical recognizer struggles with.

Model history, found and fully worked through on 2026-09-23 -- worth
recording because it wasn't obvious and cost real iteration to track down:

NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct is the version with a published
benchmark (arXiv 2506.02295: WER 0.160 / CER 0.061 on a 200-page diacritics
set, beating the Mistral OCR API), but that repo ships as a PEFT LoRA
adapter (`adapter_config.json` + `adapter_model.safetensors`), not a
standalone model. Loading it on top of either of its two plausible bases --
`unsloth/Qwen2-VL-2B-Instruct-unsloth-bnb-4bit` (the adapter's own declared
base) or the non-quantized `unsloth/Qwen2-VL-2B-Instruct` -- logs "missing
adapter keys" for ~650 LoRA weights, every single one in the vision tower
(`visual.blocks.*`). PEFT silently no-ops a LoRA weight whose target module
name doesn't exist rather than erroring, so in both cases the model ran with
its vision-side adaptation not actually applied. In testing this produced
inconsistent, sometimes badly broken output: whole-string-reversed Arabic on
some pages, degenerate repetition loops on others, outright non-text
hallucination (bounding-box-looking coordinate strings) on others. This
looks like a genuinely broken/incompletely-published adapter, not something
fixable by picking a different base repo.

NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct is used instead: it ships a plain
merged `model.safetensors` (standard `Qwen2VLForConditionalGeneration`, no
adapter, no PEFT, no base-matching problem to get wrong). Load it directly.

VLM generation has no native per-character confidence the way PaddleOCR
does. We approximate one from the model's own token probabilities (mean of
the top-1 softmax probability per generated token) rather than inventing a
fixed number -- an under-confident-looking generation (the model wasn't
sure what it was reading) should still be able to score LOW and get flagged
for review, same as a low-confidence Pass 1 page would.

Runs on CPU by default (`ocr.escalation.device`): the GPU path was tried
with the (now-abandoned) 0.2.2.1 adapter and didn't reliably fit this
machine's ~3.68GB of actually-usable VRAM alongside everything else --
revisit on a machine with more VRAM headroom, or once this merged v0.3
checkpoint has itself been VRAM-profiled on GPU.
"""

from __future__ import annotations

from bookocr.core.interfaces import OCREngine
from bookocr.core.types import EngineResult, PageImage, Region

_MODEL_NAME = "NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct"
_PROMPT = (
    "Below is the image of one page of a document. Just return the plain "
    "text representation of this document as if you were reading it "
    "naturally. Do not hallucinate."
)

# Qwen2-VL's commonly-recommended bounds for visual token count. The
# processor's own default max_pixels (12.8MP) is larger than our page
# renders and would not downscale them at all -- fine on CPU/RAM (this is
# not the tight-VRAM GPU path), but keeping a cap bounds latency/memory
# predictably regardless of a page's native resolution.
_MIN_PIXELS = 256 * 28 * 28
_MAX_PIXELS = 1024 * 28 * 28

_model_singleton = None
_processor_singleton = None
_device_singleton = None


def _get_model_and_processor(device: str):
    global _model_singleton, _processor_singleton, _device_singleton
    if _model_singleton is None or _device_singleton != device:
        import torch
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        _model_singleton = Qwen2VLForConditionalGeneration.from_pretrained(_MODEL_NAME, dtype=dtype, device_map={"": (0 if device == "cuda" else "cpu")})
        _processor_singleton = AutoProcessor.from_pretrained(_MODEL_NAME, min_pixels=_MIN_PIXELS, max_pixels=_MAX_PIXELS)
        _device_singleton = device
    return _model_singleton, _processor_singleton


def shutdown() -> None:
    """Free VRAM/RAM. Call before/after using another GPU model in the same
    process (see pipeline.py's two-sweep structure)."""
    global _model_singleton, _processor_singleton, _device_singleton
    if _model_singleton is not None:
        import torch

        del _model_singleton
        _model_singleton = None
        _processor_singleton = None
        _device_singleton = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class QariOCREngine(OCREngine):
    name = "qari-ocr"
    version = "v0.3"

    def __init__(self, config: dict | None = None):
        self.cfg = config or {}

    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        import torch
        from PIL import Image
        from qwen_vl_utils import process_vision_info

        device = self.cfg.get("device", "cpu")
        model, processor = _get_model_and_processor(device)
        image = Image.open(page.path).convert("RGB")

        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": _PROMPT}]}]
        text_prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = processor(text=[text_prompt], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt")
        inputs = inputs.to(model.device)

        with torch.inference_mode():
            out = model.generate(
                **inputs,
                max_new_tokens=self.cfg.get("max_new_tokens", 1024),
                # Mitigates degenerate repetition loops (seen on the earlier,
                # now-abandoned adapter -- kept as a cheap general safeguard).
                # Gentle values: aggressive ones measurably hurt otherwise-
                # correct generations by forcing the model off legitimate
                # short repeats. quality/scorer.py's degenerate-repetition
                # check is the real backstop, not these settings.
                repetition_penalty=1.1,
                no_repeat_ngram_size=6,
                output_scores=True,
                return_dict_in_generate=True,
            )

        input_len = inputs.input_ids.shape[1]
        generated_ids = out.sequences[:, input_len:]
        output_text = processor.batch_decode(generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0].strip()
        confidence = self._mean_token_confidence(out.scores, generated_ids[0])

        region = Region(kind="body", bbox=(0.0, 0.0, float(page.width), float(page.height)), text=output_text, confidence=confidence, reading_order=0)
        return EngineResult(text=output_text, confidence=confidence, engine_name=self.name, engine_version=self.version, regions=[region], raw={})

    @staticmethod
    def _mean_token_confidence(scores, generated_ids) -> float:
        import torch

        if not scores:
            return 0.0
        probs = []
        for step_logits, token_id in zip(scores, generated_ids):
            p = torch.softmax(step_logits[0], dim=-1)[token_id].item()
            probs.append(p)
        return round(sum(probs) / len(probs) * 100, 2) if probs else 0.0

"""Synthetic ground-truth benchmark: render known Arabic text in several
fonts, run it through the real `ocr convert` pipeline, and measure exact
error rates against the text that was rendered.

Why synthetic: a real book page has no machine-checkable reference text, and
transcribing pages of published books by hand is slow and error-prone. Text
rendered by us has a perfect, free reference, so CER/WER/punctuation numbers
are exact. It mirrors the shape of the real pages seen so far -- clean,
justified right-to-left paragraphs, dialogue lines with dashes / colons /
guillemets / parentheses, Arabic-Indic and Western digits, an embedded Latin
word, tashkeel on a few words, a centered page number, and a recurring
footer watermark on both bottom corners -- but it is NOT a substitute for
real books: fonts, kerning and ink differ. Treat it as a controlled
regression benchmark; real-page spot checks remain a separate check.

All sample text below is original, written for this benchmark.

Usage:  python scripts/synth_benchmark.py [--out DIR] [--pages-per-font N]
                                           [--escalation]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from bookocr.config import load_config
from bookocr.eval.metrics import normalize, page_report
from bookocr.pipeline import BookPipeline

# (kind, text). "p" = justified paragraph, "d" = single dialogue line.
BLOCKS = [
    ("p", "كانت المنارة القديمة تقف عند طرف الخليج منذ أكثر من مئة عام، وكان حارسها العجوز سالم يصعد درجاتها كل مساء ليشعل المصباح الكبير قبل أن تبتلع العتمة آخر ضوء في السماء."),
    ("p", "في تلك الليلة هبّت ريح باردة من الشمال، وسمع سالم طرقًا خفيفًا على الباب الحديدي في أسفل البرج. توقّف لحظة وأصغى، ثم نزل ببطء وهو يمسك بالدرابزين المتآكل."),
    ("p", "وقفت على العتبة فتاة صغيرة يبلّلها المطر، تحمل بين ذراعيها صندوقًا خشبيًا مغلقًا. قالت بصوت مرتعش:"),
    ("d", "«هل أنت الحارس؟ لقد قالوا لي إنك الوحيد الذي يعرف الطريق إلى الجزيرة.»"),
    ("p", "نظر إليها سالم طويلًا قبل أن يجيب، فقد مرّت سنوات لم يسأله فيها أحد عن الجزيرة، وظنّ أن الناس نسوها كما نسوا كثيرًا من الأشياء القديمة."),
    ("d", "– ومن أنتِ يا ابنتي؟ وماذا يوجد في هذا الصندوق؟"),
    ("d", "– اسمي ليلى، وهذا الصندوق كان لجدّي (رحمه الله). طلب مني أن أسلّمه لك قبل حلول الشتاء."),
    ("p", "أخذ سالم الصندوق بيدين ترتجفان، وأحسّ بثقله يفوق ما توقّع. ثم قال وهو يفسح لها الطريق: ادخلي واجلسي قرب الموقد، فالليلة طويلة وأمامنا حديث كثير."),
    ("p", "أشار الدفتر القديم إلى أن السفينة رست في الميناء عام 1927، وأنها حملت ٤٧ راكبًا وثلاثة عشر صندوقًا، وكُتب على الغلاف بحروف لاتينية كلمة Harbor بخط باهت."),
    ("p", "جلست ليلى تتأمل النار، وسألت نفسها: هل يعقل أن تكون الحكاية حقيقية؟ ثم تذكّرت وصية جدّها الأخيرة، فشدّت معطفها حولها وانتظرت أن يتكلم الرجل."),
    ("d", "قال سالم: «لن أفتح الصندوق الليلة.. فبعض الأسرار تحتاج إلى صباح هادئ!»"),
    ("p", "ومع أول خيوط الفجر، كان البحر قد هدأ تمامًا، وعاد ضوء المنارة ينطفئ تدريجيًا كأنه يودّع آخر النجوم. نزل الاثنان إلى الشاطئ، وحمل كلٌّ منهما طرفًا من الحبل الذي يربط القارب الصغير."),
]

FONTS = {
    "naskh": ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", 46),
    "kufi": ("/usr/share/fonts/truetype/noto/NotoKufiArabic-Regular.ttf", 40),
    "sans": ("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", 42),
    "freeserif": ("/usr/share/fonts/truetype/freefont/FreeSerif.ttf", 50),
    "dejavu": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40),
}
WATERMARK_LEFT = "t.me/demo_channel"
WATERMARK_RIGHT = "مكتبة"
ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

PAGE_W, PAGE_H = 1254, 2149
MARGIN_X, MARGIN_TOP, BOTTOM_RESERVED = 60, 90, 300


def _draw_word(draw, font, x_right, y, word):
    w = font.getlength(word, direction="rtl", language="ar")
    draw.text((x_right - w, y), word, font=font, fill=0, direction="rtl", language="ar")
    return w


def _wrap(font, text, max_width):
    words, lines, cur = text.split(" "), [], []
    for word in words:
        trial = " ".join(cur + [word])
        if cur and font.getlength(trial, direction="rtl", language="ar") > max_width:
            lines.append(cur)
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(cur)
    return lines


def render_page(font_path, size, blocks, page_number, seed):
    rng = random.Random(seed)
    font = ImageFont.truetype(font_path, size, layout_engine=ImageFont.Layout.RAQM)
    small = ImageFont.truetype(font_path, int(size * 0.7), layout_engine=ImageFont.Layout.RAQM)
    img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(img)
    max_w = PAGE_W - 2 * MARGIN_X
    line_h = int(size * 1.9)
    y = MARGIN_TOP
    used_text = []

    for kind, text in blocks:
        lines = _wrap(font, text, max_w)
        if y + line_h * len(lines) > PAGE_H - BOTTOM_RESERVED:
            break
        used_text.append(text)
        for i, words in enumerate(lines):
            last = i == len(lines) - 1
            widths = [font.getlength(w, direction="rtl", language="ar") for w in words]
            space = font.getlength(" ", direction="rtl", language="ar")
            if kind == "p" and not last and len(words) > 1:
                space = (max_w - sum(widths)) / (len(words) - 1)  # justify
            x = PAGE_W - MARGIN_X
            for word, w in zip(words, widths):
                _draw_word(draw, font, x, y, word)
                x -= w + space
            y += line_h
        y += int(line_h * 0.25) if kind == "p" else int(line_h * 0.1)

    num = str(page_number).translate(ARABIC_INDIC)
    nw = small.getlength(num, direction="rtl", language="ar")
    draw.text(((PAGE_W - nw) / 2, PAGE_H - 200), num, font=small, fill=0, direction="rtl", language="ar")
    green = (95, 130, 40)
    draw.text((50, PAGE_H - 120), WATERMARK_LEFT, font=small, fill=green)
    rw = small.getlength(WATERMARK_RIGHT, direction="rtl", language="ar")
    draw.text((PAGE_W - 50 - rw, PAGE_H - 120), WATERMARK_RIGHT, font=small, fill=green, direction="rtl", language="ar")
    return img, " ".join(used_text)


def build_pdf(pages, pdf_path):
    doc = pymupdf.open()
    for img_path in pages:
        page = doc.new_page(width=PAGE_W * 72 / 300, height=PAGE_H * 72 / 300)
        page.insert_image(page.rect, filename=str(img_path))
    doc.save(str(pdf_path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="synth_bench_out")
    ap.add_argument("--pages-per-font", type=int, default=2)
    ap.add_argument("--escalation", action="store_true", help="leave the QARI escalation pass on (slow on CPU)")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    (out / "pages").mkdir(parents=True, exist_ok=True)

    refs, img_paths, fonts_of = {}, [], {}
    page_no = 0
    for font_name, (font_path, size) in FONTS.items():
        for k in range(args.pages_per_font):
            page_no += 1
            rng = random.Random(1000 * page_no + k)
            blocks = BLOCKS[:]
            start = rng.randrange(len(blocks))
            blocks = blocks[start:] + blocks[:start]
            img, ref = render_page(font_path, size, blocks, page_no, seed=page_no)
            p = out / "pages" / f"page_{page_no:03d}_{font_name}.png"
            img.save(p)
            img_paths.append(p)
            refs[page_no], fonts_of[page_no] = ref, font_name

    pdf = out / "synthetic_book.pdf"
    build_pdf(img_paths, pdf)
    (out / "reference.json").write_text(json.dumps({str(k): v for k, v in refs.items()}, ensure_ascii=False, indent=1), encoding="utf-8")

    overrides = {} if args.escalation else {"ocr.escalation.enabled": False}
    cfg = load_config(cli_overrides=overrides)
    conv_dir = out / "converted"
    t0 = time.time()
    BookPipeline(cfg).convert(str(pdf), str(conv_dir), force=True)
    elapsed = time.time() - t0

    txt = (conv_dir / "book.txt").read_text(encoding="utf-8")
    hyp_pages, current = {}, None
    for line in txt.splitlines():
        if line.startswith("===== PAGE ") and line.endswith(" ====="):
            current = int(line.split()[2])
            hyp_pages[current] = []
        elif current is not None:
            hyp_pages[current].append(line)

    per_page, by_font = [], {}
    for n, ref in refs.items():
        hyp = "\n".join(hyp_pages.get(n, []))
        r = page_report(ref, hyp)
        r.update(page=n, font=fonts_of[n])
        per_page.append(r)
        by_font.setdefault(fonts_of[n], []).append(r)

    def mean(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    print(f"\nconverted {len(refs)} pages in {elapsed:.0f}s\n")
    print(f"{'font':<10} {'CER strict':>10} {'CER loose':>10} {'WER strict':>10} {'WER loose':>10} {'punct kept':>10}")
    for font_name, rows in by_font.items():
        print(f"{font_name:<10} {mean(rows,'cer_strict'):>10.2%} {mean(rows,'cer_loose'):>10.2%} {mean(rows,'wer_strict'):>10.2%} {mean(rows,'wer_loose'):>10.2%} {mean(rows,'punct_recall'):>10.1%}")
    print(f"{'ALL':<10} {mean(per_page,'cer_strict'):>10.2%} {mean(per_page,'cer_loose'):>10.2%} {mean(per_page,'wer_strict'):>10.2%} {mean(per_page,'wer_loose'):>10.2%} {mean(per_page,'punct_recall'):>10.1%}")

    (out / "results.json").write_text(json.dumps(per_page, ensure_ascii=False, indent=1), encoding="utf-8")
    leaked = [n for n, lines in hyp_pages.items() if any(WATERMARK_LEFT in l or "demo_channel" in l for l in lines)]
    print(f"\nwatermark text leaked into output on pages: {leaked or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

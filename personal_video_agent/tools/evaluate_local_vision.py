"""Run a real local OCR/chart smoke check, with a repeat read to measure caching."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from pva import intelligence, visual


def fixture(path: Path) -> None:
    image = Image.new("RGB", (900, 650), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 26) if font_path.exists() else ImageFont.load_default(size=26)
    draw.text((65, 20), "Monthly Revenue (USD)", font=font, fill="black")
    for value in range(0, 41, 10):
        y = 390 - value * 7
        draw.line((100, y, 780, y), fill="#ccd3dc", width=1)
        draw.text((45, y - 13), str(value), font=font, fill="black")
    draw.line((100, 90, 100, 390, 780, 390), fill="black", width=3)
    for x, value, label, color in ((240, 20, "Jan", "#367cc1"), (520, 35, "Feb", "#20a683")):
        draw.rectangle((x, 390-value*7, x+120, 390), fill=color)
        draw.text((x+35, 390-value*7-35), str(value), font=font, fill="black")
        draw.text((x+35, 400), label, font=font, fill="black")
    for row, values in enumerate((("Month", "Revenue", "Cost"), ("Jan", "20", "10"), ("Feb", "35", "12"))):
        for col, value in enumerate(values):
            x, y = 130+col*210, 470+row*52
            draw.rectangle((x, y, x+210, y+52), outline="black", width=2)
            draw.text((x+18, y+10), value, font=font, fill="black")
    image.save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    output = ROOT / "data" / "vision-eval"
    output.mkdir(parents=True, exist_ok=True)
    image = args.image or output / "chart-and-table.png"
    if args.image is None:
        fixture(image)
    intelligence._load_env()
    previous = [None, 0.0]

    def progress(detail):
        now = time.monotonic()
        phase = detail.split(" · ", 1)[0]
        if phase != previous[0] or now - previous[1] >= 20:
            print(detail, flush=True)
            previous[:] = [phase, now]

    started = time.monotonic()
    result = visual.read(image, on_progress=progress, timeout=args.timeout, purpose="evaluation")
    seconds = round(time.monotonic()-started, 3)
    cached_started = time.monotonic()
    cached = visual.read(image, timeout=args.timeout, purpose="evaluation")
    report = {"image": str(image), "model": result["model"], "device": result["device"],
              "seconds": seconds, "repeat_seconds": round(time.monotonic()-cached_started, 3),
              "cache_matches": result == cached, "block_labels": [b["label"] for b in result["blocks"]],
              "text": result["text"], "api_calls": 0,
              "load_seconds": result.get("load_seconds"),
              "inference_seconds": result.get("inference_seconds")}
    if args.image is None:
        report["expected_values_found"] = {value: value.casefold() in result["text"].casefold()
                                            for value in ("Jan", "Feb", "20", "35", "10", "12")}
    (output / "last-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / (image.stem + ".result.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    main()

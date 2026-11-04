"""One isolated PaddleOCR-VL 1.6 layout + recognition run; no paid API calls."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def blocks_from_result(payload: dict) -> list[dict]:
    payload = payload.get("res", payload)
    blocks = []
    for block in payload.get("parsing_res_list", []):
        text = str(block.get("block_content") or "").strip()
        label = str(block.get("block_label") or "text")
        if text and label not in {"image", "header_image", "footer_image"}:
            blocks.append({"label": label,
                           "text": text, "bbox": block.get("block_bbox"),
                           "order": block.get("block_order")})
    return blocks


def run(image: Path, output: Path, device: str) -> None:
    from paddleocr import LayoutDetection, PaddleOCRVL

    print("PVA_OCR:Detecting document layout and complex visual regions", flush=True)
    load_started = time.monotonic()
    layout = LayoutDetection(model_name="PP-DocLayoutV3", device=device, cpu_threads=4)
    complex_labels = {"chart", "table", "display_formula", "inline_formula"}
    has_complex = False
    for page in layout.predict(str(image)):
        payload = page.json
        if isinstance(payload, str):
            payload = json.loads(payload)
        boxes = payload.get("res", payload).get("boxes", [])
        has_complex = has_complex or any(box.get("label") in complex_labels for box in boxes)
    del layout
    if not has_complex:
        output.write_text(json.dumps({"model": "PP-DocLayoutV3 + RapidOCR", "device": device,
            "text_only": True, "text": "", "blocks": [],
            "load_seconds": round(time.monotonic()-load_started, 3), "inference_seconds": 0}), encoding="utf-8")
        return
    print("PVA_OCR:Loading PaddleOCR-VL 1.6 for chart, table and formula recognition", flush=True)
    thresholds = {index: (0.3 if index in {3, 5, 15, 21} else 1.0) for index in range(25)}
    pipeline = PaddleOCRVL(pipeline_version="v1.6", device=device, cpu_threads=4,
                           use_doc_orientation_classify=False,
                           use_doc_unwarping=False, use_chart_recognition=True,
                           layout_threshold=thresholds)
    load_seconds = time.monotonic() - load_started
    print("PVA_OCR:Reading image layout, text, tables and charts", flush=True)
    inference_started = time.monotonic()
    blocks = []
    for result in pipeline.predict(str(image), max_new_tokens=1024):
        payload = result.json
        if isinstance(payload, str):
            payload = json.loads(payload)
        blocks.extend(blocks_from_result(payload))
    if not blocks:
        raise ValueError("PaddleOCR-VL found no readable text, table or chart")
    text = "\n\n".join(f"[{block['label']}] {block['text']}" for block in blocks)
    output.write_text(json.dumps({"model": "PaddleOCR-VL-1.6-0.9B", "device": device,
                                  "text": text, "blocks": blocks,
                                  "structured_only": True,
                                  "load_seconds": round(load_seconds, 3),
                                  "inference_seconds": round(time.monotonic()-inference_started, 3)}, ensure_ascii=False),
                      encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    run(args.image, args.output, args.device)

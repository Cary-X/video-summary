"""Bounded local visual inference, content caching, and measured local usage."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import store


ROOT = Path(__file__).resolve().parents[1]
MODEL = "PaddleOCR-VL-1.6-0.9B"
_lock = threading.Lock()
_start_worker = subprocess.Popen


def _stop(process) -> None:
    if process is not None and process.poll() is None:
        if os.name == "nt":
            # A Windows venv launcher can spawn the actual interpreter as a child.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def python_path() -> Path:
    default = ROOT / ".venv-paddleocr" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return Path(os.getenv("PVA_PADDLE_PYTHON") or default)


def read(image: Path, *, db_path: Path | None = None, video_id: str | None = None,
         finding_task_id: str | None = None, on_progress=None, timeout: float = 600,
         purpose: str = "frame reading") -> dict:
    started = time.monotonic()
    device = os.getenv("PVA_PADDLE_DEVICE", "cpu")
    key = hashlib.sha256(image.read_bytes() + (MODEL + device + "layout-chart-v2").encode()).hexdigest()
    db = store.connect(db_path or ROOT / "data" / "personal.db")
    result, error, cached = None, None, False
    process = None
    acquired = False
    try:
        saved = db.execute("SELECT result_json FROM visual_cache WHERE cache_key=?", (key,)).fetchone()
        if saved:
            result = json.loads(saved["result_json"])
            cached = True
            if on_progress:
                on_progress("Using cached local visual evidence")
            return result
        python = python_path()
        if not python.is_file():
            raise RuntimeError("Local vision is not installed. Run setup-local-vision.ps1 first.")
        while not _lock.acquire(timeout=1):
            if on_progress:
                on_progress("Waiting for the other local visual task")
        acquired = True
        saved = db.execute("SELECT result_json FROM visual_cache WHERE cache_key=?", (key,)).fetchone()
        if saved:
            result, cached = json.loads(saved["result_json"]), True
            return result
        with tempfile.TemporaryDirectory(prefix="pva-vision-", ignore_cleanup_errors=True) as work:
            output, log = Path(work) / "result.json", Path(work) / "worker.log"
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PADDLE_PDX_CACHE_HOME"] = str(ROOT / "data" / "models" / "paddleocr")
            env.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
            env.setdefault("OMP_NUM_THREADS", "4")
            env.setdefault("MKL_NUM_THREADS", "4")
            with log.open("wb") as stream:
                process = _start_worker(
                    [str(python), "-u", "-m", "pva.paddle_worker", str(image.resolve()),
                     str(output), "--device", device], cwd=str(ROOT), env=env,
                    stdout=stream, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                deadline = time.monotonic() + timeout
                phase = "Starting local vision"
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise RuntimeError(f"Local PaddleOCR-VL exceeded {timeout:.0f}s")
                        if on_progress:
                            try:
                                phases = [line.split("PVA_OCR:", 1)[1] for line in
                                          log.read_text(encoding="utf-8", errors="replace").splitlines()
                                          if line.startswith("PVA_OCR:")]
                                phase = phases[-1] if phases else phase
                            except OSError:
                                pass
                            on_progress(f"{phase} · {device.upper()} · {time.monotonic()-started:.0f}s")
                        time.sleep(1)
                except BaseException:
                    _stop(process)
                    raise
            if process.returncode or not output.is_file():
                detail = log.read_text(encoding="utf-8", errors="replace")[-1800:]
                raise RuntimeError("PaddleOCR-VL failed: " + detail)
            result = json.loads(output.read_text(encoding="utf-8"))
            if result.get("text_only") or result.get("structured_only"):
                from .intelligence import local_ocr
                ocr = local_ocr(image)
                if ocr:
                    result["text"] = "[OCR text]\n" + ocr + "\n\n" + result["text"]
                    result["blocks"].insert(0, {"label": "text", "text": ocr, "bbox": None, "order": None})
            if not str(result.get("text") or "").strip():
                raise RuntimeError("PaddleOCR-VL returned no readable visual evidence")
            with db:
                db.execute("INSERT OR REPLACE INTO visual_cache VALUES (?,?,?)",
                           (key, store.now(), json.dumps(result, ensure_ascii=False)))
            return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        _stop(process)
        if acquired:
            _lock.release()
        with db:
            db.execute("""INSERT INTO local_visual_usage
              (at,video_id,finding_task_id,purpose,model,device,seconds,status,cache_hit,output_chars,error)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (store.now(), video_id, finding_task_id, purpose, (result or {}).get("model", MODEL), device,
               round(time.monotonic()-started, 3), "failed" if error else "success",
               int(cached), len((result or {}).get("text", "")), error))
        db.close()

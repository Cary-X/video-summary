"""Isolated local ASR worker. Stdout contains JSON progress and the final result."""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path


def select_device() -> tuple[str, str]:
    import ctranslate2
    if not ctranslate2.get_cuda_device_count():
        return "cpu", "No CUDA device; using CPU INT8"
    if os.name == "nt":
        try:
            # Device detection alone does not check the libraries used by encode().
            for library in ("cublas64_12.dll", "cublasLt64_12.dll"):
                ctypes.WinDLL(library)
            directory = Path(ctranslate2.__file__).parent
            ctypes.WinDLL(str(directory / "cudnn64_9.dll")
                          if (directory / "cudnn64_9.dll").exists() else "cudnn64_9.dll")
        except OSError:
            return "cpu", "CUDA runtime incomplete; using CPU INT8"
    return "cuda", "Using NVIDIA GPU INT8"


def emit(**message) -> None:
    print(json.dumps(message, ensure_ascii=True), flush=True)


def run(path: str, *, force_cpu: bool = False) -> None:
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from huggingface_hub.errors import LocalEntryNotFoundError

    device, reason = (("cpu", "Using CPU INT8 for this entire transcript")
                      if force_cpu else select_device())
    emit(progress=0.02, detail=reason, device=device)
    emit(progress=0.04, detail=f"Decoding audio · {device.upper()}")
    audio = decode_audio(path)
    duration = len(audio) / 16000
    emit(progress=0.08, detail=f"Loading small model · {device.upper()} INT8")
    options = dict(device=device, compute_type="int8" if device == "cpu" else "int8_float16",
                   cpu_threads=4, num_workers=1)
    try:
        model = WhisperModel("small", local_files_only=True, **options)
    except LocalEntryNotFoundError:
        emit(progress=0.08, detail="Downloading local ASR model; first use only")
        model = WhisperModel("small", **options)
    emit(progress=0.10, detail=f"Detecting spoken language · {device.upper()}")
    pieces, info = model.transcribe(audio, vad_filter=True)
    rows = []
    for segment in pieces:
        if segment.text.strip():
            rows.append({"start": float(segment.start), "end": float(segment.end),
                         "text": segment.text.strip()})
        emit(progress=0.10 + 0.89 * min(1, segment.end / max(duration, 1)),
             detail=f"{device.upper()} INT8 · {segment.end:.0f}/{duration:.0f} audio seconds")
    if not rows:
        raise RuntimeError("ASR returned no speech")
    emit(result={"segments": rows, "language": info.language or "unknown"})


if __name__ == "__main__":
    try:
        run(sys.argv[1], force_cpu="--cpu" in sys.argv[2:])
    except Exception as exc:
        emit(error=f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)

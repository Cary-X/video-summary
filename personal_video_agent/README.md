# Raccoon Agent

[简体中文](README.zh-CN.md) · [Changelog](CHANGELOG.md)

Summarize videos, audio, PDFs, Word documents and public webpages in a private local library. Open the TV marker beside a summary point to see its source.

## Setup and launch

Install 64-bit Python 3.11+ and FFmpeg, then run these commands in this directory.

```powershell
python -m venv .venv64
.\.venv64\Scripts\python.exe -m pip install -r requirements.txt
```

Double-click **Start Raccoon Agent.cmd**. In Settings choose an API provider, enter its key and select a model, or run the local setup script and choose Local llama.cpp.

Local model / 本地模型: `powershell -ExecutionPolicy Bypass -File .\setup-local-llama.ps1`

## Hardware and memory

The base environment, GGUF and runtime disk sizes were measured locally. Other cache figures and RAM/VRAM ranges are planning estimates. Peak use varies with context, source size and concurrency.

| Component | Disk | RAM / VRAM | Requirements |
|---|---|---|---|
| App, PDF/DOCX and web text | 0.79 GiB | 8 GB RAM; 16 GB with local workers | Windows x64, Python 3.11+ |
| Video/audio download | Several GB temporary / 临时文件数 GB | Depends on media size / 随文件大小变化 | FFmpeg, internet for URLs / 链接需联网 |
| Faster Whisper small | ~0.5 GB model / 模型 | 2–4 GB RAM or VRAM / 内存或显存 | CPU INT8 or NVIDIA CUDA 12 cuBLAS + cuDNN 9 |
| llama.cpp Qwen3 1.7B | 1.27 GiB GGUF + 0.08 GiB runtime | 3–6 GB RAM or VRAM / 内存或显存 | CPU or compatible GPU / 兼容显卡 |
| RapidOCR | Included in base environment / 包含于基础环境 | 1–2 GB RAM | CPU |
| PaddleOCR-VL 1.6 | 1.04 GiB environment + model cache / 另加模型缓存 | 16 GB system RAM recommended / 建议整机 16 GB | Separate setup; compatible Paddle runtime for GPU / 独立安装，GPU 需兼容运行时 |
| Summary, matching and OCR APIs | No local weights / 无本地权重 | No dedicated GPU / 无需独显 | Internet and API key / 联网及密钥 |

[Faster Whisper benchmarks](https://github.com/SYSTRAN/faster-whisper#benchmark) · [Qwen3 GGUF](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF) · [PaddleOCR-VL](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)

## Configuration and data

Run `setup-local-vision.ps1` for local PaddleOCR. Copy `.env.example` to `.env` for photo matching and vision configuration. Keep `data/` and `.env` when upgrading. Re-enter the summary API key in Settings after restarting.

Public webpages support article and table extraction. Scans and embedded images use the configured OCR reader. Check source evidence for complex tables.

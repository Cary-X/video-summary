# Raccoon Agent

This folder contains the Windows x64 app, bundled Python and media tools,
local models, and the migrated library. Keep the folder together.

- **Start Raccoon Agent.cmd** opens the web Library on the local machine.
- **Start Raccoon Drop.cmd** (Raccoon Box) starts the desktop drop box and the local service.
- **frontend/** contains the web and desktop interfaces.
- **backend/** contains the processing service.
- **data/** contains models, the library database, and saved sources and evidence.
- **runtime/** contains Python, OCR dependencies, FFmpeg, and FFprobe.
- **.env** contains private provider settings. Keep this file and the data directory private when sharing a copy.

Local inference can use CPU. GPU acceleration depends on Windows drivers and the
appropriate GPU libraries. Internet sources and configured cloud providers still
require network access. Summary API keys entered in the web UI are session-only.

Further reading: [Chinese guide](README.zh-CN.md) and [changelog](CHANGELOG.md).

## GitHub source checkout

Install 64-bit Python 3.12 and FFmpeg (with FFprobe on PATH). From the `raccoon` directory run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.server
```

Open http://127.0.0.1:8507/. To use Raccoon Box, keep the server running and
run `.\.venv\Scripts\python.exe desktop_widget.py` in another terminal.
The Windows shortcuts use the portable installation's bundled runtime.
GitHub provides source and artwork. Local models and runtime binaries are installed separately.

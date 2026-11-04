"""Start and own a private llama.cpp worker for local GGUF inference."""
from __future__ import annotations

import atexit
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


DATA = Path(__file__).resolve().parents[1] / "data"
LOG = DATA / "llama.cpp.log"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def paths() -> tuple[Path, Path]:
    executable = "llama-server.exe" if os.name == "nt" else "llama-server"
    return (
        Path(os.getenv("PVA_LLAMA_SERVER") or DATA / "llama.cpp" / executable).expanduser().resolve(),
        Path(os.getenv("PVA_LLAMA_MODEL") or DATA / "models" / "qwen3-1.7b.gguf").expanduser().resolve(),
    )


def available() -> bool:
    return all(path.is_file() for path in paths())


class LocalServer:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.key = ""
        self.cpu = False
        self.lock = threading.Lock()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def request(self, path: str, payload: dict | None = None, *, timeout: int = 240) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
        )
        with _opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def start(self, cpu: bool) -> None:
        executable, model = paths()
        if not executable.is_file() or not model.is_file():
            raise ValueError("llama.cpp runtime or GGUF model is missing. Run setup-local-llama.ps1.")
        self.stop()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.key = secrets.token_urlsafe(24)
        self.cpu = cpu
        LOG.parent.mkdir(parents=True, exist_ok=True)
        device_args = ["--device", "none", "--n-gpu-layers", "0"] if cpu else ["--n-gpu-layers", "99"]
        with LOG.open("ab") as log:
            log.write(f"\n--- llama.cpp start ({'CPU' if cpu else 'GPU auto'}) ---\n".encode())
            log.flush()
            self.process = subprocess.Popen(
                [str(executable), "--model", str(model), "--alias", model.stem,
                 "--host", "127.0.0.1", "--port", str(self.port),
                 "--ctx-size", "8192", "--parallel", "1", *device_args,
                 "--api-key", self.key, "--no-ui"],
                cwd=executable.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                if self.request("/health", timeout=2).get("status") == "ok":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("llama.cpp did not start. See data/llama.cpp.log.")

    def complete(self, payload: dict) -> dict:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                try:
                    self.start(self.cpu or os.getenv("PVA_LLAMA_DEVICE", "").casefold() == "cpu")
                except (RuntimeError, OSError):
                    if self.cpu:
                        raise
                    self.start(cpu=True)
            try:
                return self.request("/v1/chat/completions", payload)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"llama.cpp returned HTTP {exc.code}. See data/llama.cpp.log.") from exc
            except (OSError, ValueError) as exc:
                if not self.cpu and self.process.poll() is not None:
                    self.start(cpu=True)
                    return self.request("/v1/chat/completions", payload)
                raise RuntimeError("llama.cpp did not finish. See data/llama.cpp.log.") from exc


server = LocalServer()
atexit.register(server.stop)

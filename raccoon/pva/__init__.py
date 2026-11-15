"""Compatibility import path for launchers and model subprocesses."""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[1] / "backend" / "pva")]
__version__ = "0.1.0"

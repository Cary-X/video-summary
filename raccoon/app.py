"""Compatibility entry point for Streamlit."""
from pathlib import Path
import runpy
import sys

project = Path(__file__).resolve().parent
sys.path.insert(0, str(project))
runpy.run_path(str(project / "frontend" / "app.py"), run_name="__main__")

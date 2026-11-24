"""Entry point for Streamlit."""
from pathlib import Path

_source = Path(__file__).resolve().parent / "frontend" / "app.py"
exec(compile(_source.read_bytes(), str(_source), "exec"),
     {**globals(), "__file__": str(_source)})

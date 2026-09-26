"""
Thin re-export so a plain `uvicorn main:app` from the repo root finds the
API, which actually lives in api/main.py. See api/main.py itself for why
that's a subfolder (this is a monorepo: api/ is the FastAPI service, web/
is the React frontend).
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("profilewatch_xmp_api", Path(__file__).resolve().parent / "api" / "main.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

app = _module.app

#!/usr/bin/env python3
"""
Thin entrypoint for the web-connected VTX client agent.
Keeps a stable script name for installers/startup launchers.
"""

from pathlib import Path
import sys


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from vtx_client_agent_web import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())


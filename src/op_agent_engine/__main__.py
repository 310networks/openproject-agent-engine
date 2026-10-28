"""Allow ``python -m op_agent_engine``."""

from __future__ import annotations

from op_agent_engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

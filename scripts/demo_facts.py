"""Stamp session facts into the viewer's bundled demo index.

The demo has no server, so the Insights view reads facts straight off
``viewer/public/demo/index.json``. Run this after changing a demo map or
``agentplay.facts``; ``tests/test_facts.py`` fails while the two disagree.

    python/.venv/bin/python scripts/demo_facts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agentplay import facts

DEMO = Path(__file__).resolve().parent.parent / "viewer" / "public" / "demo"


def main() -> int:
    index_path = DEMO / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for row in index.get("sessions", []):
        doc = json.loads((DEMO / row["file"]).read_text(encoding="utf-8"))
        row.update(facts.stamp(doc))
    index_path.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Stamped facts on {len(index.get('sessions', []))} demo sessions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

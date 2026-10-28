"""How your prompts land: what the session index says about the way you start and steer sessions.

Only your own sessions (two or more messages from you) in the last ``days``. Every finding is a comparison or a
total with its numbers, and only when there's enough to say it: ``MIN`` sessions on each side of a comparison.
Nothing here reads what you wrote; the index holds lengths and counts, never text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store

MIN = 5
DETAILED = 300  # characters in the first message: a paragraph, not a line


def _parse(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def report(store: Store, now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = [r for r in store.index.values()
            if r.get("role", "root") == "root" and int(r.get("userTurns") or 0) >= 2
            and (_parse(r.get("startedAt")) or since) > since]
    facts = [(r, r.get("facts") or {}) for r in rows]
    out: list[dict[str, str]] = []

    # 1. How you start: a detailed first message against a short one, by the follow-ups each needed.
    long_ = [r for r, f in facts if int(f.get("firstPromptChars") or 0) >= DETAILED]
    short = [r for r, f in facts if 0 < int(f.get("firstPromptChars") or 0) < DETAILED]
    if len(long_) >= MIN and len(short) >= MIN:
        a = _avg([int(r.get("userTurns") or 0) - 1 for r in long_])
        b = _avg([int(r.get("userTurns") or 0) - 1 for r in short])
        if abs(a - b) >= 1:
            out.append({"kind": "start", "text":
                        f"Sessions you opened with a detailed ask ({DETAILED}+ characters) needed {a:.0f} follow-up "
                        f"messages from you on average; ones you opened with a line needed {b:.0f}.",
                        # Detailed openings usually mean bigger jobs, so the lesson is about size, not length.
                        "tip": "Big asks drew the most back-and-forth: split them into steps you can check one by one."
                        if a > b else "Saying more up front saved you follow-ups. Keep opening with what done looks like."})

    # 2. Where money went without moving the work: retries and re-reading files.
    retry = sum(float((f.get("waste") or {}).get("retry") or 0) for _, f in facts)
    reread = sum(float((f.get("waste") or {}).get("reread") or 0) for _, f in facts)
    total = sum(float(r.get("costUsd") or 0) for r in rows)
    if total > 0 and retry + reread >= 0.05 * total:
        out.append({"kind": "waste", "text":
                    f"${retry + reread:,.0f} of your ${total:,.0f} went to retrying failed steps (${retry:,.0f}) and "
                    f"re-reading files it had already read (${reread:,.0f}).",
                    "tip": "Name the files up front and ask it to fix the first failure before trying again."})

    # 3. Edits made blind: to files it hadn't read in that session.
    edited = sum(int(f.get("editedFiles") or 0) for _, f in facts)
    blind = sum(int(f.get("blindEdits") or 0) for _, f in facts)
    if edited >= 20 and blind / edited >= 0.15:
        out.append({"kind": "blind", "text":
                    f"{blind / edited:.0%} of its edits were to files it hadn't read first ({blind} of {edited}).",
                    "tip": "Point it at the files to read before it changes anything."})

    # 4. Rework: the same file edited again and again.
    rework = sum(int(f.get("reworkEdits") or 0) for _, f in facts)
    if edited >= 20 and rework / edited >= 0.3:
        out.append({"kind": "rework", "text":
                    f"It went back to edit the same files {rework} times across {len(rows)} sessions.",
                    "tip": "Agree the approach in a sentence before it starts editing."})
    return {"days": days, "sessions": len(rows), "findings": out[:4]}

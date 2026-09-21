"""Session state and history compaction: facts kept by code, not "remembered" by the model.

State (stored as JSON on the session):
    scope             the last product filter and week the user looked at
    groups            SKU lists as handles (G1, G2, ...), each with the week, regions and when it was resolved.
                      Tools take `group_id` instead of long SKU lists. A group says WHICH products, never what
                      their status is: status is always re-queried.
    earlier_questions questions from turns that have been dropped from the history (answers are gone)
    last_checks       for each checked scope (week, skus, regions) the failing records of its last diagnosis, so a
                      repeated question is answered with what changed since then (computed here, not by the model)

History compaction keeps the conversation the model sees small and stable. It runs when a new question
arrives, on the turns stored so far:
    * the turn being processed keeps its full tool traffic (run_turn adds it after the compacted history)
    * every earlier turn keeps only its question and final answer
    * beyond MAX_TURNS earlier turns, the oldest leave the history and survive only as a question in the state
Whole turns are dropped or shortened together, so every tool_use stays paired with its tool_result.
The full, uncompacted transcript of every turn is kept in the audit log (turn_transcripts).
"""
from __future__ import annotations

import hashlib
import json

MAX_GROUPS = 10
MAX_CHECKS = 5            # scopes whose last diagnosis is remembered
SAMPLE = 5                # examples listed per kind of change
MAX_TURNS = 10            # turns kept in the history (older ones as question + answer only)
MAX_EARLIER = 30          # remembered questions from dropped turns


class SessionState:
    def __init__(self, data: dict | None = None):
        d = data or {}
        self.scope: dict = dict(d.get("scope") or {})
        self.groups: dict[str, dict] = dict(d.get("groups") or {})
        self.next_group: int = int(d.get("next_group", 1))
        self.earlier_questions: list[str] = list(d.get("earlier_questions") or [])
        self.last_checks: dict[str, dict] = dict(d.get("last_checks") or {})

    def to_json(self) -> dict:
        return {"scope": self.scope, "groups": self.groups, "next_group": self.next_group,
                "earlier_questions": self.earlier_questions, "last_checks": self.last_checks}

    @staticmethod
    def scope_id(week: str, skus: list[str], regions: list[str]) -> str:
        """Identifies what a check looked at: the same week, SKUs and regions, however they were listed."""
        raw = json.dumps([week, sorted(set(skus)), sorted(set(regions))])
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def record_check(self, scope_id: str, as_of: str, data_version: str | None, failing: dict[str, str]) -> dict | None:
        """Remember this diagnosis and return what changed since the previous one for the same scope
        (None the first time). `failing` maps 'sku|pack|region' to 'CODE:reason' for every failing record."""
        previous = self.last_checks.get(scope_id)
        self.last_checks[scope_id] = {"as_of": as_of, "data_version": data_version, "failing": failing}
        while len(self.last_checks) > MAX_CHECKS:                  # forget the oldest scope
            del self.last_checks[min(self.last_checks, key=lambda k: self.last_checks[k]["as_of"])]
        if previous is None:
            return None
        return {"previous_as_of": previous["as_of"], "previous_data_version": previous["data_version"],
                **diff_checks(previous["failing"], failing)}

    def set_scope(self, **scope) -> None:
        self.scope = {k: v for k, v in scope.items() if v}

    def add_group(self, skus: list[str], label: str, week: str, regions: list[str], as_of: str,
                  data_version: str | None) -> str:
        """Store a SKU list under a handle. The same list, week and regions reuses its handle."""
        skus, regions = sorted(set(skus)), sorted(regions)
        for handle, g in self.groups.items():
            if g["skus"] == skus and g["week"] == week and g["regions"] == regions:
                g.update(label=label, as_of=as_of, data_version=data_version)
                return handle
        handle = f"G{self.next_group}"
        self.next_group += 1                                        # handles are never reused
        self.groups[handle] = {"skus": skus, "label": label, "week": week, "regions": regions,
                               "as_of": as_of, "data_version": data_version}
        while len(self.groups) > MAX_GROUPS:                       # drop the oldest (JSON does not keep key order)
            del self.groups[min(self.groups, key=lambda h: int(h[1:]))]
        return handle

    def get_group(self, handle: str) -> dict | None:
        return self.groups.get(handle)

    def note_dropped(self, questions: list[str]) -> None:
        self.earlier_questions = (self.earlier_questions + questions)[-MAX_EARLIER:]

    def render(self) -> str:
        """The block added to the system prompt each turn ('' when there is nothing to say)."""
        if not (self.scope or self.groups or self.earlier_questions):
            return ""
        lines = ["Session state (kept by the service, not by you):"]
        if self.scope:
            lines.append("- last product filter: " + ", ".join(f"{k} {v}" for k, v in self.scope.items()))
        if self.groups:
            lines.append("- product groups (pass group_id to a tool instead of listing skus):")
            for handle in sorted(self.groups, key=lambda h: int(h[1:])):
                g = self.groups[handle]
                lines.append(f"  {handle}: {len(g['skus'])} skus, {g['label']}, week {g['week']}, "
                             f"regions {'/'.join(g['regions'])}, resolved {g['as_of']}")
            lines.append("  A group says which products; it says nothing about their current status. Re-query for status.")
        if self.earlier_questions:
            lines.append("- earlier questions in this session (their answers are no longer available): "
                         + " | ".join(self.earlier_questions))
        return "\n".join(lines)


def diff_checks(before: dict[str, str], now: dict[str, str]) -> dict:
    """What changed between two diagnoses of the same scope, given their failing records."""
    def item(key: str, **why) -> dict:
        sku, pack, region = key.split("|")
        return {"sku": sku, "pack_qty": None if pack == "None" else int(pack), "region": region, **why}

    resolved = sorted(k for k in before if k not in now)
    new = sorted(k for k in now if k not in before)
    changed = sorted(k for k in before if k in now and before[k] != now[k])
    return {"resolved": len(resolved), "new_failures": len(new), "changed": len(changed),
            "still_failing": sum(1 for k in before if k in now and before[k] == now[k]),
            "examples": {"resolved": [item(k, was=before[k]) for k in resolved[:SAMPLE]],
                         "new_failures": [item(k, now=now[k]) for k in new[:SAMPLE]],
                         "changed": [item(k, was=before[k], now=now[k]) for k in changed[:SAMPLE]]}}


# ---------------- history compaction ----------------
def _turns(messages: list[dict]) -> list[list[dict]]:
    """Split a message list into turns; a turn starts at a user message whose content is a plain string."""
    turns: list[list[dict]] = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            turns.append([m])
        elif turns:
            turns[-1].append(m)
    return turns


def _final_answer(turn: list[dict]) -> str:
    said = ""
    for m in turn[1:]:
        if m["role"] == "assistant" and not any(b["type"] == "tool_use" for b in m["content"]):
            said = "".join(b["text"] for b in m["content"] if b["type"] == "text") or said
    return said or "(no answer)"


def compact_history(messages: list[dict], max_turns: int = MAX_TURNS) -> tuple[list[dict], list[str]]:
    """Compact the stored turns for the next model call. Returns (messages, questions of the turns dropped
    entirely). Every turn in `messages` is a previous turn, so each is reduced to question + final answer.
    Idempotent."""
    turns = _turns(messages)
    dropped = [t[0]["content"] for t in turns[:-max_turns]] if len(turns) > max_turns else []
    out: list[dict] = []
    for turn in turns[-max_turns:]:
        out += [turn[0], {"role": "assistant", "content": [{"type": "text", "text": _final_answer(turn)}]}]
    return out, dropped

"""Session state and history compaction: facts kept by code, not "remembered" by the model.

State (stored as JSON on the session):
    scope             the last product filter and week the user looked at
    groups            SKU lists as handles (G1, G2, ...), each with the week, regions and when it was resolved.
                      Tools take `group_id` instead of long SKU lists. A group says WHICH products, never what
                      their status is: status is always re-queried.
    earlier_questions questions from turns that have been dropped from the history (answers are gone)

History compaction keeps the conversation the model sees small and stable. It runs when a new question
arrives, on the turns stored so far:
    * the turn being processed keeps its full tool traffic (run_turn adds it after the compacted history)
    * every earlier turn keeps only its question and final answer
    * beyond MAX_TURNS earlier turns, the oldest leave the history and survive only as a question in the state
Whole turns are dropped or shortened together, so every tool_use stays paired with its tool_result.
The full, uncompacted transcript of every turn is kept in the audit log (turn_transcripts).
"""
from __future__ import annotations

MAX_GROUPS = 10
MAX_TURNS = 10            # turns kept in the history (older ones as question + answer only)
MAX_EARLIER = 30          # remembered questions from dropped turns


class SessionState:
    def __init__(self, data: dict | None = None):
        d = data or {}
        self.scope: dict = dict(d.get("scope") or {})
        self.groups: dict[str, dict] = dict(d.get("groups") or {})
        self.next_group: int = int(d.get("next_group", 1))
        self.earlier_questions: list[str] = list(d.get("earlier_questions") or [])

    def to_json(self) -> dict:
        return {"scope": self.scope, "groups": self.groups, "next_group": self.next_group,
                "earlier_questions": self.earlier_questions}

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

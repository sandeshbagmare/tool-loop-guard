"""Detect an agent that repeats or cycles its tool calls.

An agent stuck on the same call looks like progress in the trace: the model keeps
emitting a tool call, the runtime keeps executing it, and the context window fills with
near-identical messages until a recursion limit kills the run — with the actual cause
nowhere in the error. This catches the pattern early, while the reason is still visible.

    >>> guard = LoopGuard(max_repeats=2)
    >>> guard.observe("read_file", {"path": "a.txt"}).action
    'allow'
    >>> guard.observe("read_file", {"path": "a.txt"}).action
    'allow'
    >>> guard.observe("read_file", {"path": "a.txt"}).action
    'stop'
    >>> guard.observe("read_file", {"path": "a.txt"}).reason
    'read_file called 3 times with the same arguments'

Two failures are covered: a repeated identical call, and a cycle such as
``search`` → ``read`` → ``search`` → ``read`` where nothing repeats three times in a row
but nothing progresses either. Standard library only, Python 3.9+.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

__all__ = ["Decision", "LoopGuard", "signature"]

__version__ = "0.1.0"

_MAX_STRING = 200


def signature(name: str, arguments: Any = None) -> str:
    """A stable one-line fingerprint of a tool call.

    Keys are sorted, whitespace is dropped, and long strings are truncated, so the same
    call spelled with a different dict order or a re-serialised argument blob is still
    recognised as the same call.
    """
    return f"{name}({_stable(arguments)})"


def _stable(value: Any) -> str:
    if isinstance(value, dict):
        items = sorted((str(key), _stable(item)) for key, item in value.items())
        return "{" + ",".join(f"{json.dumps(key)}:{item}" for key, item in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable(item) for item in value) + "]"
    if isinstance(value, str):
        trimmed = value.strip()
        if len(trimmed) > _MAX_STRING:
            trimmed = trimmed[:_MAX_STRING] + "…"
        return json.dumps(trimmed, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


@dataclass(frozen=True)
class Decision:
    """What the guard thinks about the call that was just observed."""

    action: str
    reason: str | None = None
    count: int = 0
    cycle: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action != "stop"

    def __bool__(self) -> bool:
        """Truthy while the run may continue, so ``if not guard.observe(...)`` reads well."""
        return self.allowed

    def __str__(self) -> str:
        return self.action if self.reason is None else f"{self.action}: {self.reason}"


class LoopGuard:
    """Track recent tool calls and decide when the agent has stopped making progress.

    ``max_repeats`` is how many times the same call may appear in the window before it
    is treated as a loop (3 means the third identical call stops the run).
    ``cycle_length`` is the longest period considered when looking for ``A B A B``.
    ``window`` bounds memory: only this many recent calls are inspected.

    A guard is cheap, so keep one per run and call :meth:`reset` between runs.
    """

    def __init__(
        self,
        *,
        max_repeats: int = 3,
        cycle_length: int = 4,
        window: int = 40,
        on_stop: str | None = None,
    ) -> None:
        if max_repeats < 2:
            raise ValueError("max_repeats must be at least 2")
        if cycle_length < 2:
            raise ValueError("cycle_length must be at least 2")
        if window < 2 * cycle_length:
            raise ValueError("window must be at least twice cycle_length")
        self.max_repeats = max_repeats
        self.cycle_length = cycle_length
        self.window = window
        self.on_stop = on_stop
        self._recent: deque[str] = deque(maxlen=window)

    def reset(self) -> None:
        """Forget everything, for a new run on the same guard."""
        self._recent.clear()

    def observe(self, name: str, arguments: Any = None) -> Decision:
        """Record a call and say whether the run should continue, warn, or stop."""
        fingerprint = signature(name, arguments)
        self._recent.append(fingerprint)
        count = Counter(self._recent)[fingerprint]
        if count >= self.max_repeats:
            return Decision(
                action="stop",
                reason=f"{name} called {count} times with the same arguments",
                count=count,
                cycle=(fingerprint,),
            )
        if count >= 2 and count == self.max_repeats - 1:
            return Decision(
                action="warn",
                reason=f"{name} called {count} times with the same arguments",
                count=count,
                cycle=(fingerprint,),
            )
        cycle = self._cycle()
        if cycle:
            seen: list[str] = []
            for item in cycle:
                tool = item.split("(", 1)[0]
                if tool not in seen:
                    seen.append(tool)
            names = seen
            unit = " ↔ ".join(names)
            return Decision(
                action="stop",
                reason=f"repeating cycle of {len(cycle)} calls: {unit}",
                count=len(cycle),
                cycle=tuple(cycle),
            )
        return Decision(action="allow", count=count)

    def check(self, name: str, arguments: Any = None) -> None:
        """Raise :class:`LoopDetected` when the call would close a loop."""
        decision = self.observe(name, arguments)
        if decision.action == "stop":
            raise LoopDetected(decision.reason or "loop detected", decision)

    def _cycle(self) -> list[str] | None:
        """The smallest period ``p`` such that the tail of the window repeats with period p."""
        history = list(self._recent)
        if len(history) < 4:
            return None
        for period in range(2, self.cycle_length + 1):
            needed = max(4, 2 * period)
            if len(history) < needed:
                continue
            tail = history[-needed:]
            if all(tail[index] == tail[index - period] for index in range(period, needed)):
                if len(set(tail)) == 1:  # a plain repeat, already reported above
                    return None
                return tail[-period:]
        return None


class LoopDetected(RuntimeError):
    """Raised by :meth:`LoopGuard.check` when the agent is looping."""

    def __init__(self, message: str, decision: Decision) -> None:
        super().__init__(message)
        self.decision = decision
        self.action = decision.action
        self.reason = decision.reason

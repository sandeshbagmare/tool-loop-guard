"""Tests for tool_loop_guard."""

import json

import pytest

from tool_loop_guard import Decision, LoopDetected, LoopGuard, signature


class TestSignature:
    def test_dict_order_does_not_matter(self):
        assert signature("t", {"a": 1, "b": 2}) == signature("t", {"b": 2, "a": 1})

    def test_the_name_matters(self):
        assert signature("a", {}) != signature("b", {})

    def test_nested_structures_are_canonical(self):
        assert signature("t", {"x": [1, {"b": 2, "a": 1}]}) == signature(
            "t", {"x": [1, {"a": 1, "b": 2}]}
        )

    def test_surrounding_whitespace_in_strings_is_ignored(self):
        assert signature("t", "  hello ") == signature("t", "hello")

    def test_long_strings_are_truncated(self):
        left = signature("t", "x" * 500)
        right = signature("t", "x" * 500 + "different tail that is beyond the limit")
        assert left == right
        assert len(left) < 260

    def test_a_short_difference_survives(self):
        assert signature("t", "abc") != signature("t", "abd")

    def test_scalars(self):
        assert signature("t", None) == "t(null)"
        assert signature("t", True) == "t(true)"
        assert signature("t", False) == "t(false)"
        assert signature("t", 1) != signature("t", 1.0)
        assert signature("t", "s") == 't("s")'

    def test_an_object_without_a_json_form(self):
        assert "object at" in signature("t", object())

    def test_no_arguments(self):
        assert signature("t") == "t(null)"
        assert signature("t", None) == "t(null)"


class TestObserve:
    def test_the_first_call_is_allowed(self):
        decision = LoopGuard().observe("read", "a.txt")
        assert decision.action == "allow"
        assert decision.allowed is True
        assert decision.reason is None

    def test_the_call_before_the_limit_warns(self):
        guard = LoopGuard(max_repeats=3)
        guard.observe("read", "a.txt")
        guard.observe("read", "a.txt")
        decision = guard.observe("read", "a.txt")
        assert decision.action == "stop"

    def test_a_warning_comes_before_the_stop(self):
        guard = LoopGuard(max_repeats=3)
        guard.observe("read", "a.txt")
        second = guard.observe("read", "a.txt")
        assert second.action == "warn"
        assert second.count == 2
        assert "read called 2 times" in second.reason

    def test_different_arguments_are_different_calls(self):
        guard = LoopGuard(max_repeats=2)
        assert guard.observe("read", "a").action == "allow"
        assert guard.observe("read", "b").action == "allow"

    def test_dict_key_order_does_not_hide_a_repeat(self):
        guard = LoopGuard(max_repeats=2)
        guard.observe("t", {"a": 1, "b": 2})
        assert guard.observe("t", {"b": 2, "a": 1}).action == "stop"

    def test_the_stop_reason_names_the_tool_and_the_count(self):
        guard = LoopGuard(max_repeats=3)
        for _ in range(3):
            decision = guard.observe("search", {"q": "x"})
        assert decision.reason == "search called 3 times with the same arguments"

    def test_the_count_reflects_the_window(self):
        guard = LoopGuard(max_repeats=5, window=10)
        for _ in range(4):
            guard.observe("t", 1)
        assert guard.observe("t", 1).action == "stop"
        assert guard.observe("t", 1).count == 6

    def test_a_bool_decision_is_true_while_allowed(self):
        guard = LoopGuard(max_repeats=2)
        assert bool(guard.observe("t", 1)) is True
        assert bool(guard.observe("t", 1)) is False

    def test_str_of_a_decision(self):
        assert str(Decision("allow")) == "allow"
        assert str(Decision("stop", "because")) == "stop: because"


class TestCycles:
    def test_a_two_step_cycle_is_detected(self):
        guard = LoopGuard(max_repeats=99)
        for _ in range(2):
            guard.observe("search", "q")
            decision = guard.observe("read", "f")
        assert decision.action == "stop"
        assert "search ↔ read" in decision.reason

    def test_a_three_step_cycle(self):
        guard = LoopGuard(max_repeats=99)
        for _ in range(2):
            guard.observe("a", 1)
            guard.observe("b", 2)
            decision = guard.observe("c", 3)
        assert decision.action == "stop"
        assert "repeating cycle of 3 calls" in decision.reason

    def test_a_cycle_longer_than_the_limit_is_allowed(self):
        guard = LoopGuard(max_repeats=99, cycle_length=2, window=40)
        for _ in range(2):
            for name in ["a", "b", "c"]:
                decision = guard.observe(name, 1)
        assert decision.action == "allow"

    def test_progress_is_never_a_cycle(self):
        guard = LoopGuard(max_repeats=99)
        for index in range(12):
            assert guard.observe("read", index).action == "allow"

    def test_an_identical_repeat_is_not_reported_as_a_cycle(self):
        guard = LoopGuard(max_repeats=99)
        for _ in range(6):
            decision = guard.observe("read", "same")
        assert decision.action == "allow"

    def test_the_cycle_is_exposed(self):
        guard = LoopGuard(max_repeats=99)
        for _ in range(2):
            guard.observe("a", 1)
            decision = guard.observe("b", 2)
        assert len(decision.cycle) == 2

    def test_only_a_suffix_that_repeats_counts(self):
        guard = LoopGuard(max_repeats=99)
        guard.observe("warmup", 0)
        guard.observe("x", 1)
        guard.observe("y", 2)
        guard.observe("x", 1)
        assert guard.observe("y", 2).action == "stop"


class TestCheck:
    def test_check_passes_while_the_run_progresses(self):
        guard = LoopGuard(max_repeats=3)
        guard.check("a", 1)
        guard.check("b", 2)

    def test_check_raises_on_a_loop(self):
        guard = LoopGuard(max_repeats=2)
        guard.check("a", 1)
        with pytest.raises(LoopDetected) as caught:
            guard.check("a", 1)
        assert caught.value.action == "stop"
        assert "twice" in str(caught.value) or "2 times" in str(caught.value)

    def test_the_exception_carries_the_decision(self):
        guard = LoopGuard(max_repeats=2)
        guard.check("a", 1)
        with pytest.raises(LoopDetected) as caught:
            guard.check("a", 1)
        assert isinstance(caught.value.decision, Decision)
        assert caught.value.decision.count == 2

    def test_it_is_a_runtime_error(self):
        assert issubclass(LoopDetected, RuntimeError)


class TestReset:
    def test_reset_forgets_history(self):
        guard = LoopGuard(max_repeats=2)
        guard.observe("a", 1)
        guard.reset()
        assert guard.observe("a", 1).action == "allow"

    def test_two_runs_on_one_guard(self):
        guard = LoopGuard(max_repeats=2)
        for _ in range(1):
            guard.observe("a", 1)
        guard.reset()
        for _ in range(1):
            guard.observe("a", 1)
        assert guard.observe("a", 1).action == "stop"


class TestConfiguration:
    def test_a_low_max_repeats_is_rejected(self):
        with pytest.raises(ValueError, match="max_repeats"):
            LoopGuard(max_repeats=1)

    def test_a_bad_cycle_length(self):
        with pytest.raises(ValueError, match="cycle_length"):
            LoopGuard(cycle_length=1)

    def test_a_window_smaller_than_the_cycle(self):
        with pytest.raises(ValueError, match="window"):
            LoopGuard(cycle_length=8, window=10)

    def test_the_window_bounds_memory(self):
        guard = LoopGuard(max_repeats=3, window=6, cycle_length=2)
        for index in range(100):
            guard.observe("t", index)
        assert len(guard._recent) == 6

    def test_an_on_stop_hint_is_kept(self):
        guard = LoopGuard(on_stop="ask the user for a new query")
        assert guard.on_stop == "ask the user for a new query"


class TestMiddlewareStyleUse:
    def test_a_tool_wrapper_returns_a_corrective_message_instead_of_running(self):
        """The shape a LangGraph middleware would use around a tool call."""
        guard = LoopGuard(max_repeats=3)
        calls = []

        def guarded(name, arguments, tool):
            decision = guard.observe(name, arguments)
            if decision.action == "stop":
                return f"stopped: {decision.reason}"
            calls.append(name)
            return tool(**arguments)

        def read(path):
            return f"contents of {path}"

        assert guarded("read", {"path": "a"}, read) == "contents of a"
        assert guarded("read", {"path": "a"}, read) == "contents of a"
        assert guarded("read", {"path": "a"}, read) == (
            "stopped: read called 3 times with the same arguments"
        )
        assert calls == ["read", "read"]

    def test_the_guard_can_drive_a_json_status_line(self):
        guard = LoopGuard(max_repeats=2)
        guard.observe("t", 1)
        decision = guard.observe("t", 1)
        assert json.loads(json.dumps({"action": decision.action}))["action"] == "stop"

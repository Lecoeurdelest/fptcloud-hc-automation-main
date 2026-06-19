"""Validation primitives for checklist assertions.

The live runner has service-specific validation paths, while the queue/checklist
pipeline needs a small reusable validator layer for Phase 4. These validators
operate on parsed Terraform state and side-effect-free probe callables so they
stay unit-testable without cloud access.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hc.executor.models import TFState
from hc.models import ExpectedAssertion, TaskSpec, Verdict


@dataclass(frozen=True)
class ValidationResult:
    verdict: Verdict
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS


class Validator(Protocol):
    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        """Evaluate one expected assertion."""


def _string_match(value: Any, assertion: ExpectedAssertion) -> ValidationResult:
    text = "" if value is None else str(value)
    subject = assertion.path or assertion.probe
    if assertion.equals is not None and text != assertion.equals:
        return ValidationResult(
            Verdict.FAIL,
            f"expected {subject} == {assertion.equals!r}, got {text!r}",
        )
    if assertion.contains is not None and assertion.contains not in text:
        return ValidationResult(
            Verdict.FAIL,
            f"expected {subject} to contain {assertion.contains!r}, got {text!r}",
        )
    return ValidationResult(Verdict.PASS, "assertion passed")


class TFStateValidator:
    """Evaluate paths such as ``fptcloud_subnet.this.cidr`` against TFState."""

    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        if tf_state is None:
            return ValidationResult(Verdict.INCONCLUSIVE, "terraform state is unavailable")
        if not assertion.path:
            return ValidationResult(Verdict.FAIL, "tf_state assertion requires path")
        value = self.resolve_path(tf_state, assertion.path)
        if value is _MISSING:
            return ValidationResult(Verdict.FAIL, f"path not found: {assertion.path}")
        return _string_match(value, assertion)

    @staticmethod
    def resolve_path(tf_state: TFState, path: str) -> Any:
        parts = [part for part in path.split(".") if part]
        if len(parts) >= 3:
            attrs = tf_state.get_resource_attrs(parts[0], parts[1])
            if attrs is not None:
                return _walk(attrs, parts[2:])
        if parts and parts[0] == "outputs" and len(parts) > 1:
            return _walk(tf_state.get_output(parts[1]), parts[2:])
        return _walk(tf_state.raw, parts)


class ManualValidator:
    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        note = assertion.note or "manual validation required"
        return ValidationResult(Verdict.INCONCLUSIVE, note)


class InVMValidator:
    def __init__(self, probe_runner: Callable[[TaskSpec, ExpectedAssertion], str] | None = None):
        self._probe_runner = probe_runner

    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        if self._probe_runner is None:
            return ValidationResult(Verdict.INCONCLUSIVE, "in-VM probe runner is not configured")
        output = self._probe_runner(task, assertion)
        return _string_match(output, assertion)


class APIProbeValidator:
    def __init__(self, probe_runner: Callable[[TaskSpec, ExpectedAssertion], Any] | None = None):
        self._probe_runner = probe_runner

    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        if self._probe_runner is not None:
            result = self._probe_runner(task, assertion)
            return _string_match(result, assertion)
        if assertion.url:
            return self._http_probe(assertion)
        return ValidationResult(Verdict.INCONCLUSIVE, "api probe runner is not configured")

    @staticmethod
    def _http_probe(assertion: ExpectedAssertion) -> ValidationResult:
        expected_status = assertion.status_code or 200
        try:
            request = Request(assertion.url or "", method="GET")
            # URL comes from operator-authored probe config.
            with urlopen(request, timeout=10) as response:
                status = int(response.status)
        except HTTPError as exc:
            status = int(exc.code)
        except (OSError, URLError) as exc:
            return ValidationResult(Verdict.FAIL, f"api probe failed: {exc}")
        if status != expected_status:
            return ValidationResult(Verdict.FAIL, f"expected HTTP {expected_status}, got {status}")
        return ValidationResult(Verdict.PASS, f"HTTP {status}")


class CompositeValidator:
    def __init__(self, validators: dict[str, Validator] | None = None):
        self.validators = validators or default_validators()

    def evaluate_all(
        self,
        task: TaskSpec,
        tf_state: TFState | None,
        assertions: Sequence[ExpectedAssertion],
        *,
        mode: str = "and",
    ) -> ValidationResult:
        if not assertions:
            return ValidationResult(Verdict.INCONCLUSIVE, "no assertions configured")
        results = [self.evaluate(task, tf_state, assertion) for assertion in assertions]
        if mode == "or":
            if any(result.verdict == Verdict.PASS for result in results):
                return ValidationResult(Verdict.PASS, _join_messages(results))
            if any(result.verdict == Verdict.INCONCLUSIVE for result in results):
                return ValidationResult(Verdict.INCONCLUSIVE, _join_messages(results))
            return ValidationResult(Verdict.FAIL, _join_messages(results))
        if mode == "not":
            first = results[0]
            if first.verdict == Verdict.PASS:
                return ValidationResult(Verdict.FAIL, first.message)
            if first.verdict == Verdict.FAIL:
                return ValidationResult(Verdict.PASS, first.message)
            return first
        if any(result.verdict == Verdict.FAIL for result in results):
            return ValidationResult(Verdict.FAIL, _join_messages(results))
        if any(result.verdict == Verdict.INCONCLUSIVE for result in results):
            return ValidationResult(Verdict.INCONCLUSIVE, _join_messages(results))
        return ValidationResult(Verdict.PASS, _join_messages(results))

    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        validator = self.validators.get(assertion.type)
        if validator is None:
            return ValidationResult(Verdict.FAIL, f"unknown validator type: {assertion.type}")
        return validator.evaluate(task, tf_state, assertion)


def default_validators() -> dict[str, Validator]:
    return {
        "tf_state": TFStateValidator(),
        "manual": ManualValidator(),
        "in_vm": InVMValidator(),
        "api_probe": APIProbeValidator(),
    }


def evaluate_assertions(
    task: TaskSpec,
    tf_state: TFState | None,
    assertions: Iterable[ExpectedAssertion],
    *,
    mode: str = "and",
) -> ValidationResult:
    return CompositeValidator().evaluate_all(task, tf_state, list(assertions), mode=mode)


class _Missing:
    pass


_MISSING = _Missing()


def _walk(value: Any, parts: Sequence[str]) -> Any:
    current = value
    for part in parts:
        if current is None:
            return _MISSING
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return _MISSING
            continue
        if not hasattr(current, part):
            return _MISSING
        current = getattr(current, part)
    return current


def _join_messages(results: Sequence[ValidationResult]) -> str:
    return "; ".join(result.message for result in results if result.message)

from __future__ import annotations

import pytest

from hc.executor.models import TFState
from hc.models import ExpectedAssertion, TaskSpec, Verdict
from hc.validator import (
    APIProbeValidator,
    CompositeValidator,
    InVMValidator,
    ManualValidator,
    TFStateValidator,
    evaluate_assertions,
)


def _task() -> TaskSpec:
    return TaskSpec(run_id="r1", tc_id="TC-001", tenant_id="t1", spec_hash="hash")


def _state() -> TFState:
    return TFState.from_json(
        {
            "format_version": "1.0",
            "resources": [
                {
                    "mode": "managed",
                    "type": "fptcloud_subnet",
                    "name": "this",
                    "instances": [{"attributes": {"cidr": "172.26.221.0/24", "id": "s-1"}}],
                }
            ],
            "outputs": {"bucket": {"value": "hc-bucket", "sensitive": False}},
        }
    )


@pytest.mark.unit
def test_tf_state_validator_passes_resource_attribute_path() -> None:
    result = TFStateValidator().evaluate(
        _task(),
        _state(),
        ExpectedAssertion(
            type="tf_state", path="fptcloud_subnet.this.cidr", equals="172.26.221.0/24"
        ),
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_tf_state_validator_fails_missing_path() -> None:
    result = TFStateValidator().evaluate(
        _task(),
        _state(),
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.missing", equals="x"),
    )

    assert result.verdict == Verdict.FAIL
    assert "path not found" in result.message


@pytest.mark.unit
def test_manual_validator_is_inconclusive() -> None:
    result = ManualValidator().evaluate(
        _task(), None, ExpectedAssertion(type="manual", note="provider gap")
    )

    assert result.verdict == Verdict.INCONCLUSIVE
    assert result.message == "provider gap"


@pytest.mark.unit
def test_in_vm_validator_uses_probe_runner() -> None:
    validator = InVMValidator(lambda _task, _assertion: "ok from guest")
    result = validator.evaluate(
        _task(), None, ExpectedAssertion(type="in_vm", probe="echo ok", contains="ok")
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_api_probe_validator_uses_probe_runner() -> None:
    validator = APIProbeValidator(lambda _task, _assertion: "object_exists")
    result = validator.evaluate(
        _task(),
        None,
        ExpectedAssertion(type="api_probe", check="object_exists", equals="object_exists"),
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_composite_and_or_not_modes() -> None:
    task = _task()
    assertions = [
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.cidr", equals="bad"),
        ExpectedAssertion(type="manual", note="needs human"),
    ]

    assert evaluate_assertions(task, _state(), assertions).verdict == Verdict.FAIL
    assert (
        evaluate_assertions(task, _state(), assertions, mode="or").verdict
        == Verdict.INCONCLUSIVE
    )
    assert (
        CompositeValidator().evaluate_all(task, _state(), assertions[:1], mode="not").verdict
        == Verdict.PASS
    )

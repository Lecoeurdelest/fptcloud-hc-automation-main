from __future__ import annotations

import ssl

import pytest

from hc.executor.models import TFState
from hc.models import ExpectedAssertion, TaskSpec, Verdict
from hc.validator import core as validator_core
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


def _vm_state() -> TFState:
    return TFState.from_json(
        {
            "format_version": "1.0",
            "resources": [
                {
                    "mode": "managed",
                    "type": "fptcloud_instance",
                    "name": "this",
                    "instances": [
                        {
                            "attributes": {
                                "public_ip": "203.0.113.10",
                                "username": "ubuntu",
                                "password": "secret",
                            }
                        }
                    ],
                }
            ],
        }
    )


class _FakeChannel:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class _FakeStream:
    def __init__(self, body: bytes, exit_code: int = 0) -> None:
        self._body = body
        self.channel = _FakeChannel(exit_code)

    def read(self) -> bytes:
        return self._body


class _FakeSSHClient:
    def __init__(self, stdout: bytes, exit_code: int = 0) -> None:
        self._stdout = stdout
        self._exit_code = exit_code
        self.connected = False

    def connect(self, **kwargs: object) -> None:
        self.connected = True

    def exec_command(
        self, command: str, timeout: float
    ) -> tuple[None, _FakeStream, _FakeStream]:
        return None, _FakeStream(self._stdout, self._exit_code), _FakeStream(b"")

    def close(self) -> None:
        self.connected = False


class _FailingSSHClient:
    def connect(self, **kwargs: object) -> None:
        raise TimeoutError("timed out")

    def close(self) -> None:
        return None


class _FakeWinRMResult:
    def __init__(self, stdout: bytes, status_code: int = 0) -> None:
        self.std_out = stdout
        self.std_err = b""
        self.status_code = status_code


class _FakeWinRMSession:
    def __init__(self, stdout: bytes, status_code: int = 0) -> None:
        self._stdout = stdout
        self._status_code = status_code

    def run_ps(self, command: str) -> _FakeWinRMResult:
        return _FakeWinRMResult(self._stdout, self._status_code)


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
def test_tf_state_validator_fails_equals_mismatch() -> None:
    result = TFStateValidator().evaluate(
        _task(),
        _state(),
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.cidr", equals="bad"),
    )

    assert result.verdict == Verdict.FAIL


@pytest.mark.unit
def test_tf_state_validator_supports_contains_and_regex() -> None:
    validator = TFStateValidator()

    contains_result = validator.evaluate(
        _task(),
        _state(),
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.cidr", contains="221.0"),
    )
    regex_result = validator.evaluate(
        _task(),
        _state(),
        ExpectedAssertion(
            type="tf_state",
            path="fptcloud_subnet.this.cidr",
            regex_match=r"^172\.26\.\d+\.0/24$",
        ),
    )

    assert contains_result.verdict == Verdict.PASS
    assert regex_result.verdict == Verdict.PASS


@pytest.mark.unit
def test_tf_state_validator_supports_jsonpath_style_path() -> None:
    result = TFStateValidator().evaluate(
        _task(),
        _state(),
        ExpectedAssertion(
            type="tf_state",
            path="$.resources[0].instances[0].attributes.cidr",
            equals="172.26.221.0/24",
        ),
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_tf_state_validator_supports_present_and_absent() -> None:
    validator = TFStateValidator()

    present_result = validator.evaluate(
        _task(),
        _state(),
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.id", present=True),
    )
    absent_result = validator.evaluate(
        _task(),
        _state(),
        ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.deleted_at", absent=True),
    )

    assert present_result.verdict == Verdict.PASS
    assert absent_result.verdict == Verdict.PASS


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
def test_in_vm_validator_winrm_boot_probe_passes_on_ok() -> None:
    validator = InVMValidator(
        winrm_session_factory=lambda *args, **kwargs: _FakeWinRMSession(b"ok")
    )

    result = validator.evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(
            type="in_vm",
            transport="winrm",
            username="Administrator",
            password="secret",
            probe="echo ok",
            contains="ok",
            exit_code=0,
        ),
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_in_vm_validator_winrm_unreachable_fails() -> None:
    def failing_session(*args: object, **kwargs: object) -> object:
        raise OSError("connection refused")

    result = InVMValidator(winrm_session_factory=failing_session).evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(type="in_vm", transport="winrm", probe="echo ok", contains="ok"),
    )

    assert result.verdict == Verdict.FAIL


@pytest.mark.unit
def test_in_vm_validator_winrm_auth_failure_fails() -> None:
    def auth_failure(*args: object, **kwargs: object) -> object:
        raise PermissionError("401 unauthorized")

    result = InVMValidator(winrm_session_factory=auth_failure).evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(type="in_vm", transport="winrm", probe="echo ok", contains="ok"),
    )

    assert result.verdict == Verdict.FAIL
    assert "authentication" in result.message.lower()


@pytest.mark.unit
def test_in_vm_validator_ssh_timeout_is_inconclusive() -> None:
    result = InVMValidator(ssh_client_factory=_FailingSSHClient).evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(type="in_vm", transport="ssh", probe="echo ok", contains="ok"),
    )

    assert result.verdict == Verdict.INCONCLUSIVE


@pytest.mark.unit
def test_in_vm_validator_linux_lsblk_requires_80gb() -> None:
    validator = InVMValidator(ssh_client_factory=lambda: _FakeSSHClient(b"85899345920\n"))

    pass_result = validator.evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(
            type="in_vm",
            transport="ssh",
            probe="lsblk -b -o SIZE -d -n /dev/vda",
            contains="85899345920",
            exit_code=0,
        ),
    )
    fail_result = InVMValidator(ssh_client_factory=lambda: _FakeSSHClient(b"42949672960\n")).evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(
            type="in_vm",
            transport="ssh",
            probe="lsblk -b -o SIZE -d -n /dev/vda",
            contains="85899345920",
        ),
    )

    assert pass_result.verdict == Verdict.PASS
    assert fail_result.verdict == Verdict.FAIL


@pytest.mark.unit
def test_in_vm_validator_file_exists_probe() -> None:
    validator = InVMValidator(ssh_client_factory=lambda: _FakeSSHClient(b"true\n"))

    result = validator.evaluate(
        _task(),
        _vm_state(),
        ExpectedAssertion(type="in_vm", transport="ssh", file_exists="/tmp/testbackup.txt"),
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
def test_api_probe_validator_http_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"bucket ok"

    def fake_urlopen(*args: object, **kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(validator_core, "urlopen", fake_urlopen)

    result = APIProbeValidator().evaluate(
        _task(),
        None,
        ExpectedAssertion(
            type="api_probe",
            url="https://example.test/health",
            status_code=200,
            contains="bucket",
            timeout_seconds=1,
            tls_verify=False,
        ),
    )

    assert result.verdict == Verdict.PASS


@pytest.mark.unit
def test_api_probe_validator_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Response:
        status = 503

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not ready"

    def fake_urlopen(*args: object, **kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(validator_core, "urlopen", fake_urlopen)
    monkeypatch.setattr(validator_core.time, "sleep", lambda _seconds: None)

    result = APIProbeValidator().evaluate(
        _task(),
        None,
        ExpectedAssertion(type="api_probe", url="https://example.test/health", retries=2),
    )

    assert result.verdict == Verdict.FAIL
    assert calls == 3


@pytest.mark.unit
def test_api_probe_validator_tls_verify_failure_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(*args: object, **kwargs: object) -> object:
        raise ssl.SSLError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(validator_core, "urlopen", fake_urlopen)

    result = APIProbeValidator().evaluate(
        _task(),
        None,
        ExpectedAssertion(type="api_probe", url="https://example.test/health"),
    )

    assert result.verdict == Verdict.FAIL
    assert "api probe failed" in result.message


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


@pytest.mark.unit
def test_composite_and_or_success_paths() -> None:
    task = _task()
    passing = ExpectedAssertion(
        type="tf_state", path="fptcloud_subnet.this.cidr", equals="172.26.221.0/24"
    )
    failing = ExpectedAssertion(type="tf_state", path="fptcloud_subnet.this.cidr", equals="bad")

    assert CompositeValidator().evaluate_all(task, _state(), [passing]).verdict == Verdict.PASS
    assert (
        CompositeValidator().evaluate_all(task, _state(), [failing, passing], mode="or").verdict
        == Verdict.PASS
    )

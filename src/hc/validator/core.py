"""Validation primitives for checklist assertions.

The live runner has service-specific validation paths, while the queue/checklist
pipeline needs a small reusable validator layer for Phase 4. These validators
operate on parsed Terraform state and side-effect-free probe callables so they
stay unit-testable without cloud access.
"""

from __future__ import annotations

import re
import ssl
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module
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


@dataclass(frozen=True)
class InVMConnection:
    transport: str
    host: str
    port: int
    username: str
    password: str | None = None
    private_key_path: str | None = None
    timeout_seconds: float = 30


@dataclass(frozen=True)
class ProbeOutput:
    stdout: str
    exit_code: int = 0


class Validator(Protocol):
    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        """Evaluate one expected assertion."""


def _string_match(value: Any, assertion: ExpectedAssertion) -> ValidationResult:
    text = "" if value is None else str(value)
    subject = assertion.path or assertion.probe
    if assertion.present is True and value in (None, ""):
        return ValidationResult(Verdict.FAIL, f"expected {subject} to be present")
    if assertion.absent is True:
        if value in (None, ""):
            return ValidationResult(Verdict.PASS, "assertion passed")
        return ValidationResult(Verdict.FAIL, f"expected {subject} to be absent")
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
    if assertion.regex_match is not None and re.search(assertion.regex_match, text) is None:
        return ValidationResult(
            Verdict.FAIL,
            f"expected {subject} to match /{assertion.regex_match}/, got {text!r}",
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
            if assertion.absent is True:
                return ValidationResult(Verdict.PASS, f"path absent: {assertion.path}")
            return ValidationResult(Verdict.FAIL, f"path not found: {assertion.path}")
        return _string_match(value, assertion)

    @staticmethod
    def resolve_path(tf_state: TFState, path: str) -> Any:
        parts = _path_parts(path)
        if parts and parts[0] == "$":
            return _walk(tf_state.raw, parts[1:])
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
    def __init__(
        self,
        probe_runner: Callable[[TaskSpec, ExpectedAssertion], str | dict[str, Any]] | None = None,
        ssh_client_factory: Callable[[], Any] | None = None,
        winrm_session_factory: Callable[..., Any] | None = None,
    ):
        self._probe_runner = probe_runner
        self._ssh_client_factory = ssh_client_factory
        self._winrm_session_factory = winrm_session_factory

    def evaluate(
        self, task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
    ) -> ValidationResult:
        try:
            if self._probe_runner is not None:
                output = _coerce_probe_output(self._probe_runner(task, assertion))
            else:
                connection = _resolve_in_vm_connection(task, tf_state, assertion)
                command = _build_in_vm_command(assertion, connection.transport)
                output = self._run_probe(connection, command)
            return _evaluate_probe_output(output, assertion)
        except ProbeAuthError as exc:
            return ValidationResult(Verdict.FAIL, str(exc))
        except ProbeConnectionError as exc:
            transport = (assertion.transport or assertion.os_type or "").lower()
            verdict = Verdict.FAIL if transport == "winrm" or "windows" in transport else Verdict.INCONCLUSIVE
            return ValidationResult(verdict, str(exc))
        except ProbeConfigError as exc:
            return ValidationResult(Verdict.FAIL, str(exc))
        except ProbeDependencyMissing as exc:
            return ValidationResult(Verdict.INCONCLUSIVE, str(exc))

    def _run_probe(self, connection: InVMConnection, command: str) -> ProbeOutput:
        if connection.transport == "winrm":
            return self._run_winrm(connection, command)
        return self._run_ssh(connection, command)

    def _run_ssh(self, connection: InVMConnection, command: str) -> ProbeOutput:
        client = self._make_ssh_client()
        try:
            client.connect(
                hostname=connection.host,
                port=connection.port,
                username=connection.username,
                password=connection.password,
                key_filename=connection.private_key_path,
                timeout=connection.timeout_seconds,
                banner_timeout=connection.timeout_seconds,
                auth_timeout=connection.timeout_seconds,
            )
            _stdin, stdout, stderr = client.exec_command(command, timeout=connection.timeout_seconds)
            exit_code = int(stdout.channel.recv_exit_status())
            stdout_text = _read_stream(stdout)
            stderr_text = _read_stream(stderr)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if "auth" in exc.__class__.__name__.lower() or "authentication" in message.lower():
                raise ProbeAuthError(f"SSH authentication failed: {message}") from exc
            raise ProbeConnectionError(f"SSH probe failed: {message}") from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return ProbeOutput(stdout=_combine_stdout_stderr(stdout_text, stderr_text), exit_code=exit_code)

    def _run_winrm(self, connection: InVMConnection, command: str) -> ProbeOutput:
        session_factory = self._winrm_session_factory or _default_winrm_session_factory
        try:
            session = session_factory(
                f"http://{connection.host}:{connection.port}/wsman",
                auth=(connection.username, connection.password or ""),
                transport="ntlm",
                server_cert_validation="ignore",
                operation_timeout_sec=int(connection.timeout_seconds),
                read_timeout_sec=int(connection.timeout_seconds + 10),
            )
            result = session.run_ps(command)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            class_name = exc.__class__.__name__.lower()
            if "auth" in class_name or "unauthorized" in class_name or "401" in message:
                raise ProbeAuthError(f"WinRM authentication failed: {message}") from exc
            raise ProbeConnectionError(f"WinRM probe failed: {message}") from exc
        stdout_text = _decode_bytes(getattr(result, "std_out", b""))
        stderr_text = _decode_bytes(getattr(result, "std_err", b""))
        exit_code = int(getattr(result, "status_code", 1))
        return ProbeOutput(stdout=_combine_stdout_stderr(stdout_text, stderr_text), exit_code=exit_code)

    def _make_ssh_client(self) -> Any:
        if self._ssh_client_factory is not None:
            return self._ssh_client_factory()
        try:
            paramiko = import_module("paramiko")
        except ImportError as exc:
            raise ProbeDependencyMissing("paramiko is required for SSH in-VM probes") from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return client


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
        attempts = max(1, int(assertion.retries or 0) + 1)
        timeout = assertion.timeout_seconds or 10
        method = assertion.method or "GET"
        context = None
        if assertion.tls_verify is False:
            context = ssl._create_unverified_context()
        last_result = ValidationResult(Verdict.FAIL, "api probe did not run")

        for attempt in range(1, attempts + 1):
            status = None
            body = ""
            try:
                request = Request(assertion.url or "", method=method)
                # URL comes from operator-authored probe config.
                with urlopen(request, timeout=timeout, context=context) as response:
                    status = int(response.status)
                    body = response.read().decode("utf-8", errors="replace")
            except HTTPError as exc:
                status = int(exc.code)
                body = exc.read().decode("utf-8", errors="replace")
            except (OSError, URLError) as exc:
                last_result = ValidationResult(Verdict.FAIL, f"api probe failed: {exc}")
            if status is not None:
                last_result = _evaluate_http_response(assertion, expected_status, status, body)
                if last_result.verdict == Verdict.PASS:
                    return last_result

            if attempt < attempts:
                time.sleep(min(0.25 * attempt, 1.0))

        return last_result


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


class ProbeConfigError(Exception):
    pass


class ProbeDependencyMissing(Exception):
    pass


class ProbeConnectionError(Exception):
    pass


class ProbeAuthError(Exception):
    pass


def _resolve_in_vm_connection(
    task: TaskSpec, tf_state: TFState | None, assertion: ExpectedAssertion
) -> InVMConnection:
    transport = _in_vm_transport(task, assertion)
    host = assertion.host
    if host is None and assertion.host_path and tf_state is not None:
        resolved_host = TFStateValidator.resolve_path(tf_state, assertion.host_path)
        host = None if resolved_host is _MISSING else str(resolved_host)
    attrs = _first_instance_attrs(tf_state)
    if host is None:
        host = _first_present(
            attrs,
            (
                "public_ip",
                "public_ip_address",
                "floating_ip",
                "floating_ip_address",
                "access_ip_v4",
                "ip_address",
                "private_ip",
            ),
        )
    if host is None and tf_state is not None:
        host = _first_present(
            {
                "public_ip": tf_state.get_output("public_ip"),
                "fip_address": tf_state.get_output("fip_address"),
                "ip_address": tf_state.get_output("ip_address"),
                "private_ip": tf_state.get_output("private_ip"),
            },
            ("public_ip", "fip_address", "ip_address", "private_ip"),
        )
    if not host:
        raise ProbeConfigError("in-VM probe requires host or host_path")

    username = assertion.username or _first_present(
        attrs, ("username", "admin_username", "user", "default_user")
    )
    password = assertion.password or _first_present(attrs, ("password", "admin_password"))
    if username is None:
        username = "Administrator" if transport == "winrm" else "ubuntu"
    private_key_path = assertion.private_key_path
    port = assertion.port or (5985 if transport == "winrm" else 22)
    timeout = assertion.timeout_seconds or 30

    return InVMConnection(
        transport=transport,
        host=host,
        port=port,
        username=username,
        password=password,
        private_key_path=private_key_path,
        timeout_seconds=timeout,
    )


def _in_vm_transport(task: TaskSpec, assertion: ExpectedAssertion) -> str:
    raw = assertion.transport or assertion.os_type or str(task.spec.get("vars", {}).get("os", ""))
    lowered = raw.lower()
    if lowered in {"winrm", "windows"} or "windows" in lowered:
        return "winrm"
    return "ssh"


def _build_in_vm_command(assertion: ExpectedAssertion, transport: str) -> str:
    if assertion.file_exists:
        path = assertion.file_exists
        if transport == "winrm":
            escaped = path.replace("'", "''")
            return f"if (Test-Path -LiteralPath '{escaped}') {{ 'true' }} else {{ 'false' }}"
        escaped = path.replace("'", "'\"'\"'")
        return f"test -e '{escaped}' && echo true || echo false"
    command = assertion.command or assertion.probe
    if not command:
        raise ProbeConfigError("in-VM probe requires command, probe, or file_exists")
    return command


def _evaluate_probe_output(output: ProbeOutput, assertion: ExpectedAssertion) -> ValidationResult:
    if assertion.exit_code is not None and output.exit_code != assertion.exit_code:
        return ValidationResult(
            Verdict.FAIL,
            f"expected exit_code {assertion.exit_code}, got {output.exit_code}",
        )
    if assertion.file_exists and "true" not in output.stdout.lower():
        return ValidationResult(Verdict.FAIL, f"file not found: {assertion.file_exists}")
    if assertion.stdout_contains is not None and assertion.stdout_contains not in output.stdout:
        return ValidationResult(
            Verdict.FAIL,
            f"expected stdout to contain {assertion.stdout_contains!r}, got {output.stdout!r}",
        )
    return _string_match(output.stdout, assertion)


def _coerce_probe_output(output: str | dict[str, Any]) -> ProbeOutput:
    if isinstance(output, dict):
        stdout = str(output.get("stdout", ""))
        exit_code = int(output.get("exit_code", 0))
        return ProbeOutput(stdout=stdout, exit_code=exit_code)
    return ProbeOutput(stdout=str(output), exit_code=0)


def _first_instance_attrs(tf_state: TFState | None) -> dict[str, Any]:
    if tf_state is None:
        return {}
    for resource in tf_state.resources:
        if resource.type == "fptcloud_instance" and resource.instances:
            return resource.instances[0].attributes
    for resource in tf_state.resources:
        if resource.instances:
            return resource.instances[0].attributes
    return {}


def _first_present(attrs: dict[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _default_winrm_session_factory(*args: Any, **kwargs: Any) -> Any:
    try:
        winrm = import_module("winrm")
    except ImportError as exc:
        raise ProbeDependencyMissing("pywinrm is required for WinRM in-VM probes") from exc
    return winrm.Session(*args, **kwargs)


def _read_stream(stream: Any) -> str:
    data = stream.read()
    return _decode_bytes(data)


def _decode_bytes(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _combine_stdout_stderr(stdout: str, stderr: str) -> str:
    if stderr:
        return f"{stdout}\n{stderr}".strip()
    return stdout


class _Missing:
    pass


_MISSING = _Missing()


def _path_parts(path: str) -> list[str]:
    normalized = path.strip()
    if normalized.startswith("$"):
        normalized = normalized.replace("[", ".[").replace("]", "")
    parts: list[str] = []
    for raw_part in normalized.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if part.startswith("["):
            part = part[1:]
        if (
            len(part) >= 2
            and part[0] in {"'", '"'}
            and part[-1] == part[0]
        ):
            part = part[1:-1]
        parts.append(part)
    return parts


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


def _evaluate_http_response(
    assertion: ExpectedAssertion, expected_status: int, status: int, body: str
) -> ValidationResult:
    if status != expected_status:
        return ValidationResult(Verdict.FAIL, f"expected HTTP {expected_status}, got {status}")
    match_result = _string_match(body, assertion)
    if match_result.verdict != Verdict.PASS:
        return match_result
    return ValidationResult(Verdict.PASS, f"HTTP {status}")

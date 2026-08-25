from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import IO, Iterable, Literal, cast

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import _RootedPathMissing, _RootedReader
from pre_pr_verify.models import ChangeSet
from pre_pr_verify.snapshot import (
    _git_snapshot_materialization_gap,
    _snapshot_materialization_gap,
    disposable_git_snapshot,
    disposable_snapshot,
)
from pre_pr_verify.verification import build_execution_request
from pre_pr_verify.verification_models import (
    CapabilityName,
    DecisionKind,
    EnvironmentProfile,
    ExecutionCapability,
    ExecutionDecision,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    FailureKind,
    OutputEvidence,
    RequirementLevel,
    SnapshotManifest,
    SourcePreservationFailure,
    VerificationEvidence,
    VerificationPlan,
    build_verification_evidence,
    derive_execution_decision,
)


_DirectGitClassification = Literal[
    "not_direct_git",
    "supported_bounded_git",
    "unsupported_bounded_git",
    "prohibited_profile_override",
]

_GIT_CONFIG_SELECTORS = (
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
)


def _classify_direct_git_request(argv: list[str]) -> _DirectGitClassification:
    """Classify only the frozen, deliberately tiny direct-Git surface."""

    if not argv or os.path.basename(argv[0]) != "git":
        return "not_direct_git"

    for token in argv[1:]:
        if token in {"-C", "-c", *_GIT_CONFIG_SELECTORS}:
            return "prohibited_profile_override"
        if token.startswith(("-C", "-c")):
            return "prohibited_profile_override"
        if any(token.startswith(selector + "=") for selector in _GIT_CONFIG_SELECTORS):
            return "prohibited_profile_override"

    args = argv[1:]
    if args in (["rev-parse", "HEAD"], ["rev-parse", "--show-toplevel"]):
        return "supported_bounded_git"

    if args and args[0] == "ls-files":
        pathspecs = args[1:]
        if pathspecs and pathspecs[0] == "--":
            pathspecs = pathspecs[1:]
            if all(pathspec != "" for pathspec in pathspecs):
                return "supported_bounded_git"
        elif all(pathspec != "" and not pathspec.startswith("-") for pathspec in pathspecs):
            return "supported_bounded_git"
        return "unsupported_bounded_git"

    if args and args[0] == "status":
        if args[1:] in ([], ["--porcelain"], ["--porcelain=v1"], ["--short"]):
            return "supported_bounded_git"
        return "unsupported_bounded_git"

    if args and args[0] == "diff":
        diff_args = args[1:]
        if diff_args in ([], ["--cached"]):
            return "supported_bounded_git"
        if diff_args and diff_args[0] == "--cached":
            diff_args = diff_args[1:]
        if diff_args and diff_args[0] == "--":
            if all(pathspec != "" for pathspec in diff_args[1:]):
                return "supported_bounded_git"
        return "unsupported_bounded_git"

    return "unsupported_bounded_git"


def _direct_git_gate_failure(
    environment_profile: EnvironmentProfile,
    argv: list[str],
) -> FailureKind | None:
    if environment_profile is not EnvironmentProfile.GIT_REPOSITORY:
        return None
    classification = _classify_direct_git_request(argv)
    if classification == "prohibited_profile_override":
        return FailureKind.CONFIGURATION
    if classification == "unsupported_bounded_git":
        return FailureKind.CAPABILITY
    return None


def decide_execution(
    request: ExecutionRequest, capability: ExecutionCapability
) -> ExecutionDecision:
    return derive_execution_decision(request, capability)


class _Collector:
    _MAX_REDACTION_VALUES = 256
    _MAX_REDACTION_PATTERN_BYTES = 4096
    _REDACTION_MARKER = b"[REDACTED]"

    def __init__(self, limit: int, redaction_values: Iterable[str]):
        self.limit = limit
        self.digest = hashlib.sha256()
        self.total = 0
        self.excerpt = bytearray()
        self.truncated = False
        self.redacted = False
        self._pending = bytearray()
        values = tuple(value.encode("utf-8") for value in redaction_values if value)
        self._patterns = tuple(sorted(set(values), key=len, reverse=True))
        self._matcher: re.Pattern[bytes] | None = None
        marker_collides = any(
            self._overlaps(self._REDACTION_MARKER, pattern)
            for pattern in self._patterns
        )
        self._suppress_output = (
            len(values) > self._MAX_REDACTION_VALUES
            or any(len(value) > self._MAX_REDACTION_PATTERN_BYTES for value in values)
            or marker_collides
        )
        if not self._suppress_output and self._patterns:
            try:
                # A positive lookahead visits every input offset.  The
                # captured alternation therefore finds overlapping and
                # self-overlapping literals in one bounded matcher.
                alternatives = b"|".join(re.escape(pattern) for pattern in self._patterns)
                self._matcher = re.compile(b"(?=(" + alternatives + b"))")
            except re.error:
                # A bounded pattern set should compile, but redaction must
                # fail closed if the runtime rejects it.
                self._suppress_output = True

    @staticmethod
    def _overlaps(left: bytes, right: bytes) -> bool:
        if left in right or right in left:
            return True
        for size in range(1, min(len(left), len(right))):
            if left[-size:] == right[:size] or right[-size:] == left[:size]:
                return True
        return False

    def feed(self, chunk: bytes) -> None:
        self.digest.update(chunk)
        self.total += len(chunk)
        if self._suppress_output:
            self.truncated = self.total > 0
            self.redacted = True
            return
        if not self._patterns:
            self._append(chunk)
            return
        self._pending.extend(chunk)
        self._drain()

    def _append(self, value: bytes) -> None:
        remaining = self.limit - len(self.excerpt)
        if remaining <= 0:
            if value:
                self.truncated = True
            return
        self.excerpt.extend(value[:remaining])
        if len(value) > remaining:
            self.truncated = True

    def _find_matches(self, data: bytes) -> list[tuple[int, int]]:
        if self._matcher is None:
            return []
        return [
            (match.start(), match.start() + len(match.group(1)))
            for match in self._matcher.finditer(data)
        ]

    def _redact_prefix(
        self, data: bytes, boundary: int, matches: list[tuple[int, int]]
    ) -> tuple[bytes, bool]:
        intervals = sorted(
            ((start, end) for start, end in matches if end <= boundary),
            key=lambda item: (item[0], -item[1]),
        )
        output = bytearray()
        cursor = 0
        replaced = False
        for start, end in intervals:
            if start < cursor:
                cursor = max(cursor, end)
                continue
            output.extend(data[cursor:start])
            output.extend(self._REDACTION_MARKER)
            cursor = end
            replaced = True
        output.extend(data[cursor:boundary])
        return bytes(output), replaced

    def _drain(self, *, final: bool = False) -> None:
        if self._suppress_output or not self._pending:
            return
        data = bytes(self._pending)
        matches = self._find_matches(data)
        if final:
            boundary = len(data)
            partial_starts = [
                len(data) - size
                for pattern in self._patterns
                for size in range(1, min(len(pattern), len(data) + 1))
                if data.endswith(pattern[:size])
            ]
            if partial_starts:
                boundary = min(partial_starts)
        else:
            boundary = max(0, len(data) - max(map(len, self._patterns)) - 1)
        # Never emit a prefix of a match that straddles the retained boundary.
        # This applies to both streaming and final drains, including a
        # self-overlapping match whose later occurrence starts before the
        # boundary.
        while boundary > 0:
            crossing = [
                start
                for start, end in matches
                if start < boundary and end > boundary
            ]
            if not crossing:
                break
            boundary = min(crossing)
        if boundary <= 0:
            return
        transformed, replaced = self._redact_prefix(data, boundary, matches)
        self._append(transformed)
        self.redacted = self.redacted or replaced
        del self._pending[:boundary]

    def finish(self) -> None:
        if self._suppress_output or not self._patterns:
            self._pending.clear()
            return
        self._drain(final=True)
        if self._pending:
            # The stream ended with a possible secret prefix. Do not persist
            # that prefix merely because no completing byte arrived.
            self._append(self._REDACTION_MARKER)
            self.redacted = True
            self._pending.clear()


def _empty_output() -> OutputEvidence:
    return OutputEvidence(
        excerpt="",
        sha256=hashlib.sha256(b"").hexdigest(),
        total_bytes=0,
        truncated=False,
        redacted=False,
    )


def _output_evidence(collector: _Collector) -> OutputEvidence:
    collector.finish()
    text = bytes(collector.excerpt).decode("utf-8", "backslashreplace")
    return OutputEvidence(
        excerpt=text,
        sha256=collector.digest.hexdigest(),
        total_bytes=collector.total,
        truncated=collector.truncated,
        redacted=collector.redacted,
    )


def _sanitized_environment(snapshot_root: Path) -> dict[str, str]:
    temporary = snapshot_root / ".pre-pr-verify-tmp"
    home = snapshot_root / ".pre-pr-verify-home"
    temporary.mkdir(mode=0o700, exist_ok=True)
    home.mkdir(mode=0o700, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _not_run_result(
    request: ExecutionRequest,
    capability: ExecutionCapability,
    decision: ExecutionDecision,
    *,
    failure_kind: FailureKind | None = None,
) -> ExecutionResult:
    classified_failure = failure_kind or decision.blocked_failure_kind
    if classified_failure is None:
        classified_failure = FailureKind.CAPABILITY
    return ExecutionResult(
        request=request,
        capability=capability,
        decision=decision,
        status=ExecutionStatus.NOT_RUN,
        failure_kind=classified_failure,
        exit_code=None,
        duration_ms=0,
        stdout=_empty_output(),
        stderr=_empty_output(),
        required_evidence_gap=request.requirement_level is RequirementLevel.REQUIRED,
    )


def _runtime_error_result(
    request: ExecutionRequest,
    capability: ExecutionCapability,
    decision: ExecutionDecision,
    failure_kind: FailureKind,
) -> ExecutionResult:
    return ExecutionResult(
        request=request,
        capability=capability,
        decision=decision,
        status=ExecutionStatus.ERRORED,
        failure_kind=failure_kind,
        exit_code=None,
        duration_ms=0,
        stdout=_empty_output(),
        stderr=_empty_output(),
        required_evidence_gap=request.requirement_level is RequirementLevel.REQUIRED,
    )


def _process_group_alive(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is None:
        return True
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # An indeterminate process-group query is a live-group safety concern.
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Bound cleanup after a deadline; never wait forever for a broken host."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    return not _process_group_alive(process)


def _validated_cwd(root: Path, request: ExecutionRequest) -> Path:
    if request.cwd == ".":
        return root
    if any(part.lower() == ".git" for part in request.cwd.split("/")):
        raise PermissionError("execution cwd cannot enter .git")
    raw_path = os.fsencode(request.cwd)
    reader = _RootedReader(root, "execution cwd")
    metadata = reader.stat(raw_path)
    if metadata is None:
        raise FileNotFoundError(request.cwd)
    if stat.S_ISLNK(metadata.st_mode):
        raise PermissionError("execution cwd is a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(request.cwd)
    return Path(os.fsdecode(os.fsencode(root) + b"/" + raw_path))


def execute_request(
    request: ExecutionRequest,
    capability: ExecutionCapability,
    snapshot_root: Path,
    *,
    redaction_values: Iterable[str] = (),
) -> ExecutionResult:
    # A caller may provide a one-shot iterable (for example, a secret store
    # view). Materialize it once so both output streams receive the same set.
    redaction_values = tuple(redaction_values)
    decision = decide_execution(request, capability)
    if decision.kind is not DecisionKind.EXECUTABLE:
        return _not_run_result(
            request,
            capability,
            decision,
            failure_kind=request.cwd_validation_failure,
        )
    try:
        root = snapshot_root.absolute()
        cwd = _validated_cwd(root, request)
    except (_RootedPathMissing, FileNotFoundError):
        blocked_request = request.model_copy(
            update={"cwd_validation_failure": FailureKind.CONFIGURATION}
        )
        blocked_decision = decide_execution(blocked_request, capability)
        return _not_run_result(
            blocked_request,
            capability,
            blocked_decision,
            failure_kind=FailureKind.CONFIGURATION,
        )
    except (NotADirectoryError, OSError, PreflightError, RuntimeError, ValueError):
        blocked_request = request.model_copy(
            update={"cwd_validation_failure": FailureKind.PERMISSION}
        )
        blocked_decision = decide_execution(blocked_request, capability)
        return _not_run_result(
            blocked_request,
            capability,
            blocked_decision,
            failure_kind=FailureKind.PERMISSION,
        )

    stdout_collector = _Collector(request.output_limit_bytes, redaction_values)
    stderr_collector = _Collector(request.output_limit_bytes, redaction_values)
    started = time.monotonic()
    status = ExecutionStatus.ERRORED
    failure_kind: FailureKind | None = FailureKind.INFRASTRUCTURE
    exit_code: int | None = None
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[IO[bytes], _Collector]] = {}
    timed_out = False
    process_group_finished = False
    try:
        process = subprocess.Popen(
            list(request.argv),
            cwd=cwd,
            env=_sanitized_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        for stream, collector in (
            (process.stdout, stdout_collector),
            (process.stderr, stderr_collector),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, collector)
            streams[stream.fileno()] = (stream, collector)
        deadline = started + request.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if streams:
                ready = selector.select(remaining)
                if not ready:
                    timed_out = True
                    break
                for key, _ in ready:
                    stream = cast(IO[bytes], key.fileobj)
                    collector = key.data
                    try:
                        chunk = stream.read(65_536)
                    except (BlockingIOError, OSError):
                        continue
                    if chunk is None:
                        continue
                    if chunk:
                        collector.feed(chunk)
                        continue
                    selector.unregister(stream)
                    streams.pop(stream.fileno(), None)
                    stream.close()
            elif not _process_group_alive(process):
                process_group_finished = True
                break
            else:
                time.sleep(min(0.01, remaining))
        if timed_out:
            _terminate_process_group(process)
            status = ExecutionStatus.TIMED_OUT
            failure_kind = FailureKind.INFRASTRUCTURE
        else:
            try:
                exit_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                status = ExecutionStatus.TIMED_OUT
                failure_kind = FailureKind.INFRASTRUCTURE
            else:
                if exit_code == 0:
                    status = ExecutionStatus.PASSED
                    failure_kind = None
                else:
                    status = ExecutionStatus.FAILED
                    failure_kind = request.nonzero_failure_kind
    except PermissionError:
        status = ExecutionStatus.ERRORED
        failure_kind = FailureKind.PERMISSION
    except FileNotFoundError:
        status = ExecutionStatus.ERRORED
        failure_kind = FailureKind.CONFIGURATION
    except OSError:
        status = ExecutionStatus.ERRORED
        failure_kind = FailureKind.INFRASTRUCTURE
    except Exception:
        status = ExecutionStatus.ERRORED
        failure_kind = FailureKind.INFRASTRUCTURE
    finally:
        if process is not None and not process_group_finished and not timed_out:
            _terminate_process_group(process)
        for key in list(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
            try:
                cast(IO[bytes], key.fileobj).close()
            except OSError:
                pass
        if process is not None:
            streams_to_close: list[IO[bytes]] = []
            if process.stdout is not None:
                streams_to_close.append(process.stdout)
            if process.stderr is not None:
                streams_to_close.append(process.stderr)
            for stream in streams_to_close:
                try:
                    selector.unregister(stream)
                except (KeyError, ValueError):
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
        selector.close()
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    required_gap = (
        request.requirement_level is RequirementLevel.REQUIRED
        and not (
            status is ExecutionStatus.PASSED
            or (
                status is ExecutionStatus.FAILED
                and failure_kind is FailureKind.VERIFICATION
            )
        )
    )
    return ExecutionResult(
        request=request,
        capability=capability,
        decision=decision,
        status=status,
        failure_kind=failure_kind,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=_output_evidence(stdout_collector),
        stderr=_output_evidence(stderr_collector),
        required_evidence_gap=required_gap,
    )


def execute_verification_plan(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    capability: ExecutionCapability,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    required_capabilities: Iterable[CapabilityName],
    redaction_values: Iterable[str] = (),
) -> VerificationEvidence:
    executions = []
    source_preservation_failures: list[SourcePreservationFailure] = []
    required_capabilities = tuple(required_capabilities)
    redaction_values = tuple(redaction_values)
    command_ordinal = 0
    for check in plan.checks:
        if check.argv is None:
            continue
        execution_record = None
        direct_git_failure = _direct_git_gate_failure(
            check.environment_profile, check.argv
        )
        if direct_git_failure is not None:
            # Reject unsupported direct Git before materialization or child
            # process creation.  The incomplete manifest is only the bound
            # evidence record for the pre-execution gate; it is not a
            # partially materialized execution environment.
            gap_snapshot = _git_snapshot_materialization_gap(
                changeset,
                discovery,
                materialization_ordinal=command_ordinal,
                failure=direct_git_failure,
                object_format=None,
            )
            request = build_execution_request(
                check,
                gap_snapshot,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
                required_capabilities=required_capabilities,
                snapshot_validation_failure=direct_git_failure,
            )
            result = execute_request(
                request,
                capability,
                Path(changeset.repository_root),
                redaction_values=redaction_values,
            )
            executions.append((gap_snapshot, result))
            command_ordinal += 1
            continue
        try:
            materializer = (
                disposable_git_snapshot
                if check.environment_profile is EnvironmentProfile.GIT_REPOSITORY
                else disposable_snapshot
            )
            with materializer(
                changeset,
                discovery,
                plan=plan,
                materialization_ordinal=command_ordinal,
            ) as snapshot:
                request = build_execution_request(
                    check,
                    snapshot.manifest,
                    timeout_seconds=timeout_seconds,
                    output_limit_bytes=output_limit_bytes,
                    required_capabilities=required_capabilities,
                )
                result = execute_request(
                    request,
                    capability,
                    snapshot.path,
                    redaction_values=redaction_values,
                )
                execution_record = (snapshot.manifest, result)
        except PreflightError:
            if execution_record is None:
                # The ChangeSet and plan already establish review scope. A
                # later inability to materialize complete content is evidence
                # that the command could not run, not a new capture failure.
                if check.environment_profile is EnvironmentProfile.GIT_REPOSITORY:
                    gap_snapshot = _git_snapshot_materialization_gap(
                        changeset,
                        discovery,
                        materialization_ordinal=command_ordinal,
                        failure=FailureKind.CAPABILITY,
                        object_format=None,
                    )
                else:
                    gap_snapshot = _snapshot_materialization_gap(
                        changeset,
                        discovery,
                        materialization_ordinal=command_ordinal,
                    )
                request = build_execution_request(
                    check,
                    gap_snapshot,
                    timeout_seconds=timeout_seconds,
                    output_limit_bytes=output_limit_bytes,
                    required_capabilities=required_capabilities,
                    snapshot_validation_failure=FailureKind.CAPABILITY,
                )
                result = execute_request(
                    request,
                    capability,
                    Path(changeset.repository_root),
                    redaction_values=redaction_values,
                )
                executions.append((gap_snapshot, result))
            else:
                # The child already ran. Preserve its actual status/output;
                # source mutation discovered by the final recapture is a
                # separate deterministic evidence gap.
                executions.append(execution_record)
                snapshot_manifest, _result = execution_record
                if _result.status is not ExecutionStatus.NOT_RUN:
                    source_preservation_failures.append(
                        SourcePreservationFailure(
                            ordinal=command_ordinal,
                            check_id=check.check_id,
                            snapshot_identity=snapshot_manifest.identity,
                            reason="source repository changed after command execution",
                        )
                    )
        else:
            assert execution_record is not None
            executions.append(execution_record)
        command_ordinal += 1
    return build_verification_evidence(
        plan,
        executions,
        source_preservation_failures=source_preservation_failures,
    )

import base64

import pytest
from pydantic import ValidationError

from pre_pr_verify.models import (
    ChangeOrigin,
    ChangeSet,
    Comparison,
    ContentBlob,
    ContentLimits,
    FileChange,
    FileKind,
    FileState,
    IdentityKind,
    RawPath,
    ScopeMode,
    build_changeset,
)


def path(value: bytes) -> RawPath:
    return RawPath.from_bytes(value)


def regular_state(value: bytes, content: bytes) -> FileState:
    digest = __import__("hashlib").sha256(content).hexdigest()
    return FileState(
        path=path(value),
        kind=FileKind.REGULAR,
        mode="100644",
        size=len(content),
        identity_kind=IdentityKind.SHA256,
        content_identity=digest,
        binary=False,
        content_captured=True,
    )


def test_raw_path_is_reversible_and_safe_for_hostile_bytes() -> None:
    raw = b"line\n\xff\tname"
    encoded = RawPath.from_bytes(raw)

    assert encoded.raw_b64 == base64.b64encode(raw).decode("ascii")
    assert encoded.to_bytes() == raw
    assert encoded.utf8 is None
    assert "\\n" in encoded.display
    assert "\\xff" in encoded.display


def test_changeset_identity_is_deterministic_and_validated() -> None:
    content = b"new\n"
    state = regular_state(b"src/example.py", content)
    kwargs = dict(
        repository_root="/tmp/repo",
        scope=ScopeMode.PENDING,
        comparison=Comparison(
            requested_base_ref="main",
            resolved_base_commit="1" * 40,
            merge_base_commit="1" * 40,
            head_commit="2" * 40,
        ),
        limits=ContentLimits(per_file_bytes=1024, total_bytes=4096),
        changes=[
            FileChange(
                origins=[ChangeOrigin.UNTRACKED],
                base=FileState.absent(path(b"src/example.py")),
                head=FileState.absent(path(b"src/example.py")),
                index=FileState.absent(path(b"src/example.py")),
                working=state,
                effective=state,
            )
        ],
        contents=[
            ContentBlob(
                sha256=state.content_identity,
                size=len(content),
                data_b64=base64.b64encode(content).decode("ascii"),
            )
        ],
    )

    first = build_changeset(**kwargs)
    second = build_changeset(**kwargs)

    assert first.identity == second.identity
    assert first.empty is False
    assert ChangeSet.model_validate_json(first.model_dump_json()) == first

    payload = first.model_dump(mode="json")
    payload["identity"] = "0" * 64
    with pytest.raises(ValidationError, match="identity"):
        ChangeSet.model_validate(payload)


def test_empty_changeset_is_valid_and_has_identity() -> None:
    changeset = build_changeset(
        repository_root="/tmp/repo",
        scope=ScopeMode.COMMITTED_ONLY,
        comparison=Comparison(
            requested_base_ref="main",
            resolved_base_commit="1" * 40,
            merge_base_commit="1" * 40,
            head_commit="1" * 40,
        ),
        limits=ContentLimits(),
        changes=[],
        contents=[],
    )

    assert changeset.empty is True
    assert len(changeset.identity) == 64


def test_captured_content_reference_must_exist() -> None:
    state = regular_state(b"missing.txt", b"missing")
    with pytest.raises(ValidationError, match="content blob"):
        build_changeset(
            repository_root="/tmp/repo",
            scope=ScopeMode.PENDING,
            comparison=Comparison(
                requested_base_ref="main",
                resolved_base_commit="1" * 40,
                merge_base_commit="1" * 40,
                head_commit="2" * 40,
            ),
            limits=ContentLimits(),
            changes=[
                FileChange(
                    origins=[ChangeOrigin.UNSTAGED],
                    base=state,
                    head=state,
                    index=state,
                    working=state,
                    effective=state,
                )
            ],
            contents=[],
        )


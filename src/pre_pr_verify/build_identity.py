from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Final


MAX_CORE_FILES: Final = 256
MAX_CORE_BYTES: Final = 16 * 1_048_576


def installed_core_identity(package_root: Path | str | None = None) -> str:
    """Return a bounded content identity for the installed Python verifier core.

    The identity intentionally does not inspect Git or the target repository.
    Callers still supply it independently to ReviewArtifact construction/loading.
    """

    root = Path(package_root) if package_root is not None else Path(__file__).parent
    files = sorted(root.glob("*.py"), key=lambda path: path.name.encode("utf-8"))
    if not files or len(files) > MAX_CORE_FILES:
        raise RuntimeError("installed verifier core file set is outside its bound")

    digest = hashlib.sha256()
    total = 0
    for path in files:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("installed verifier core contains a non-regular Python file")
        content = path.read_bytes()
        total += len(content)
        if total > MAX_CORE_BYTES:
            raise RuntimeError("installed verifier core content exceeds its identity bound")
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"core-sha256:{digest.hexdigest()}"

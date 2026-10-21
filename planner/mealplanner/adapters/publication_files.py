"""PublicationWriter that writes a bundle into an output directory (the Pages ``publication/`` layout).

Sequence: build the complete new bundle (new index + ``publications/<id>/``) in a staging
directory inside the output directory, validate exactly those bytes, move the publication
directory into place, and only then atomically replace ``index.json`` (temp file +
``os.replace``). Any failure before the index swap removes the staging directory and the new
publication directory and leaves the previous ``index.json`` untouched. Earlier publication
directories are kept (no retention cleanup yet).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..application.publication import PublicationBundle, PublicationError, check_bundle
from ..domain.snapshot import JsonObject

# Full-bundle validator: receives a directory laid out like the deployed ``publication/``
# directory (index.json + publications/<id>/...) and returns error strings.
BundleValidator = Callable[[Path], Sequence[str]]


def _dump(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


class FilePublicationWriter:
    """Implements PublicationWriter. ``validate`` is the optional full (JSON-schema) validator."""

    def __init__(self, out_dir: Path | str, validate: BundleValidator | None = None) -> None:
        self.out_dir = Path(out_dir)
        self._validate = validate

    def write(self, bundle: PublicationBundle) -> None:
        pid = bundle.publication_id
        final_dir = self.out_dir / "publications" / pid
        if final_dir.exists():
            raise PublicationError(f"publication directory already exists: {final_dir}")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        staging = self.out_dir / f".staging-{pid}"
        tmp_index = self.out_dir / f".index-{pid}.json.tmp"
        moved = False
        try:
            if staging.exists():
                raise PublicationError(f"staging directory already exists: {staging}")
            for rel, content in bundle.files.items():
                _write_text(staging / "publications" / pid / rel, _dump(content))
            _write_text(staging / "index.json", _dump(bundle.index))
            self._check(staging, bundle)
            (self.out_dir / "publications").mkdir(exist_ok=True)
            os.rename(staging / "publications" / pid, final_dir)  # same filesystem: atomic
            moved = True
            _write_text(tmp_index, _dump(bundle.index))
            os.replace(tmp_index, self.out_dir / "index.json")  # the only step that activates it
        except BaseException:
            if moved:
                shutil.rmtree(final_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            tmp_index.unlink(missing_ok=True)

    def _check(self, staging: Path, bundle: PublicationBundle) -> None:
        """Re-read what was written and validate it (python coherence checks, then the full validator)."""
        pub_dir = staging / "publications" / bundle.publication_id
        try:
            index = json.loads((staging / "index.json").read_text(encoding="utf-8"))
            files: Mapping[str, JsonObject] = {
                p.relative_to(pub_dir).as_posix(): json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(pub_dir.rglob("*"))
                if p.is_file()
            }
            errors = check_bundle(index, files)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors = [f"written bundle is unreadable or malformed: {exc!r}"]
        if not errors and self._validate is not None:
            errors = list(self._validate(staging))
        if errors:
            raise PublicationError("written bundle failed validation", errors)

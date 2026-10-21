"""PublicationRepository over a git working copy of the public repository (subprocess git, no shell).

The remote ``main`` is authoritative: ``sync`` clones if the working copy is absent, otherwise fetches
and hard-resets to ``origin/main`` (local leftovers of a failed attempt vanish). Pushes are never
forced. Authentication is an SSH deploy key given by path (``GIT_SSH_COMMAND`` with pinned host keys,
``BatchMode``); error messages shown to callers are scrubbed of key, known_hosts and working copy paths.

Layout written into the repository: ``publication/`` (``index.json`` + the newest ``KEEP_PUBLICATIONS``
``publications/<id>/`` directories, via ``FilePublicationWriter``) and ``app-ref`` (one line: the
meal-prep commit whose frontend code renders this data).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from ..application.publication import PublicationBundle
from ..application.publishing import PublishedContent, PublishRepoError
from ..domain.snapshot import JsonObject
from .publication_files import BundleValidator, FilePublicationWriter

KEEP_PUBLICATIONS = 3
BRANCH = "main"
AUTHOR_NAME = "meal-prep publisher"
AUTHOR_EMAIL = "publisher@meal-prep.invalid"
NETWORK_TIMEOUT_SECONDS = 90.0
LOCAL_TIMEOUT_SECONDS = 30.0
PUBLICATION_DIR = "publication"
APP_REF_FILE = "app-ref"
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+@")


def prune_publications(publication_dir: Path, keep: int = KEEP_PUBLICATIONS) -> None:
    """Keep the newest ``keep`` publication directories (ids start with a UTC timestamp) and the active one."""
    root = publication_dir / "publications"
    if not root.is_dir():
        return
    active = None
    try:
        active = json.loads((publication_dir / "index.json").read_text(encoding="utf-8")).get("publication_id")
    except (OSError, ValueError):
        pass
    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    for name in names[: max(len(names) - keep, 0)]:
        if name != active:
            shutil.rmtree(root / name, ignore_errors=True)


class GitPublicationRepository:
    def __init__(
        self,
        remote_url: str,
        workdir: Path | str,
        *,
        key_path: Path | str,
        known_hosts_path: Path | str,
        validate: BundleValidator | None = None,
        network_timeout: float = NETWORK_TIMEOUT_SECONDS,
        local_timeout: float = LOCAL_TIMEOUT_SECONDS,
        keep: int = KEEP_PUBLICATIONS,
    ) -> None:
        self._remote = remote_url
        self._dir = Path(workdir)
        self._key = str(key_path)
        self._known_hosts = str(known_hosts_path)
        self._validate = validate
        self._network_timeout = network_timeout
        self._local_timeout = local_timeout
        self._keep = keep

    # ------------------------------------------------------------ port

    def sync(self) -> PublishedContent | None:
        if (self._dir / ".git").is_dir():
            self._git("remote", "set-url", "origin", self._remote)
            self._git("fetch", "--prune", "origin", BRANCH, network=True)
            self._git("checkout", "--force", "-B", BRANCH, f"origin/{BRANCH}")
            self._git("reset", "--hard", f"origin/{BRANCH}")
            self._git("clean", "-fdx")
        else:
            if self._dir.exists():  # an interrupted clone or foreign content: this directory is ours alone
                shutil.rmtree(self._dir, ignore_errors=True)
            self._dir.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                ["clone", "--branch", BRANCH, "--no-tags", self._remote, str(self._dir)],
                cwd=self._dir.parent,
                timeout=self._network_timeout,
                step="clone",
            )
        return self._published()

    def write(self, bundle: PublicationBundle, app_ref: str) -> None:
        out = self._dir / PUBLICATION_DIR
        FilePublicationWriter(out, self._validate).write(bundle)
        prune_publications(out, self._keep)
        (self._dir / APP_REF_FILE).write_text(app_ref.strip() + "\n", encoding="utf-8")

    def commit_and_push(self, message: str) -> str:
        self._git("add", "--all", "--", PUBLICATION_DIR, APP_REF_FILE)
        if not self._git("status", "--porcelain").strip():
            raise PublishRepoError("There is nothing to commit.")
        self._git(
            "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
            "commit", "--quiet", "--no-verify", "-m", message,
            step="commit",
        )
        sha = self._git("rev-parse", "HEAD").strip()
        self._git("push", "origin", f"HEAD:refs/heads/{BRANCH}", network=True)  # never --force
        return sha

    # ------------------------------------------------------------ internals

    def _published(self) -> PublishedContent | None:
        out = self._dir / PUBLICATION_DIR
        try:
            index = json.loads((out / "index.json").read_text(encoding="utf-8"))
            pid = index["publication_id"]
            pub_dir = out / "publications" / pid
            files: Mapping[str, JsonObject] = {
                p.relative_to(pub_dir).as_posix(): json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(pub_dir.rglob("*"))
                if p.is_file()
            }
            if "manifest.json" not in files:
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None  # nothing (readable) published yet: the next publish writes a fresh bundle
        try:
            app_ref = (self._dir / APP_REF_FILE).read_text(encoding="utf-8").strip() or None
        except OSError:
            app_ref = None
        return PublishedContent(pid, index, files, app_ref)

    def _env(self) -> dict[str, str]:
        ssh = (
            f"ssh -i {shlex.quote(self._key)} -F /dev/null -o IdentitiesOnly=yes"
            f" -o StrictHostKeyChecking=yes -o UserKnownHostsFile={shlex.quote(self._known_hosts)}"
            " -o BatchMode=yes -o ConnectTimeout=20"
        )
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
        env.update(
            GIT_SSH_COMMAND=ssh,
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            LC_ALL="C",
        )
        return env

    def _git(self, *args: str, network: bool = False, step: str | None = None) -> str:
        return self._run(
            list(args),
            cwd=self._dir,
            timeout=self._network_timeout if network else self._local_timeout,
            step=step or args[0],
        )

    def _run(self, args: list[str], *, cwd: Path, timeout: float, step: str) -> str:
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", *args]
        try:
            done = subprocess.run(
                command, cwd=cwd, env=self._env(), capture_output=True, text=True, timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise PublishRepoError("git is not installed.") from exc
        except subprocess.TimeoutExpired as exc:
            raise PublishRepoError(f"git {step} timed out after {int(timeout)} seconds.") from exc
        except OSError as exc:
            raise PublishRepoError(f"git {step} could not start.") from exc
        if done.returncode != 0:
            raise PublishRepoError(f"git {step} failed: {self._scrub(done.stderr or done.stdout)}")
        return done.stdout

    def _scrub(self, text: str) -> str:
        for secret in (self._key, self._known_hosts, str(self._dir), str(self._dir.parent)):
            if secret:
                text = text.replace(secret, "<path>")
        text = _URL_CREDENTIALS.sub("", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(lines[-3:])[:300] or "no details"

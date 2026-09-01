"""Storing the provider key somewhere the next start will find it.

The console can hand this service a key. That is a boundary this repository did
not have before -- no other endpoint writes a secret, or writes configuration at
all -- and the shape of the service is most of what keeps it narrow:

* it is handed a path, and never works out its own. Deciding *where* secrets
  live is a configuration decision, and configuration decisions are made in
  ``bootstrap``; a service that resolved its own path would be a second place
  that knows, and the two would disagree the first time one of them moved.
* it never returns a key. ``status`` returns a fingerprint -- four characters --
  and there is no method that returns more. A read-back endpoint is the thing
  that turns "someone reached the loopback API" into "someone has the key", and
  the cheapest way not to have one is not to write one.
* it refuses to write inside the checkout. ``zip -r`` and Finder's "Compress"
  ignore ``.gitignore``, so a secret under the working tree leaves the machine
  the first time somebody archives the folder. That rule was documented before
  this service existed; here it is enforced.

What it deliberately cannot do is make a stored key take effect. A process reads
its key once, at composition, and the API mounts the chat routes only when that
read found one -- so a key stored now is a key the *next* start will use.
``ProviderKeyStatus`` carries that as two separate fields rather than one, because
a settings page that reported "configured" for a key nothing is using yet would
be answering a question nobody asked.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Directory mode for the parent, file mode for the key. Both are advisory on
#: Windows, where ``chmod`` moves only the read-only bit -- there the file
#: inherits the profile's ACL, which is already owner-only. Said rather than
#: assumed: "0600" in a comment is a claim the filesystem may not be making.
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class ProviderKeyRefused(ValueError):
    """The key was not stored, and the message names the rule that stopped it."""


@dataclass(frozen=True, slots=True)
class ProviderKeyStatus:
    """What is stored, what is running, and whether those are the same thing."""

    #: A key this process was composed with. It is the only one that can answer.
    active: bool
    #: A key on disk. It will be read at the next start.
    stored: bool
    #: The last four characters of the stored key, or of the active one when
    #: nothing is stored. Never more than four, and never the key.
    fingerprint: str | None
    #: Where a stored key lives, folded to `~` for display. ``None`` when this
    #: deployment declares no key file at all.
    path: str | None
    #: True when storing has already happened but cannot have taken effect yet.
    restart_required: bool


def fingerprint(value: str) -> str:
    """Enough of a key to recognise, not enough to use.

    The last four characters, and only when there are enough that four are not
    most of it. Not a prefix: providers put a fixed marker there (``sk-``), so a
    prefix identifies the vendor and two different keys would show the same one.
    """
    return f"…{value[-4:]}" if len(value) >= 12 else "…"


@dataclass(frozen=True, slots=True)
class ProviderKeyStore:
    """Reads and writes one key file, and refuses the paths it must refuse."""

    key_file: Path | None
    checkout_root: Path | None

    def read(self) -> str | None:
        """The stored key, with every whitespace character removed."""
        if self.key_file is None:
            return None
        try:
            raw = self.key_file.read_text(encoding="utf-8")
        except OSError:
            # Missing, unreadable, a directory: one answer, as in scripts/dev.sh.
            return None
        return "".join(raw.split()) or None

    def status(self, *, active_key: str | None) -> ProviderKeyStatus:
        stored = self.read()
        # The stored key when there is one, otherwise whatever this process is
        # running on. A settings page showing nothing while chat plainly works
        # would be reporting on the file rather than on the deployment.
        shown = stored or active_key
        return ProviderKeyStatus(
            active=active_key is not None,
            stored=stored is not None,
            fingerprint=fingerprint(shown) if shown is not None else None,
            path=_display(self.key_file),
            # Storing a key that is already the one running is not a reason to
            # restart anything, and saying otherwise would send someone to
            # restart a process that would come back identical.
            restart_required=stored is not None and stored != active_key,
        )

    def store(self, value: str) -> ProviderKeyStatus:
        cleaned = "".join(value.split())
        if not cleaned:
            raise ProviderKeyRefused("这把 key 是空的")
        target = self._writable_target()

        target.parent.mkdir(parents=True, exist_ok=True)
        _chmod(target.parent, DIRECTORY_MODE)

        # Written to a neighbour and renamed rather than opened and truncated.
        # A crash halfway through the second spelling of a key would otherwise
        # leave a file that is readable, non-empty and wrong -- which fails at
        # the provider, one layer further out than the mistake.
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".key-")
        scratch = Path(temporary)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(cleaned + "\n")
            _chmod(scratch, FILE_MODE)
            scratch.replace(target)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
        return self.status(active_key=None)

    def clear(self) -> bool:
        """Remove the stored key. ``False`` when there was nothing to remove."""
        target = self._writable_target()
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ProviderKeyRefused(f"删不掉 {_display(target)}：{exc}") from exc
        return True

    def _writable_target(self) -> Path:
        if self.key_file is None:
            raise ProviderKeyRefused(
                "这台部署声明了「没有 key 文件」（AW_KEY_FILE 是空串），"
                "所以没有可写的地方；把它取消设置再试"
            )
        if self.checkout_root is not None:
            try:
                self.key_file.resolve().relative_to(self.checkout_root.resolve())
            except ValueError:
                pass
            else:
                raise ProviderKeyRefused(
                    f"拒绝把 key 写进 checkout 里（{self.key_file}）："
                    f"打包工具不认 .gitignore"
                )
        return self.key_file


def _chmod(path: Path, mode: int) -> None:
    # Best effort by design. On Windows this moves only the read-only bit, and
    # raising here would turn "the key was stored" into "the key was stored and
    # then reported as a failure" on the platform where the mode never meant
    # anything.
    with contextlib.suppress(OSError):
        path.chmod(mode)


def _display(path: Path | None) -> str | None:
    """The path as a person would type it, with the home directory folded back."""
    if path is None:
        return None
    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except (ValueError, RuntimeError):
        return str(path)


__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "ProviderKeyRefused",
    "ProviderKeyStatus",
    "ProviderKeyStore",
    "fingerprint",
]

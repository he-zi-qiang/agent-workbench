"""Generate `chromium-seccomp.json`: Docker's default profile, three holes wider.

**Why this file exists at all.** ADR-0112 §3.5 first said Chromium's own sandbox
could not coexist with `cap_drop: ALL`, and chose `--no-sandbox` on that basis.
That was measured and it was wrong: `CLONE_NEWUSER` needs no capability -- being
available to the unprivileged is the entire point of an unprivileged user
namespace -- and what actually refuses it is Docker's *default seccomp profile*.
Under `--cap-drop=ALL --security-opt=no-new-privileges:true`, with only this
profile swapped in, Chromium starts with its layer-1 sandbox intact. The A/B is
one command apart and reproduced in the ADR.

**What is changed, and nothing else.** The upstream profile is taken verbatim
and exactly three edits are applied. Each one is the narrowest form that lets
the next syscall through, which is why this is generated rather than forked by
hand -- a fork drifts, and nobody can tell later which difference was the point.

1. `clone`'s namespace mask, `0x7E020000 -> 0x0E020000`. The rule reads
   `(flags & mask) == 0`, so clearing three bits from the mask stops refusing
   exactly those three: `CLONE_NEWUSER`, `CLONE_NEWPID`, `CLONE_NEWNET` -- the
   three the zygote asks for. `CLONE_NEWNS`, `CLONE_NEWCGROUP`, `CLONE_NEWUTS`
   and `CLONE_NEWIPC` stay refused, as upstream refuses them.
2. `unshare`, allowed under *the same* mask. Upstream allows it only with
   `CAP_SYS_ADMIN`, and `sandbox/linux/services/credentials.cc` reaches its user
   namespace through `unshare(CLONE_NEWUSER)`, not through `clone`. Carrying the
   mask over means this hole is the same shape as the first one rather than a
   second, wider one.
3. `chroot`, allowed. Upstream gates it on `CAP_SYS_CHROOT`. Inside the user
   namespace it just entered, the zygote *holds* that capability -- the kernel
   would permit the call. seccomp is a per-process filter and does not know
   about the namespace, so it refuses anyway. The failure is literal and was
   the last one to fall: `Check failed: sys_chroot("/proc/self/fdinfo/") == 0`.

`defaultAction` stays `SCMP_ACT_ERRNO`; no syscall is added to any allow list
beyond these. The profile is otherwise whatever the pinned moby tag says.

**The pin, and what it costs.** Docker offers no way to say "default, plus
this", only whole-profile replacement, so a generated file is a snapshot that
ages: a newer daemon's default knows syscalls this one does not, and a program
needing one of those would be refused here and not there. The tag is therefore
explicit and printed into the artifact, and re-running this script against a
newer tag is the whole upgrade procedure.

Usage:  python docker/make_chromium_seccomp.py [--tag v24.0.9] [-o PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

#: The seven namespace bits upstream refuses together, as one mask.
UPSTREAM_NS_MASK = 0x7E020000
#: The three the Chromium zygote asks for.
CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET = 0x10000000, 0x20000000, 0x40000000
GRANTED = CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET
#: What is still refused: NEWNS, NEWCGROUP, NEWUTS, NEWIPC.
PATCHED_NS_MASK = UPSTREAM_NS_MASK & ~GRANTED

DEFAULT_TAG = "v24.0.9"
SOURCE = (
    "https://raw.githubusercontent.com/moby/moby/{tag}/profiles/seccomp/default.json"
)


def build(profile: dict[str, Any], tag: str) -> dict[str, Any]:
    """Apply the three edits to a copy of the upstream profile."""

    if profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise SystemExit("refusing: upstream profile no longer denies by default")

    rewritten = 0
    for group in profile["syscalls"]:
        for argument in group.get("args") or []:
            if (
                argument.get("op") == "SCMP_CMP_MASKED_EQ"
                and argument.get("value") == UPSTREAM_NS_MASK
            ):
                argument["value"] = PATCHED_NS_MASK
                rewritten += 1
    if rewritten == 0:
        raise SystemExit("refusing: no clone namespace mask found to patch")

    profile["syscalls"].append(
        {
            "names": ["unshare"],
            "action": "SCMP_ACT_ALLOW",
            "args": [
                {"index": 0, "value": PATCHED_NS_MASK, "op": "SCMP_CMP_MASKED_EQ"}
            ],
            "comment": "ADR-0112 §3.5 edit 2 -- same mask as clone, not wider.",
        }
    )
    profile["syscalls"].append(
        {
            "names": ["chroot"],
            "action": "SCMP_ACT_ALLOW",
            "comment": (
                "ADR-0112 §3.5 edit 3 -- the zygote holds CAP_SYS_CHROOT inside "
                "the namespace it just entered; seccomp does not know that."
            ),
        }
    )
    profile["_generated_by"] = (
        f"docker/make_chromium_seccomp.py from moby {tag}; see ADR-0112 §3.5"
    )
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=DEFAULT_TAG, help="moby tag to read from")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).parent / "chromium-seccomp.json",
    )
    arguments = parser.parse_args(argv)

    url = SOURCE.format(tag=arguments.tag)
    with urllib.request.urlopen(url, timeout=60) as response:
        upstream = json.load(response)

    patched = build(upstream, arguments.tag)
    arguments.output.write_text(json.dumps(patched, indent=1) + "\n", encoding="utf-8")
    print(
        f"wrote {arguments.output} from moby {arguments.tag}: "
        f"mask 0x{UPSTREAM_NS_MASK:08X} -> 0x{PATCHED_NS_MASK:08X}, "
        f"+unshare(masked), +chroot"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""What a live-web search is called, and what it costs to call it (ADR-0085).

The names live in the domain for the reason ``domain/sandbox.py`` gives for
``sandbox_run``: the authorization envelope and the coding session's own tool
tuples both have to name this tool, and neither may import an adapter. Until
ADR-0085 only chat offered it, and chat assembles its registry in the same
module that builds the adapter -- so the name never needed to be sayable from
``core``. A coding session cannot do that: ``code_risk_ceiling`` reads the name
in ``application/`` and raises on one it has no spec for.

``risk="external"`` is the substantive declaration, and it says one thing
precisely. Following ``domain/sandbox.py``: external is not about side effects,
not about repeatability, and not about cost. It is about **content leaving this
process**. The sandbox earns it while being offline and leaving nothing behind;
a search earns it more plainly still, because the thing that leaves is the
user's own question, and it leaves for a third party.

The tool's own handler says the same in the one place it matters -- it records
the search in the journal *before* the call, because "a search that timed out
or failed still put the question to the open web, and a turn that did that has
already left this deployment's evidence boundary".

What external does **not** claim here is equally deliberate. That a search has
no side effect and may be repeated is said by ``idempotency="safe"`` and by the
binding carrying no ``operation_key``; the gateway reads those, not this. A
risk that also meant "irreversible" would be repeating what two other fields
already say while losing the only thing it says by itself.
"""

from __future__ import annotations

from typing import Final

#: The tool an agent calls. Named for the act rather than the provider: which
#: search back end answers is a deployment's ``[research]`` section, and a name
#: that carried the vendor would have to change when that section does.
WEB_SEARCH_TOOL: Final[str] = "web_search"

#: What a principal must hold before it may be dispatched. Already sent by the
#: console (``web/src/app/IdentityContext.tsx``) and already required by the
#: spec; naming it here is what lets a non-adapter layer say so too.
WEB_SEARCH_SCOPE: Final[str] = "external:search"

__all__ = [
    "WEB_SEARCH_SCOPE",
    "WEB_SEARCH_TOOL",
]

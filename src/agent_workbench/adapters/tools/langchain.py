"""A LangChain tool, arriving as an ordinary ``ToolBinding``.

``ports.tools`` and ``runtime.tool_gateway`` have both said, since they were
written, that "native handlers, MCP tools and LangChain tools all arrive as the
same binding, so there is exactly one place where a tool can be stopped". That
was a claim with nothing behind it. This module is what makes it checkable.

The point is not integration for its own sake -- it is that the sentence above
survives contact with somebody else's tool object. A LangChain tool brings its
own schema, its own argument coercion and its own error behaviour, and every one
of those has to end up under this project's gateway rather than beside it:

* the **schema** is the tool's own, but validation is this project's. The
  gateway validates before a handler runs, and a tool whose schema it cannot
  enforce stops the process at assembly rather than accepting anything later;
* **policy, risk and scopes** are declared here, by the deployment, not by the
  tool. A tool cannot describe its own risk into a system that is deciding
  whether to let it run;
* an **exception** becomes a ``ToolResult``. The loop is owed exactly one
  result per call id, and a third-party tool raising is the ordinary case, not
  an exceptional one.

What this deliberately does not do is give LangChain a second way in. There is
no executor here, no agent, no chain -- the tool is a callable with a schema,
and that is the entire surface this project wants from it.
"""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import (
    ToolConcurrency,
    ToolIdempotency,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

#: What a LangChain tool's schema is asked to become. The gateway's validator
#: supports a documented subset of JSON Schema and refuses the rest at
#: assembly, so a tool whose schema does not survive this is a tool this
#: process will not start holding.
SchemaObject = dict[str, JsonValue]


def binding_for(
    tool: Any,
    *,
    risk: ToolRisk,
    concurrency: ToolConcurrency,
    idempotency: ToolIdempotency,
    permission_scopes: tuple[str, ...] = (),
    timeout_seconds: int = 30,
) -> ToolBinding:
    """Wrap a LangChain ``BaseTool`` as a binding this project can run.

    Risk, concurrency, idempotency and scopes are arguments rather than read
    off the tool. A tool that described its own risk would be a tool deciding
    how carefully it is treated, and the whole point of the gateway is that
    this deployment decides that -- for a third-party tool most of all.
    """

    spec = ToolSpec(
        name=tool.name,
        description=_description(tool),
        input_schema=_schema(tool),
        concurrency=concurrency,
        risk=risk,
        idempotency=idempotency,
        timeout_seconds=timeout_seconds,
        permission_scopes=permission_scopes,
    )
    return ToolBinding(spec=spec, handler=_handler(tool))


def _description(tool: Any) -> str:
    text = (getattr(tool, "description", "") or "").strip()
    # A tool with no description still has to have one: the specification
    # travels into a model request, and an empty description is a tool the
    # model has no basis for choosing or avoiding.
    return text or f"The {tool.name} tool."


def _schema(tool: Any) -> SchemaObject:
    """The tool's own argument schema, as JSON Schema.

    LangChain tools carry a pydantic model; ``model_json_schema`` is the
    documented way to that. The result is used as-is rather than rewritten:
    a schema this project edited would be a schema the tool is not actually
    validating against.
    """

    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        schema: SchemaObject = dict(args_schema.model_json_schema())
        # ``$defs`` and ``title`` are pydantic's, not the contract's. The
        # gateway's validator rejects constructs it cannot enforce, and a
        # reference into a definitions block is one of them.
        schema.pop("title", None)
        schema.setdefault("additionalProperties", False)
        return schema
    # A tool with no declared arguments takes none. Saying so explicitly is
    # what stops the gateway from accepting anything for it.
    return {"type": "object", "additionalProperties": False, "properties": {}}


def _handler(tool: Any) -> Any:
    async def run(invocation: ToolInvocation) -> ToolResult:
        invocation.cancellation.raise_if_cancelled()
        try:
            # ``ainvoke`` rather than ``invoke``: this runtime is async, and a
            # synchronous call here would block the loop that is supposed to be
            # enforcing the timeout.
            output = await tool.ainvoke(dict(invocation.call.arguments))
        except Exception as error:
            # One result per call id, always. A third-party tool raising is
            # ordinary, and the loop is owed an answer either way -- so only
            # the exception's type crosses the boundary, never its text, which
            # is where a provider puts request bodies and keys.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="tool_failed",
                    message=f"{tool.name} raised {type(error).__name__}",
                ),
            )
        return ToolResult.succeeded(invocation.call, content=_content(output))

    return run


def _content(output: Any) -> str:
    """Whatever the tool returned, as text the model can read.

    LangChain tools return anything. This does not try to be clever about it:
    a string is passed through and everything else is rendered, because the
    alternative is a tool result whose shape depends on which tool produced it.
    """

    return output if isinstance(output, str) else repr(output)


__all__ = ["SchemaObject", "binding_for"]

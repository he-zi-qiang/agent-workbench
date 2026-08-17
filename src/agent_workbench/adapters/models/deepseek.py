"""DeepSeek, over its OpenAI-compatible chat completions API.

This is the first adapter that talks to something outside the process, and its
whole job is to make that fact invisible upstream. What leaves this module is
the same five ``ModelEvent`` kinds the scripted model produces, so the runtime
cannot tell the difference and a contract test can assert that it cannot.

Three rules shape the translation.

A tool call is emitted whole. The provider streams a call's arguments as JSON
fragments across many chunks; partial JSON must never reach schema validation
or policy, so fragments are buffered per index and turned into a ``ToolCall``
only once the stream says the call is finished.

Every stream ends with ``ModelStreamCompleted``, including the failing ones. A
caller that has to distinguish "the provider stopped" from "the adapter threw"
is a caller that will get it wrong somewhere.

Nothing from the wire is quoted back. An HTTP error carries a status code and
nothing else: the response body of a chat completion request can contain the
prompt that was sent, and error text flows into events, logs and the model's
own context.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx
from pydantic import ValidationError

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from agent_workbench.domain.runs import ModelProfileName, TokenUsage
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall, ToolSpec
from agent_workbench.ports.model import (
    ModelEvent,
    ModelFinishReason,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)

#: What a profile says about thinking. ``unsupported`` sends no ``thinking``
#: key at all -- the safe reading for a model id or compatible gateway this
#: adapter has not been told about, where an unknown key may be a 400.
#: ``disabled``/``enabled`` send it explicitly both ways, because the default
#: depends on which name a profile calls the model by: measured 2026-08-17,
#: ``deepseek-v4-flash`` reasons with no parameter at all while the
#: ``deepseek-chat`` alias -- resolving to that same model -- does not
#: (ADR-061 §1). A profile that moved to the concrete id and stayed silent
#: would be paying for reasoning nobody asked for.
ThinkingMode = Literal["unsupported", "disabled", "enabled"]

ReasoningEffort = Literal["low", "high", "max"]

CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"
DONE_SENTINEL: Final[str] = "[DONE]"
DATA_PREFIX: Final[str] = "data:"

# A tool call's arguments arrive in fragments and are held until the stream
# ends, so they are the one thing here that grows with what a provider sends.
DEFAULT_MAX_ARGUMENT_CHARS: Final[int] = 262_144

# Doubled per attempt. An immediate retry of a 429 is a way of asking to be
# rate limited harder.
RETRY_BACKOFF_SECONDS: Final[float] = 0.5


class _RetryableFailure:
    """A failure from before anything was emitted, so retrying it is safe."""

    __slots__ = ("error",)

    def __init__(self, error: ErrorInfo) -> None:
        self.error = error


class _OversizedFragment(Exception):
    """Accumulated tool arguments passed the ceiling this adapter will hold."""


class _UnreadableFrame(Exception):
    """A ``data:`` frame this adapter could not decode.

    Skipping one is not neutral. Tool arguments arrive as fragments that are
    concatenated, so dropping a fragment from the middle can leave the rest
    forming a different, perfectly valid JSON object -- a call the model never
    made, with arguments nobody chose, proposed as though it had.
    """


# The provider's vocabulary, mapped onto the port's. Anything unlisted is a
# protocol the adapter was not written against, and is reported as an error
# rather than guessed at.
FINISH_REASONS: Final[Mapping[str, ModelFinishReason]] = {
    "stop": "stop",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


@dataclass(frozen=True, slots=True)
class DeepSeekProfile:
    """The concrete model behind one profile name, and how to call it.

    The reliability fields mirror ``model.<profile>`` in the configuration
    contract. They lived there with no consumer, so a deployment could set a
    timeout or a retry count and get neither.
    """

    model_id: str
    temperature: float = 0.0
    max_output_tokens: int | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 0
    # Require a tool on the opening model turn only. Once a tool result is the
    # newest message, the provider must be free to synthesize a final answer or
    # voluntarily call another tool; requiring another call would make every
    # tool-using Agent loop consume its entire tool budget.
    tool_calling_required: bool = False
    # Probed 2026-08-16 against the live API (ADR-061): v4 models accept
    # `thinking` with tools in the same request and reason before calling
    # them; the `deepseek-chat` alias resolves server-side to v4 and does not
    # think unless asked. `unsupported` keeps this adapter byte-identical for
    # deployments pointing it at a gateway that predates the parameter.
    thinking: ThinkingMode = "unsupported"
    reasoning_effort: ReasoningEffort = "high"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")


@dataclass(slots=True)
class _PartialToolCall:
    """One tool call being assembled from stream fragments."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


class DeepSeekModel:
    """``ModelPort`` implementation for DeepSeek's chat completions API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        base_url: str,
        profiles: Mapping[ModelProfileName, DeepSeekProfile],
        max_argument_chars: int = DEFAULT_MAX_ARGUMENT_CHARS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_argument_chars < 1:
            raise ValueError("max_argument_chars must be positive")
        # The client is supplied rather than built here: connection lifetime
        # belongs to whoever assembles the process.
        self._client = client
        # Unwrapped once, at construction, and never logged. The configuration
        # contract keeps it a SecretStr everywhere else.
        self._api_key = api_key
        self._url = base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        self._profiles = dict(profiles)
        self._max_argument_chars = max_argument_chars
        # Injected so a retry test need not wait out its own backoff.
        self._sleep = sleep

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one completion, retrying only what is safe to retry.

        Retrying after any event has left this adapter would repeat text the
        caller already saw, so the only retryable failure is one that happens
        before the first event: a transport fault reaching the provider, or a
        retryable error status. Anything that goes wrong mid-stream is final.
        """

        profile = self._profiles.get(request.model_profile)
        if profile is None:
            yield ModelStreamCompleted(
                finish_reason="error",
                error=ErrorInfo(
                    code="provider_error",
                    message=f"no model configured for profile {request.model_profile}",
                ),
            )
            return

        for attempt in range(profile.max_retries + 1):
            retry: ErrorInfo | None = None
            async for event in self._attempt(request, profile):
                if isinstance(event, _RetryableFailure):
                    retry = event.error
                    break
                yield event
            if retry is None:
                return
            if attempt == profile.max_retries:
                yield ModelStreamCompleted(finish_reason="error", error=retry)
                return
            await self._sleep(RETRY_BACKOFF_SECONDS * (2**attempt))

    async def _attempt(
        self, request: ModelRequest, profile: DeepSeekProfile
    ) -> AsyncIterator[ModelEvent | _RetryableFailure]:
        """One call, signalling a retryable failure rather than yielding one.

        Only the two places that can fail before anything has been emitted
        signal; once bytes are flowing, every failure here is terminal.
        """

        payload = self._payload(request, profile)
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        partials: dict[int, _PartialToolCall] = {}
        usage = TokenUsage()
        finish: ModelFinishReason | None = None
        failure: ErrorInfo | None = None
        emitted = False

        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=payload,
                headers=headers,
                timeout=profile.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    # The body is not read and not quoted: a chat completion
                    # error can echo the prompt back.
                    rejected = ErrorInfo(
                        code="provider_error",
                        message=(
                            "the provider rejected the request with HTTP "
                            f"{response.status_code}"
                        ),
                        retryable=response.status_code >= 500
                        or response.status_code == 429,
                    )
                    if rejected.retryable:
                        yield _RetryableFailure(rejected)
                    else:
                        yield ModelStreamCompleted(
                            finish_reason="error", error=rejected
                        )
                    return

                async for line in response.aiter_lines():
                    try:
                        chunk = _parse_line(line)
                        if chunk is None:
                            continue

                        for event in _text_events(chunk):
                            emitted = True
                            yield event

                        _absorb_tool_fragments(
                            chunk, partials, self._max_argument_chars
                        )
                    except (_UnreadableFrame, _OversizedFragment) as exc:
                        # Fail closed. Whatever this stream was going to say,
                        # this adapter can no longer say it faithfully.
                        yield ModelStreamCompleted(
                            finish_reason="error",
                            error=ErrorInfo(
                                code="provider_error",
                                message=f"the provider stream was unusable: {exc}",
                            ),
                            usage=usage,
                        )
                        return
                    except ValidationError:
                        # A delta that violates a domain bound -- an over-long
                        # text chunk, most likely. The provider's own limits
                        # are not this process's contract, and a caller of
                        # ModelPort must not receive a Pydantic traceback.
                        yield ModelStreamCompleted(
                            finish_reason="error",
                            error=ErrorInfo(
                                code="provider_error",
                                message=(
                                    "the provider sent a chunk this process "
                                    "cannot represent"
                                ),
                            ),
                            usage=usage,
                        )
                        return

                    chunk_usage = _usage_of(chunk)
                    if chunk_usage is not None:
                        usage = chunk_usage

                    reported = _finish_reason_of(chunk)
                    if reported is not None:
                        finish, failure = _map_finish_reason(reported)
        except httpx.HTTPError as exc:
            # Transport faults stay transport faults: the type is descriptive
            # enough and the message may quote the URL and its query.
            failed = ErrorInfo(
                code="provider_error",
                message=f"the request to the provider failed: {type(exc).__name__}",
                retryable=True,
            )
            if emitted:
                # Mid-stream: retrying would repeat what the caller already
                # received, so this attempt is the whole story.
                yield ModelStreamCompleted(
                    finish_reason="error", error=failed, usage=usage
                )
            else:
                yield _RetryableFailure(failed)
            return

        if failure is not None:
            yield ModelStreamCompleted(
                finish_reason="error",
                error=failure,
                usage=usage,
            )
            return

        if finish is None:
            yield ModelStreamCompleted(
                finish_reason="error",
                error=ErrorInfo(
                    code="provider_error",
                    message="the provider ended the stream without a finish reason",
                ),
                usage=usage,
            )
            return

        calls, invalid = _completed_tool_calls(partials)
        if invalid is not None:
            yield ModelStreamCompleted(
                finish_reason="error",
                error=invalid,
                usage=usage,
            )
            return

        for call in calls:
            yield ModelToolCallProposed(call=call)

        yield ModelUsageReported(usage=usage)
        yield ModelStreamCompleted(finish_reason=finish, usage=usage)

    def _payload(
        self,
        request: ModelRequest,
        profile: DeepSeekProfile,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": profile.model_id,
            "messages": _chat_messages(request),
            "stream": True,
            # Without this the final chunk carries no usage, and a run that
            # cannot account for its tokens cannot enforce a token budget.
            "stream_options": {"include_usage": True},
            "temperature": profile.temperature,
        }
        max_tokens = request.max_output_tokens or profile.max_output_tokens
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if profile.thinking != "unsupported":
            # Explicit both ways, never omitted: the v4 models default
            # thinking *on*, so silence here would buy reasoning nobody
            # configured. The request's override wins over the profile's
            # default; against an `unsupported` profile it is ignored,
            # because there is no parameter to send.
            enabled = (
                request.thinking
                if request.thinking is not None
                else profile.thinking == "enabled"
            )
            payload["thinking"] = (
                {"type": "enabled", "reasoning_effort": profile.reasoning_effort}
                if enabled
                else {"type": "disabled"}
            )
        if request.tools:
            payload["tools"] = _tool_definitions(request.tools)
            if profile.tool_calling_required and (
                not request.messages or request.messages[-1].role != "tool"
            ):
                # `required` is an opening-turn policy. After a ToolResult the
                # same tools stay advertised under the provider's default auto
                # mode, so the model may either continue or finish.
                payload["tool_choice"] = "required"
        return payload


def _chat_messages(request: ModelRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    for message in request.messages:
        messages.extend(_convert_message(message))
    return messages


def _convert_message(message: Message) -> list[dict[str, Any]]:
    if message.role == "tool":
        # One provider message per result: the wire format keys a tool result
        # by its own call id rather than grouping them into a turn.
        return [
            {
                "role": "tool",
                "tool_call_id": block.tool_call_id,
                "content": block.text,
            }
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]

    text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
    converted: dict[str, Any] = {"role": message.role, "content": text}

    tool_calls = [
        {
            "id": block.tool_call_id,
            "type": "function",
            "function": {
                "name": block.tool_name,
                "arguments": json.dumps(block.arguments, ensure_ascii=False),
            },
        }
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]
    if tool_calls:
        converted["tool_calls"] = tool_calls
    return [converted]


def _tool_definitions(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in tools
    ]


def _as_object(value: object) -> dict[str, Any] | None:
    """Narrow a decoded JSON value to an object.

    ``json.loads`` returns ``Any``; everything downstream is written against
    what the provider actually sent, so the narrowing happens once, here.
    """

    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def _parse_line(line: str) -> dict[str, Any] | None:
    """Return one decoded SSE data object, or ``None`` for anything else."""

    stripped = line.strip()
    if not stripped or not stripped.startswith(DATA_PREFIX):
        return None
    data = stripped[len(DATA_PREFIX) :].strip()
    if not data or data == DONE_SENTINEL:
        return None
    try:
        decoded = json.loads(data)
    except json.JSONDecodeError as exc:
        raise _UnreadableFrame("a data frame was not valid JSON") from exc
    frame = _as_object(decoded)
    if frame is None:
        raise _UnreadableFrame("a data frame was not a JSON object")
    return frame


def _first_choice(chunk: dict[str, Any]) -> dict[str, Any] | None:
    choices = _as_list(chunk.get("choices"))
    if not choices:
        return None
    return _as_object(choices[0])


def _text_events(chunk: dict[str, Any]) -> list[ModelTextDelta | ModelThinkingDelta]:
    """Both texts a delta can carry, in the order the provider wrote them.

    ``reasoning_content`` first: the wire interleaves the two only at the
    boundary where thinking ends and the answer begins, and that one frame
    carries the tail of the reasoning beside the head of the answer. During
    either phase the other key is ``null``, which the isinstance checks skip
    -- the same way they always skipped absent content.
    """

    choice = _first_choice(chunk)
    if choice is None:
        return []
    delta = _as_object(choice.get("delta"))
    if delta is None:
        return []
    events: list[ModelTextDelta | ModelThinkingDelta] = []
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        events.append(ModelThinkingDelta(text=reasoning))
    content = delta.get("content")
    if isinstance(content, str) and content:
        events.append(ModelTextDelta(text=content))
    return events


def _absorb_tool_fragments(
    chunk: dict[str, Any],
    partials: dict[int, _PartialToolCall],
    max_argument_chars: int,
) -> None:
    choice = _first_choice(chunk)
    if choice is None:
        return
    delta = _as_object(choice.get("delta"))
    if delta is None:
        return
    fragments = _as_list(delta.get("tool_calls"))
    if fragments is None:
        return

    for raw_fragment in fragments:
        fragment = _as_object(raw_fragment)
        if fragment is None:
            continue
        index = fragment.get("index")
        if not isinstance(index, int):
            continue
        partial = partials.setdefault(index, _PartialToolCall())

        call_id = fragment.get("id")
        if isinstance(call_id, str) and call_id:
            partial.call_id = call_id

        function = _as_object(fragment.get("function"))
        if function is None:
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            partial.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            if len(partial.arguments) + len(arguments) > max_argument_chars:
                # The one thing here that grows with what the provider chooses
                # to send. Refusing is cheaper than discovering the ceiling
                # after the process has already held the whole of it.
                raise _OversizedFragment(
                    f"tool arguments passed the {max_argument_chars} character ceiling"
                )
            partial.arguments += arguments


def _usage_of(chunk: dict[str, Any]) -> TokenUsage | None:
    usage = _as_object(chunk.get("usage"))
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=_count(usage, "prompt_tokens"),
        output_tokens=_count(usage, "completion_tokens"),
        # DeepSeek reports its own cache accounting; absent on other
        # compatible servers, where it stays zero.
        cache_read_tokens=_count(usage, "prompt_cache_hit_tokens"),
    )


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _finish_reason_of(chunk: dict[str, Any]) -> str | None:
    choice = _first_choice(chunk)
    if choice is None:
        return None
    reason = choice.get("finish_reason")
    return reason if isinstance(reason, str) and reason else None


def _map_finish_reason(
    reported: str,
) -> tuple[ModelFinishReason | None, ErrorInfo | None]:
    mapped = FINISH_REASONS.get(reported)
    if mapped is not None:
        return mapped, None
    return None, ErrorInfo(
        code="provider_error",
        message=f"the provider stopped for an unsupported reason: {reported}",
    )


def _completed_tool_calls(
    partials: Mapping[int, _PartialToolCall],
) -> tuple[tuple[ToolCall, ...], ErrorInfo | None]:
    """Turn buffered fragments into whole calls, or explain why they are not."""

    calls: list[ToolCall] = []
    for index in sorted(partials):
        partial = partials[index]
        if not partial.call_id or not partial.name:
            return (), ErrorInfo(
                code="provider_error",
                message=f"the provider sent tool call {index} without an id or name",
            )
        arguments = _decode_arguments(partial.arguments)
        if arguments is None:
            # Guessing at the arguments would put something the model never
            # asked for in front of a handler.
            return (), ErrorInfo(
                code="provider_error",
                message=(f"the provider sent unparsable arguments for {partial.name}"),
            )
        calls.append(
            ToolCall(
                tool_call_id=partial.call_id,
                tool_name=partial.name,
                arguments=arguments,
            )
        )
    return tuple(calls), None


def _decode_arguments(raw: str) -> JsonObject | None:
    text = raw.strip()
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _as_object(decoded)


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "DeepSeekModel",
    "DeepSeekProfile",
    "ReasoningEffort",
    "ThinkingMode",
]

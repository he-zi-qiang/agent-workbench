"""Static architecture guards for dependency and configuration boundaries.

The checks deliberately derive their scope from the product source tree instead
of requiring future packages to exist.  The package root is already core source,
so the framework guard is active today; later ``domain``, ``runtime``,
``knowledge``, ``workflows``, and ``application`` modules are covered
automatically.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"

# These packages are the only product-side boundaries at which concrete
# frameworks, SDKs, process configuration, or interface concerns may enter.
OUTER_BOUNDARY_PACKAGES = frozenset(
    {"_config", "adapters", "apps", "bootstrap", "interfaces", "workers"}
)

# Keep this list explicit: a new concrete integration must make a conscious
# architecture decision instead of silently leaking into framework-neutral code.
FORBIDDEN_CORE_IMPORTS = frozenset(
    {
        # Agent/RAG/workflow frameworks
        "crewai",
        "langchain",
        "langchain_community",
        "langchain_core",
        "langgraph",
        "llama_index",
        # Document format SDKs belong to the project-owned Word process, never
        # to the framework-neutral runtime or workflow contracts.
        "docx",
        # HTTP interface frameworks
        "fastapi",
        "starlette",
        # Model and ML SDKs
        "anthropic",
        "cohere",
        "deepseek",
        "flagembedding",
        "google.genai",
        "google.generativeai",
        "huggingface_hub",
        "httpx",
        "mistralai",
        "mcp",
        "mcp_types",
        "openai",
        "sentence_transformers",
        "torch",
        "transformers",
        # Persistence, queue, search, and telemetry SDKs
        "asyncpg",
        "boto3",
        "botocore",
        "celery",
        "opentelemetry",
        "psycopg",
        "psycopg2",
        "qdrant_client",
        "redis",
        "sqlalchemy",
    }
)

OUTER_PROJECT_IMPORTS = frozenset(
    f"agent_workbench.{package}" for package in OUTER_BOUNDARY_PACKAGES
)

# The LlamaIndex roles ADR-017 declares off, enforced where they cannot be
# forgotten. `rag.llama_index` carries three single-valued `Literal[False]`
# fields saying this project does not use LlamaIndex's agent executor, does not
# let a QueryEngine produce the final answer, and does not fuse a second time.
# A process-side check on those fields could only compare a constant against
# itself. This is the check that can actually fail: the machinery is absent
# because it is never imported, anywhere in the source tree -- adapters
# included, since the adapter layer is exactly where somebody would reach for
# it.
#
# The tool loop has one owner and the answer has one author. A LlamaIndex agent
# would be a second runtime with its own step budget, its own tool protocol and
# no route through this project's Policy or audit pipeline; a QueryEngine or
# response synthesizer would generate the answer inside retrieval, downstream
# of the ACL check but upstream of the publish fence -- which is to say, text
# reaching a reader by a path the release gate never sees.
FORBIDDEN_LLAMA_INDEX_MODULES = frozenset(
    {
        "llama_index.core.agent",
        "llama_index.core.chat_engine",
        "llama_index.core.query_engine",
        "llama_index.core.response_synthesizers",
        "llama_index.core.query_pipeline",
    }
)

# The same two roles reached by method call rather than import. `as_query_engine`
# and `as_chat_engine` hang off the VectorStoreIndex this project *does* build,
# so no new import is needed to summon either -- which makes the import guard
# above insufficient on its own.
FORBIDDEN_LLAMA_INDEX_ATTRIBUTES = frozenset({"as_query_engine", "as_chat_engine"})

# Raw source loading belongs to bootstrap.  Process entry points may eventually
# call the public bootstrap.load_settings() facade, but must not depend on these
# implementation modules or source libraries.
RAW_CONFIG_IMPORTS = frozenset(
    {
        "agent_workbench.bootstrap.Settings",
        "agent_workbench.bootstrap.paths",
        "agent_workbench.bootstrap.settings",
        "dotenv",
        "pydantic_settings",
        "tomllib",
    }
)

ENVIRONMENT_MEMBERS = frozenset(
    {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
)


@dataclass(frozen=True, slots=True)
class ImportReference:
    """One import recovered from a product module."""

    module: str
    line: int


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One policy-sensitive source access recovered from a product module."""

    file: Path
    line: int
    expression: str


def _product_python_files() -> tuple[Path, ...]:
    """Return packaged code plus future top-level process entry points."""

    roots = (PACKAGE_ROOT, PROJECT_ROOT / "apps", PROJECT_ROOT / "workers")
    files = {
        file
        for root in roots
        if root.is_dir()
        for file in root.rglob("*.py")
        if "__pycache__" not in file.parts
    }
    return tuple(sorted(files))


def _package_section(file: Path) -> str | None:
    """Return the first package below agent_workbench, if one exists."""

    if not file.is_relative_to(PACKAGE_ROOT):
        return None
    relative = file.relative_to(PACKAGE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _is_bootstrap(file: Path) -> bool:
    return _package_section(file) == "bootstrap"


def _core_python_files() -> tuple[Path, ...]:
    """Select framework-neutral code without naming not-yet-created packages."""

    return tuple(
        file
        for file in _product_python_files()
        if file.is_relative_to(PACKAGE_ROOT)
        and _package_section(file) not in OUTER_BOUNDARY_PACKAGES
    )


def _module_name(file: Path) -> tuple[str, ...] | None:
    if not file.is_relative_to(PACKAGE_ROOT):
        return None
    relative = file.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = ("agent_workbench", *relative.parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def _resolve_from_import(
    file: Path,
    *,
    module: str | None,
    level: int,
) -> str | None:
    if level == 0:
        return module

    current_module = _module_name(file)
    if current_module is None:
        return module

    package = current_module if file.name == "__init__.py" else current_module[:-1]
    keep = len(package) - (level - 1)
    if keep < 0:
        return module

    resolved = (*package[:keep], *((module or "").split(".")))
    return ".".join(part for part in resolved if part)


def _import_references(file: Path) -> tuple[ImportReference, ...]:
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    references: list[ImportReference] = []
    importlib_modules = {"importlib"}
    import_module_functions: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                references.append(ImportReference(alias.name, node.lineno))
                if alias.name == "importlib":
                    importlib_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_from_import(
                file,
                module=node.module,
                level=node.level,
            )
            if resolved:
                references.append(ImportReference(resolved, node.lineno))
                references.extend(
                    ImportReference(f"{resolved}.{alias.name}", node.lineno)
                    for alias in node.names
                    if alias.name != "*"
                )
            if node.level == 0 and node.module == "importlib":
                import_module_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )

    # A literal dynamic import is still a static architecture dependency.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first_argument = node.args[0]
        if not isinstance(first_argument, ast.Constant) or not isinstance(
            first_argument.value, str
        ):
            continue

        is_import = (
            isinstance(node.func, ast.Name)
            and node.func.id in import_module_functions | {"__import__"}
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_modules
        )
        if is_import:
            references.append(ImportReference(first_argument.value, node.lineno))

    return tuple(references)


def _matches_module(module: str, prefixes: frozenset[str]) -> bool:
    normalized = module.casefold()
    return any(
        normalized == prefix.casefold()
        or normalized.startswith(f"{prefix.casefold()}.")
        for prefix in prefixes
    )


def _format_import_violations(
    violations: list[tuple[Path, ImportReference]],
) -> str:
    return "\n".join(
        f"{file.relative_to(PROJECT_ROOT)}:{reference.line}: imports {reference.module}"
        for file, reference in violations
    )


def _environment_references(file: Path) -> tuple[SourceReference, ...]:
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    os_modules = {"os"}
    direct_members: dict[str, str] = {}
    references: set[SourceReference] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
        elif (
            isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "os"
        ):
            direct_members.update(
                {
                    alias.asname or alias.name: alias.name
                    for alias in node.names
                    if alias.name in ENVIRONMENT_MEMBERS
                }
            )
            references.update(
                SourceReference(
                    file,
                    node.lineno,
                    f"from os import {alias.name}",
                )
                for alias in node.names
                if alias.name == "*" or alias.name in ENVIRONMENT_MEMBERS
            )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ENVIRONMENT_MEMBERS
            and isinstance(node.value, ast.Name)
            and node.value.id in os_modules
        ):
            references.add(
                SourceReference(file, node.lineno, f"{node.value.id}.{node.attr}")
            )
        elif isinstance(node, ast.Name) and node.id in direct_members:
            references.add(SourceReference(file, node.lineno, direct_members[node.id]))

    return tuple(sorted(references, key=lambda item: (item.line, item.expression)))


def _format_source_violations(violations: list[SourceReference]) -> str:
    return "\n".join(
        f"{item.file.relative_to(PROJECT_ROOT)}:{item.line}: accesses {item.expression}"
        for item in violations
    )


def test_core_keeps_frameworks_and_concrete_sdks_at_outer_boundaries() -> None:
    core_files = _core_python_files()
    assert core_files, "core source discovery must scan at least the package root"

    violations = [
        (file, reference)
        for file in core_files
        for reference in _import_references(file)
        if _matches_module(reference.module, FORBIDDEN_CORE_IMPORTS)
    ]

    assert not violations, (
        "framework-neutral core imports a concrete framework or SDK; move the "
        "integration behind an adapter:\n"
        f"{_format_import_violations(violations)}"
    )


def test_no_module_reaches_for_llamaindex_s_agent_or_query_engine() -> None:
    """ADR-017's "adapter, not executor" line, made structural.

    Scans the whole product tree rather than the core, deliberately. Core is
    already forbidden from importing ``llama_index`` at all; the layer that can
    do this is the adapter layer, which is allowed the framework and is
    therefore the only place the mistake is available.
    """

    product_files = _product_python_files()
    assert product_files, "product source discovery must not be vacuous"

    violations = [
        (file, reference)
        for file in product_files
        for reference in _import_references(file)
        if _matches_module(reference.module, FORBIDDEN_LLAMA_INDEX_MODULES)
    ]

    assert not violations, (
        "a module imports LlamaIndex's agent or answer-generating machinery; "
        "ADR-017 gives the tool loop and the final answer to this project:\n"
        f"{_format_import_violations(violations)}"
    )


def test_no_module_turns_the_index_into_a_query_or_chat_engine() -> None:
    """The same two roles, reached by method call instead of by import.

    Without this the guard above would be satisfied by a module that imports
    nothing new and simply calls ``.as_query_engine()`` on the index this
    project already builds.
    """

    violations: list[SourceReference] = []
    for file in _product_python_files():
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_LLAMA_INDEX_ATTRIBUTES
            ):
                violations.append(
                    SourceReference(file=file, line=node.lineno, expression=node.attr)
                )

    assert not violations, (
        "a module asks LlamaIndex to answer rather than to retrieve:\n"
        f"{_format_source_violations(violations)}"
    )


def test_the_retriever_this_project_does_build_is_still_reachable() -> None:
    """The control for both guards above.

    Two rules that only ever say no are satisfied by a tree with no LlamaIndex
    in it at all -- which is also what they would look like if the adapter were
    deleted tomorrow. This asserts the permitted call is present, so the pair
    describes a boundary rather than an absence.
    """

    retriever = PACKAGE_ROOT / "adapters" / "llama_index" / "retriever.py"
    tree = ast.parse(retriever.read_text(encoding="utf-8"), filename=str(retriever))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "as_retriever" in attributes
    assert not attributes & FORBIDDEN_LLAMA_INDEX_ATTRIBUTES


def test_core_does_not_reverse_depend_on_outer_project_layers() -> None:
    core_files = _core_python_files()
    assert core_files, "core source discovery must not be vacuous"

    violations = [
        (file, reference)
        for file in core_files
        for reference in _import_references(file)
        if _matches_module(reference.module, OUTER_PROJECT_IMPORTS)
    ]

    assert not violations, (
        "core code reverse-depends on bootstrap/adapter/interface code:\n"
        f"{_format_import_violations(violations)}"
    )


def test_raw_configuration_sources_are_confined_to_bootstrap() -> None:
    product_files = _product_python_files()
    assert product_files, "product source discovery must not be vacuous"

    observed = [
        (file, reference)
        for file in product_files
        for reference in _import_references(file)
        if _matches_module(reference.module, RAW_CONFIG_IMPORTS)
    ]
    assert observed, (
        "the guard must observe the existing bootstrap configuration imports; "
        "otherwise source discovery or AST extraction has regressed"
    )

    violations = [
        (file, reference) for file, reference in observed if not _is_bootstrap(file)
    ]
    assert not violations, (
        "raw configuration loading is allowed only in agent_workbench.bootstrap; "
        "inject a narrow configuration object instead:\n"
        f"{_format_import_violations(violations)}"
    )


def test_business_modules_do_not_read_process_environment_directly() -> None:
    product_files = _product_python_files()
    assert product_files, "product source discovery must not be vacuous"

    observed = [
        reference
        for file in product_files
        for reference in _environment_references(file)
    ]
    assert any(_is_bootstrap(reference.file) for reference in observed), (
        "the guard must observe bootstrap's existing os.environ access; otherwise "
        "source discovery or AST extraction has regressed"
    )

    violations = [
        reference for reference in observed if not _is_bootstrap(reference.file)
    ]
    assert not violations, (
        "business modules must receive validated configuration from bootstrap, "
        "not read or mutate process environment directly:\n"
        f"{_format_source_violations(violations)}"
    )


def test_a_tool_binding_does_not_reach_into_the_workflow_layer() -> None:
    """A tool belongs to whoever calls it, not to the graph that used to.

    `agent_workbench.workflows` is where graphs live: node ids, edges, agent
    profiles, the Task state machine. A tool binding that imported it would be
    a tool only a Task could hold -- which was literally true while
    `WorkspaceScope` sat there and three bindings imported it from there. The
    working set is not a graph concept: a Code session has one and has no graph
    at all, so the scope moved to `application` and this is what stops it from
    drifting back.

    `adapters/langgraph` is deliberately exempt and is not an exception to the
    rule so much as the reason the rule can be narrow: compiling the graph
    declarations *is* that adapter's job, and it is the only adapter whose
    subject is the workflow layer.
    """

    tool_files = tuple(
        file
        for file in _product_python_files()
        if file.is_relative_to(PACKAGE_ROOT / "adapters")
        and not file.is_relative_to(PACKAGE_ROOT / "adapters" / "langgraph")
    )
    assert tool_files, "adapter source discovery must not be vacuous"

    # The control: the exempt adapter really does import the layer, so a change
    # that broke discovery or AST extraction cannot leave this test passing on
    # an empty scan.
    exempt = tuple(
        reference
        for file in _product_python_files()
        if file.is_relative_to(PACKAGE_ROOT / "adapters" / "langgraph")
        for reference in _import_references(file)
        if _matches_module(reference.module, frozenset({"agent_workbench.workflows"}))
    )
    assert exempt, (
        "the guard must observe the LangGraph adapter's own workflow imports; "
        "otherwise the scan below proves nothing"
    )

    violations = [
        (file, reference)
        for file in tool_files
        for reference in _import_references(file)
        if _matches_module(reference.module, frozenset({"agent_workbench.workflows"}))
    ]
    assert not violations, (
        "an adapter outside agent_workbench.adapters.langgraph imports the "
        "workflow layer; move what it needs into application/ instead:\n"
        f"{_format_import_violations(violations)}"
    )


# The project's single architectural claim, written as something that can fail.
#
# `ports/agent_executor.py` states it in prose -- "Exactly one component owns a
# model-tool loop, and in this project it is the custom runtime" -- and several
# single-valued `Literal`s in settings.py say the same thing about which
# executor a deployment configures. None of them can catch the way it actually
# gets broken: not by registering a second executor behind the port, but by some
# module quietly growing its own `model -> tool -> result -> model` loop.
#
# ADR-082 makes that a live risk for the first time. A delegation handler that
# holds an `AgentExecutor` and calls it is the whole design; a delegation
# handler that consumed the model stream itself would be the second loop, and in
# review the two look almost identical.

#: Modules permitted to name the streaming model interface at all.
#:
#: An allowlist rather than a rule, for the reason `FORBIDDEN_CORE_IMPORTS` is
#: one: adding a module here has to be a decision somebody made rather than a
#: thing that happened.
MODEL_STREAM_OWNERS = frozenset(
    {
        # Defines the contract.
        "ports/model.py",
        "ports/__init__.py",
        # Implement it. An adapter *produces* ModelEvents; it does not consume
        # a loop of them.
        "adapters/models/deepseek.py",
        "adapters/models/fake.py",
        # Builds one and returns it without ever calling it.
        "bootstrap/model_factory.py",
        # Carries one as a field of an assembled test stack.
        "adapters/testing/stack.py",
        # Owns the loop. The whole point.
        "runtime/agent_runtime.py",
    }
)

#: The module every one of those names lives in. Whole-module rather than
#: per-name because `ports/model.py` contains nothing that is not part of
#: streaming a model call: importing anything from it is naming the interface.
MODEL_STREAM_MODULE = "agent_workbench.ports.model"


def _relative_module_path(file: Path) -> str:
    return file.relative_to(PACKAGE_ROOT).as_posix()


def _is_stream_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "stream"
    )


def _async_for_over_a_stream_call(file: Path) -> bool:
    """Whether this module iterates the result of a ``.stream(...)`` call.

    The first line of any tool loop, and the one part of it that cannot be
    written some other way: something has to consume the async iterator the
    model port hands back.

    Bound names are followed, because the runtime does not iterate the call
    expression directly -- it keeps the iterator in a variable so that a failure
    part-way through can close it. A check that only looked at the loop header
    would find nothing in the one file it must find something in, and would then
    report an empty scan as a clean one.
    """

    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    bound: set[str] = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_stream_call(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFor):
            continue
        iterator = node.iter
        if _is_stream_call(iterator):
            return True
        if isinstance(iterator, ast.Name) and iterator.id in bound:
            return True
    return False


def test_the_model_tool_loop_has_exactly_one_owner() -> None:
    """ADR-082's load-bearing distinction, made structural.

    "One tool loop" bounds how many *implementations* of the loop exist, not how
    many levels deep it may be entered. A delegated run re-enters the same loop,
    which is why ADR-082 does not have to supersede that invariant; a handler
    that iterated a model stream itself would be a second implementation, which
    is why it would.

    Two halves, and they cover different tree shapes on purpose.

    The first is by **shape**, over framework-neutral code only. `async for`
    over a `.stream(...)` is what a loop looks like, but the phrase means
    something else at the outer boundary -- an HTTP upload body is streamed in
    `apps/api/routes/uploads.py`, and the model adapter itself iterates a
    provider's SSE response to *produce* the events. Neither is a tool loop, and
    core is exactly the region where the phrase has only one meaning.

    The second is by **vocabulary**, over the whole tree, which is what reaches
    the adapters the first half steps around. It also catches a loop written
    with a `while` and a manual `__anext__`, which the first half would miss
    entirely.
    """

    iterating = {
        _relative_module_path(file)
        for file in _core_python_files()
        if _async_for_over_a_stream_call(file)
    }

    assert iterating == {"runtime/agent_runtime.py"}, (
        "a module other than the custom runtime consumes a model stream, which "
        "is a second model-tool loop however it is spelled; hold the "
        "AgentExecutor port and call it instead:\n" + "\n".join(sorted(iterating))
    )

    naming = {
        _relative_module_path(file)
        for file in _product_python_files()
        if file.is_relative_to(PACKAGE_ROOT)
        for reference in _import_references(file)
        if reference.module == MODEL_STREAM_MODULE
    }

    # The control: without it a change that broke import extraction would leave
    # this half asserting nothing at all.
    assert "runtime/agent_runtime.py" in naming, (
        "the guard must observe the runtime's own model-port imports; "
        "otherwise the check below proves nothing"
    )

    unexpected = naming - MODEL_STREAM_OWNERS
    assert not unexpected, (
        "a module outside the model-stream allowlist imports the streaming "
        "interface; if it needs to run an agent it should hold an "
        "AgentExecutor, and if it genuinely owns a new provider it belongs in "
        "MODEL_STREAM_OWNERS with an ADR behind it:\n" + "\n".join(sorted(unexpected))
    )

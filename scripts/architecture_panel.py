#!/usr/bin/env python
"""A local, offline panel that shows what this repository actually is.

The README can only ever be a flat reading of a system whose interesting part
is its shape: which layer may import which, where the single tool loop lives,
what a tool call passes through before a handler runs. This serves that shape
as a page you can open, click through and search -- on loopback, with no
network, no database and no build step.

Everything countable on the page is **read from the tree at build time**, not
typed into it. Module summaries are the first line of each module's own
docstring; line counts, file counts, endpoint paths, tool names, ADR titles,
config profiles and the architecture guard lists are all parsed out of the
sources that define them. That choice is the whole point of the script: this
repository has already been bitten by a number written beside an unrelated
fact ("458/458" survived in CLAUDE.md months past 800 tests), and a panel is a
much larger surface for that failure than a paragraph is. Nothing here can go
stale without the file it was read from changing.

What is *not* generated is the prose that explains architecture rather than
files: the five gates on the loop, the ordered stages of the gateway, the two
flows. That text lives in ``NARRATIVE`` below and names real symbols, and
``--check`` fails when a named path stops existing -- so the curated half
cannot rot silently either. Run it in CI if you want that guarantee enforced
rather than merely available:

    uv run python scripts/architecture_panel.py --check

Usage:

    scripts/dev.sh panel                      # build and serve on 127.0.0.1:8770
    uv run python scripts/architecture_panel.py --serve --port 8770
    uv run python scripts/architecture_panel.py --build var/panel
    uv run python scripts/architecture_panel.py --json                # data only

Loopback is not a default here, it is the only binding. The panel reads the
working tree and prints module docstrings; that is source disclosure, and this
repository's own deployment note says the API is loopback-bound local
development rather than something to expose (ADR-044). A panel that inherited
`0.0.0.0` from a copied snippet would be the same mistake with a smaller blast
radius, which is exactly the kind that ships.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field
from functools import cache
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "agent_workbench"
ASSET_DIR = PROJECT_ROOT / "docs" / "assets"
DEFAULT_OUT = PROJECT_ROOT / "var" / "panel"
DEFAULT_PORT = 8770


# --------------------------------------------------------------------------
# 1. Reading the tree
# --------------------------------------------------------------------------


def _speak_utf8() -> None:
    """Make this program's own output survive a stream that is not UTF-8.

    Almost every line it prints is Chinese, and where that breaks is narrower
    than "Windows" and worth stating precisely: a *console* stdout on Windows
    has gone through the console API in UTF-8 since Python 3.6 and is fine. A
    **redirected** one is not -- `panel --json > data.json`, or piping to
    `more` -- because there Python falls back to the ANSI code page with strict
    error handling, and on an English-locale install that is cp1252, which
    cannot encode one character of this. The failure it produces is the worst
    shape available: everything is built, the file is written, and then the
    *report* raises UnicodeEncodeError, so work that succeeded looks like it
    failed.

    Reconfiguring to UTF-8 covers the redirected case exactly. On a legacy
    conhost that has been switched to a raw code page it degrades to mojibake
    rather than to a traceback, which is the right way round -- and the one
    line that must survive either way, the URL, is printed as ASCII on its own.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a stream someone replaced with a plain object
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def _rel(path: Path) -> str:
    """A repo-relative path as a string, with forward slashes on every platform.

    ``str(p.relative_to(root))`` is the obvious spelling and it is wrong on
    Windows: it yields ``docs\\assets\\arch-layers.svg``. That string is not
    only displayed -- it is the key the panel matches on (which adapter a tool
    was declared in), it is written into the JSON the page is built from, and it
    is what a reader copies into an editor. ``as_posix()`` makes the whole
    program speak one path dialect regardless of the host.
    """
    return path.relative_to(PROJECT_ROOT).as_posix()


def _summary(doc: str | None) -> str:
    """The first sentence of a docstring, which is where this repo puts the claim.

    Every module under ``src/`` opens with a one-line statement of what it owns
    followed by a blank line and the reasoning. Taking line one gives a summary
    written by whoever last changed the module, rather than one written beside
    it by someone reading it -- the second kind is the kind that drifts.
    """
    if not doc:
        return ""
    first = doc.strip().split("\n\n", 1)[0]
    return " ".join(first.split())


@cache
def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _loc(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _module_symbols(tree: ast.Module) -> list[dict[str, Any]]:
    """Top-level classes and functions, with their own first docstring line.

    Nested definitions are deliberately skipped. The panel is a map, and a map
    that showed every helper inside every class would be the source tree again
    at one third the resolution.
    """
    out: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "func"
        else:
            continue
        if node.name.startswith("_"):
            continue
        out.append(
            {
                "name": node.name,
                "kind": kind,
                "doc": _summary(ast.get_docstring(node)),
                "line": node.lineno,
            }
        )
    return out


def _const_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "value"`` assignments, annotated or not.

    Used for router prefixes and tool names, both of which are declared this way
    so that a rename is one edit rather than one per call site.
    """
    found: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            found[target.id] = value.value
    return found


def _string_set(tree: ast.Module, name: str) -> list[str]:
    """The literal strings inside a module-level ``frozenset({...})`` or list.

    The architecture guards are written as literals on purpose -- a computed
    allowlist would be an allowlist nobody can read -- which is what makes them
    parseable here without importing the test module.
    """
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            return [
                e.value
                for e in value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
        if isinstance(value, ast.Dict):
            return [
                k.value
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
    return []


# The layer each top-level package belongs to, and the one sentence that says
# why the layer exists at all. Order is the reading order of the panel, which
# is inside-out: the rules before the things that obey them.
LAYERS: list[dict[str, Any]] = [
    {
        "id": "domain",
        "title": "domain",
        "kind": "core",
        "packages": ["domain"],
        "tagline": "不变量写进类型，构造失败即拒绝",
        "blurb": (
            "「什么状态根本不该存在」由构造函数回答，而不是由每个调用方记得检查。"
            'DomainModel 全局 frozen=True / extra="forbid"，所以一个越权的信封、'
            "一份自相矛盾的预算、一个能放宽自己的请求，都在 __init__ 里就失败。"
        ),
    },
    {
        "id": "ports",
        "title": "ports",
        "kind": "core",
        "packages": ["ports"],
        "tagline": "Protocol 契约，唯一的跨层接缝",
        "blurb": (
            "把「系统需要什么能力」和「谁来提供」分开。这里没有 SQL、没有 HTTP、"
            "没有向量库调用——只有 typing.Protocol。换掉一个厂商是新增一个 adapter，"
            "不是修改核心层。"
        ),
    },
    {
        "id": "runtime",
        "title": "runtime",
        "kind": "core",
        "packages": ["runtime"],
        "tagline": "全仓唯一的 model → tool → result → model 循环",
        "blurb": (
            "Agent Runtime 本体。把一次运行跑到终态，并在循环上装齐预算、截止、"
            "上下文压缩、取消、重复调用五道闸；每一次工具调用都必须穿过同一个 "
            "Tool Gateway。第二份消费模型流的循环是这条架构线存在的理由。"
        ),
    },
    {
        "id": "workflows",
        "title": "workflows",
        "kind": "core",
        "packages": ["workflows"],
        "tagline": "控制流是声明：边是数据，路由是纯函数",
        "blurb": (
            "图长什么样、哪个 agent 能看到什么、哪一步需要人点头，写成能单独读、"
            "单独测的数据结构。编译成 LangGraph 这件事发生在 adapters/langgraph/，"
            "这里一行 langgraph 都不 import。"
        ),
    },
    {
        "id": "application",
        "title": "application",
        "kind": "core",
        "packages": ["application", "evaluation"],
        "tagline": "用例编排：发布围栏、幂等、崩溃恢复",
        "blurb": (
            "「一次问答」「一个 Task」「一次编码会话」的步骤、授权围栏与失败处理。"
            "它不允许自己长出工具循环——要跑 agent 只能过 ports/agent_executor。"
        ),
    },
    {
        "id": "adapters",
        "title": "adapters",
        "kind": "outer",
        "packages": ["adapters"],
        "tagline": "一个目录接一个外部世界",
        "blurb": (
            "PostgreSQL、Qdrant、LangGraph、LlamaIndex、MCP、模型供应商、"
            "OpenTelemetry……各家方言在自己的目录边界上翻译成 ports 的协议。"
            "翻译错了是一个 adapter 的事，不是全仓的事。"
        ),
    },
    {
        "id": "apps",
        "title": "apps · bootstrap · workers",
        "kind": "outer",
        "packages": ["apps", "bootstrap", "workers"],
        "tagline": "让一份 TOML 变成若干个启动即验伪的进程",
        "blurb": (
            "进程边界与依赖装配。os.environ 只允许出现在 bootstrap 包内；"
            "Settings 类型不得越过 projections.py 继续传播；配置声称的能力与代码"
            "不符时，进程在加载阶段就起不来。"
        ),
    },
]

PACKAGE_LAYER = {pkg: layer["id"] for layer in LAYERS for pkg in layer["packages"]}


@dataclass
class Module:
    name: str
    path: str
    loc: int
    doc: str
    symbols: list[dict[str, Any]] = field(default_factory=list)


def scan_packages() -> list[dict[str, Any]]:
    """Every Python package under ``src/agent_workbench`` with its real numbers."""
    packages: list[dict[str, Any]] = []
    for pkg_dir in sorted(
        p for p in PACKAGE_ROOT.iterdir() if p.is_dir() and not p.name.startswith("__")
    ):
        top = pkg_dir.name
        groups: dict[str, list[Module]] = {}
        for py in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            tree = _parse(py)
            group = (
                py.parent.relative_to(pkg_dir).as_posix()
                if py.parent != pkg_dir
                else ""
            )
            module = Module(
                name=py.stem,
                path=_rel(py),
                loc=_loc(py),
                doc=_summary(ast.get_docstring(tree)) if tree else "",
                symbols=_module_symbols(tree) if tree else [],
            )
            groups.setdefault(group, []).append(module)

        subs = [
            {
                "name": name or ".",
                "loc": sum(m.loc for m in mods),
                "files": len(mods),
                "modules": [vars(m) for m in mods],
            }
            for name, mods in sorted(groups.items())
        ]
        packages.append(
            {
                "name": top,
                "layer": PACKAGE_LAYER.get(top, "outer"),
                "path": _rel(pkg_dir),
                "loc": sum(s["loc"] for s in subs),
                "files": sum(s["files"] for s in subs),
                "groups": subs,
            }
        )
    return packages


def scan_endpoints() -> list[dict[str, Any]]:
    """The HTTP surface, read off the decorators rather than off the docs.

    Prefixes are module-level constants in the same file as the router, so one
    pass of ``_const_strings`` resolves both halves of every path. A route whose
    prefix came from somewhere else would show up here with the constant's name
    still in it, which is a visible failure rather than a silently wrong path.
    """
    routes_dir = PACKAGE_ROOT / "apps" / "api" / "routes"
    out: list[dict[str, Any]] = []
    for py in sorted(routes_dir.glob("*.py")):
        tree = _parse(py)
        if tree is None:
            continue
        consts = _const_strings(tree)
        prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if not (isinstance(func, ast.Name) and func.id == "APIRouter"):
                continue
            name = (
                node.targets[0].id
                if isinstance(node.targets[0], ast.Name)
                else "router"
            )
            prefix = ""
            for kw in node.value.keywords:
                if kw.arg == "prefix":
                    if isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)
                    elif isinstance(kw.value, ast.Name):
                        prefix = consts.get(kw.value.id, kw.value.id)
            prefixes[name] = prefix

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not (
                    isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                ):
                    continue
                verb = deco.func.attr
                if verb not in {"get", "post", "put", "patch", "delete", "head"}:
                    continue
                owner = deco.func.value
                owner_name = owner.id if isinstance(owner, ast.Name) else "router"
                if owner_name not in prefixes:
                    continue
                tail = (
                    deco.args[0].value
                    if deco.args and isinstance(deco.args[0], ast.Constant)
                    else ""
                )
                out.append(
                    {
                        "method": verb.upper(),
                        "path": (prefixes[owner_name] + str(tail)) or "/",
                        "handler": node.name,
                        "doc": _summary(ast.get_docstring(node)),
                        "module": _rel(py),
                        "group": py.stem,
                    }
                )
    # health lives outside routes/ only in the sense of having no prefix; it is
    # already covered above. Sort by path so the panel reads as a surface.
    return sorted(out, key=lambda r: (r["group"], r["path"], r["method"]))


def scan_tools() -> list[dict[str, Any]]:
    """Tool names, taken from the constants that the specs are built from.

    A tool name is a wire contract: it is frozen into every authorization
    envelope at submission and written into every event. So it is declared once
    as a module constant, and that constant is what this reads -- listing the
    handler files would have found the same files and none of the names.

    The constant *name* is the filter, not the value. ``TOOL_CALL_ID_PREFIX``
    and ``DEMO_TOOL_CALL_ID`` are also module-level strings with "TOOL" in the
    name, and a value-shaped filter let ``tc``, ``texec`` and ``toolu_demo_1``
    onto an earlier version of this list -- three identifier prefixes presented
    as tools the agent could call.
    """
    kinds = (
        ("adapters/tools/fakes.py", "demo"),
        ("apps/cli/", "demo"),
        ("apps/word_mcp/", "mcp"),
        ("apps/web_mcp/", "mcp"),
        ("apps/sandbox_mcp/", "mcp"),
        ("apps/computer_mcp/", "mcp"),
    )
    out: list[dict[str, Any]] = []
    for py in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = _parse(py)
        if tree is None:
            continue
        rel = _rel(py)
        kind = next((k for frag, k in kinds if frag in rel), "in-process")
        for const, value in _const_strings(tree).items():
            if not (const.endswith("_TOOL") or const.endswith("TOOL_NAME")):
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
                continue
            out.append(
                {
                    "name": value,
                    "kind": kind,
                    "constant": const,
                    "module": rel,
                    "doc": _summary(ast.get_docstring(tree)),
                }
            )
    # The MCP servers declare their catalogue as ``Tool(name="...")`` literals
    # rather than as module constants, because the name is only ever used once
    # -- inside the object it names. Reading the keyword is the only way to see
    # them, and leaving them out would have shown the computer server as a
    # process that exposes nothing.
    for server in sorted((PACKAGE_ROOT / "apps").glob("*_mcp/server.py")):
        tree = _parse(server)
        if tree is None:
            continue
        alias = server.parent.name.removesuffix("_mcp")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if fname not in {"Tool", "ToolSpec"}:
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    out.append(
                        {
                            "name": str(kw.value.value),
                            "kind": "mcp",
                            "constant": f"{alias} server",
                            "module": _rel(server),
                            "doc": _summary(ast.get_docstring(tree)),
                        }
                    )

    seen: dict[str, dict[str, Any]] = {}
    for tool in out:
        seen.setdefault(tool["name"], tool)
    return sorted(seen.values(), key=lambda t: (t["kind"], t["name"]))


def scan_adrs() -> list[dict[str, Any]]:
    """Every ADR, numbered, with the title its own first heading gives it."""
    adr_dir = PROJECT_ROOT / "docs" / "adr"
    out: list[dict[str, Any]] = []
    for md in sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        heading = next(
            (
                ln.lstrip("# ").strip()
                for ln in text.splitlines()
                if ln.startswith("# ")
            ),
            md.stem,
        )
        number = md.stem.split("-", 1)[0]
        status = ""
        m = re.search(
            r"^\s*[-*]?\s*(?:状态|Status)\s*[:：]\s*(.+)$", text, re.MULTILINE
        )
        if m:
            status = m.group(1).strip()
        out.append(
            {
                "id": number,
                "title": heading,
                "path": _rel(md),
                "status": status,
                "superseded": bool(re.search(r"被.*取代|Superseded", text)),
            }
        )
    return out


def scan_profiles() -> list[dict[str, Any]]:
    """The config profiles, with the schema version each one declares."""
    out: list[dict[str, Any]] = []
    for toml in sorted((PROJECT_ROOT / "config").glob("config.*.toml")):
        text = toml.read_text(encoding="utf-8", errors="replace")
        schema = ""
        m = re.search(r'^\s*config_schema_version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            schema = m.group(1)
        header = []
        for line in text.splitlines():
            if line.startswith("#"):
                header.append(line.lstrip("# ").rstrip())
            elif header:
                break
        enabled = re.findall(r'^\s*alias\s*=\s*"([a-z_]+)"', text, re.MULTILINE)
        out.append(
            {
                "name": toml.stem.replace("config.", ""),
                "path": _rel(toml),
                "schema": schema,
                "headline": " ".join(header[:3]) if header else "",
                "mcp_servers": enabled,
                "loc": len(text.splitlines()),
            }
        )
    return out


def count_protocols() -> dict[str, int]:
    """How many ports modules define a Protocol, and how many Protocols in all.

    Counting the *files* under ``ports/`` would count ``__init__.py`` as a
    contract, which it is not. Counting classes whose base is ``Protocol`` is
    the question anyone actually means by "how many seams are there".
    """
    d = PACKAGE_ROOT / "ports"
    modules = protocols = 0
    for f in sorted(d.glob("*.py")):
        tree = _parse(f)
        if tree is None:
            continue
        found = [
            c
            for c in tree.body
            if isinstance(c, ast.ClassDef)
            and any(
                getattr(b, "id", getattr(b, "attr", "")) == "Protocol" for b in c.bases
            )
        ]
        if found:
            modules += 1
            protocols += len(found)
    return {"modules": modules, "protocols": protocols}


def scan_guards() -> dict[str, Any]:
    """The architecture guard lists, read out of the test that enforces them."""
    guard = PROJECT_ROOT / "tests" / "architecture" / "test_dependency_boundaries.py"
    tree = _parse(guard)
    if tree is None:
        return {}
    return {
        "path": _rel(guard),
        "outer_packages": _string_set(tree, "OUTER_BOUNDARY_PACKAGES"),
        "core_allowlist": _string_set(tree, "CORE_THIRD_PARTY_ALLOWLIST"),
        "forbidden_core": _string_set(tree, "FORBIDDEN_CORE_IMPORTS"),
        "forbidden_llama_modules": _string_set(tree, "FORBIDDEN_LLAMA_INDEX_MODULES"),
        "forbidden_llama_attrs": _string_set(tree, "FORBIDDEN_LLAMA_INDEX_ATTRIBUTES"),
        "model_stream_owners": _string_set(tree, "MODEL_STREAM_OWNERS"),
    }


def scan_web() -> dict[str, Any]:
    """The console: features, and where the frontend is allowed to reach the network."""
    web = PROJECT_ROOT / "web"
    features = []
    feat_dir = web / "src" / "features"
    if feat_dir.is_dir():
        for d in sorted(p for p in feat_dir.iterdir() if p.is_dir()):
            files = [
                f
                for f in d.rglob("*.ts*")
                if not f.name.endswith(".test.tsx") and not f.name.endswith(".test.ts")
            ]
            tests = [f for f in d.rglob("*.test.ts*")]
            features.append(
                {
                    "name": d.name,
                    "files": len(files),
                    "tests": len(tests),
                    "loc": sum(_loc(f) for f in files),
                }
            )
    net_files: list[str] = []
    for f in sorted((web / "src").rglob("*.ts*")) if (web / "src").is_dir() else []:
        if ".test." in f.name:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bfetch\(|new EventSource\(", text):
            net_files.append(_rel(f))
    scripts: dict[str, str] = {}
    node = ""
    pkg = web / "package.json"
    if pkg.is_file():
        data = json.loads(pkg.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {})
        node = data.get("engines", {}).get("node", "")
    return {
        "features": features,
        "network_files": net_files,
        "scripts": scripts,
        "node": node,
    }


def scan_tests() -> list[dict[str, Any]]:
    """Test directories with the number of test functions each one holds."""
    tests = PROJECT_ROOT / "tests"
    out: list[dict[str, Any]] = []
    for d in sorted(
        p for p in tests.iterdir() if p.is_dir() and not p.name.startswith("__")
    ):
        files = [f for f in d.rglob("test_*.py") if "__pycache__" not in f.parts]
        count = 0
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            count += len(re.findall(r"^\s*(?:async )?def test_", text, re.MULTILINE))
        out.append(
            {
                "name": d.name,
                "files": len(files),
                "tests": count,
                "loc": sum(_loc(f) for f in files),
            }
        )
    return out


def scan_entrypoints() -> list[dict[str, Any]]:
    """Console scripts, straight out of ``[project.scripts]``."""
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.scripts\]\n(.*?)(?:\n\[|\Z)", text, re.DOTALL)
    if not block:
        return []
    out = []
    for line in block.group(1).splitlines():
        m = re.match(r'\s*([a-z0-9-]+)\s*=\s*"([^"]+)"', line)
        if m:
            out.append({"name": m.group(1), "target": m.group(2)})
    return out


def scan_dev_commands() -> list[dict[str, Any]]:
    """The launch paths, read from the header comment that documents them."""
    text = (PROJECT_ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        m = re.match(r"#\s+scripts/dev\.sh\s+([a-z-]+)\s*#?\s*(.*)$", line)
        if m:
            out.append({"name": m.group(1), "doc": m.group(2).strip()})
    return out


def _dict_of_tuples(tree: ast.Module, name: str) -> dict[str, list[str]]:
    """A module-level ``{"a": ("b",), ...}`` literal, as plain data."""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        out: dict[str, list[str]] = {}
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            succs: list[str] = []
            if isinstance(value, (ast.Tuple, ast.List)):
                succs = [
                    e.value
                    for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
            out[key.value] = succs
        return out
    return {}


def scan_conditional_edges() -> dict[str, dict[str, list[str]]]:
    """The conditional edges, read from the ``add_conditional_edges`` calls.

    They are not in ``_STATIC_EDGES`` on purpose -- a node whose successor
    depends on state has no static successor -- so a picture drawn from that
    table alone shows a graph with holes where the interesting parts are. The
    compiler names every legal target in a list beside the router, and that
    list is what LangGraph is actually given, so it is the honest source.
    """
    py = PACKAGE_ROOT / "adapters" / "langgraph" / "workflow.py"
    tree = _parse(py)
    if tree is None:
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        edges: dict[str, list[str]] = {}
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr != "add_conditional_edges" or len(node.args) < 3:
                continue
            src = node.args[0]
            if not (isinstance(src, ast.Constant) and isinstance(src.value, str)):
                continue
            targets: list[str] = []
            arg = node.args[2]
            if isinstance(arg, (ast.List, ast.Tuple)):
                for e in arg.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        targets.append(e.value)
                    elif isinstance(e, ast.Name):
                        targets.append("END")
            elif isinstance(arg, ast.Call):
                # list(research_graph.RESEARCH_BRANCHES) — resolved by the caller
                targets.append("*BRANCHES*")
            if targets:
                edges[src.value] = targets
        if edges:
            out[fn.name] = edges
    return out


def scan_graphs() -> list[dict[str, Any]]:
    """Both workflow graphs, read from the edge table that compiles them.

    ``_STATIC_EDGES`` is described in ``research_graph.py`` as "the single
    source of which nodes exist" -- a node with no static successor is listed
    with an empty tuple rather than omitted, precisely so that reading this one
    table answers the question. So this reads that table and not the prose, and
    a node added to the graph appears in the panel without anyone remembering
    to add it.
    """
    conditional_by_builder = scan_conditional_edges()
    builders = {
        "research_graph": ("build_v1_graph", "build_research_graph"),
        "general_graph": ("build_v2_graph",),
    }
    out = []
    for stem, label in (("research_graph", "固定研究图"), ("general_graph", "通用图")):
        py = PACKAGE_ROOT / "workflows" / f"{stem}.py"
        if not py.is_file():
            continue
        tree = _parse(py)
        if tree is None:
            continue
        consts = _const_strings(tree)
        edges = _dict_of_tuples(tree, "_STATIC_EDGES")
        version = next(
            (v for k, v in consts.items() if k.startswith("GRAPH_VERSION")), stem
        )
        branches = _string_set(tree, "RESEARCH_BRANCHES")
        cond_edges: dict[str, list[str]] = {}
        for builder in builders.get(stem, ()):
            found = conditional_by_builder.get(builder)
            if found:
                cond_edges = {
                    node: (list(branches) if targets == ["*BRANCHES*"] else targets)
                    for node, targets in found.items()
                }
                break
        nodes = sorted(
            {
                consts.get("ENTRY_NODE", ""),
                *edges,
                *(t for v in edges.values() for t in v),
                *cond_edges,
                *(t for v in cond_edges.values() for t in v),
            }
            - {"", "END"}
        )
        out.append(
            {
                "version": version,
                "label": label,
                "module": _rel(py),
                "doc": _summary(ast.get_docstring(tree)),
                "entry": consts.get("ENTRY_NODE", ""),
                "terminal": consts.get("TERMINAL_NODE", ""),
                "nodes": nodes,
                "edges": edges,
                "conditional_edges": cond_edges,
                "conditional": sorted(_string_set(tree, "CONDITIONAL_NODES")),
                "branches": sorted(branches),
                "loc": _loc(py),
            }
        )
    return out


def scan_profiles_summary() -> dict[str, Any]:
    """The one schema version the code will accept, from the Literal that pins it."""
    settings = PACKAGE_ROOT / "bootstrap" / "settings.py"
    text = settings.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'config_schema_version:\s*Literal\["([^"]+)"\]', text)
    literals = re.findall(r"^\s*([a-z_]+):\s*Literal\[([^\]]+)\]", text, re.MULTILINE)
    single = [
        {"field": name, "value": values.strip()}
        for name, values in literals
        if values.count(",") == 0 and name != "config_schema_version"
    ]
    return {
        "schema_version": m.group(1) if m else "",
        "single_valued_literals": single,
        "path": _rel(settings),
    }


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


# --------------------------------------------------------------------------
# 2. The half that is prose, and the check that keeps it honest
# --------------------------------------------------------------------------
#
# Everything above is read from the tree. What follows cannot be: "the five
# gates on the loop" is an architectural claim, not a fact about a file, and a
# panel that only listed files would show a reader the parts and none of the
# shape. The compromise is that every entry names a real ``path`` and ``symbol``
# and ``--check`` fails when one of them stops existing -- so this text can go
# out of date only by someone deleting the thing it describes, which is a
# deletion they will hear about.

NARRATIVE: dict[str, Any] = {
    "claim": (
        "两种产品形态共用一份自研 Agent Runtime，而这份 Runtime 拥有全仓库唯一一条 "
        "model → tool → result → model 循环。LangGraph、LlamaIndex、MCP 一律从 "
        "Ports/Adapters 进来，谁都不许在这条循环里占一轮。"
    ),
    "runtime_intro": [
        {
            "path": "src/agent_workbench/runtime/agent_runtime.py",
            "symbol": "ClaudeLikeAgentRuntime",
            "body": (
                "ClaudeLikeAgentRuntime._run 是一个 while True。它的循环体检查五道闸，跑<b>恰好一次</b>模型流，"
                "把这次调用映射成一个终态或者一批工具调用，把这批调用交给 Tool Gateway 的四个阶段，"
                "再把结果按模型自己的调用顺序回填，然后进入下一轮。全仓唯一一处 "
                '<span class="mono">async for</span> 消费模型流就在这里——这一点由 '
                '<span class="mono">test_the_model_tool_loop_has_exactly_one_owner</span> 用两种形状同时守住：'
                '一种按 AST 找循环，一种按谁能 import <span class="mono">ports/model</span>。'
            ),
        }
    ],
    "loop": [
        {
            "title": "取消检查",
            "where": "cancellation.cancelled",
            "body": '一轮里查六次，这是第一次。被取消时，已经准备好的调用会逐个变成 <span class="mono">cancelled</span> 的 ToolResult ——'
            "它们仍然欠模型一个答复，不能凭空消失。",
        },
        {
            "title": "预算闸（开始这一轮之前）",
            "where": "domain/runs.py :: halt_reason_for",
            "body": '步数、token、成本、截止各有上限。<span class="mono">max_tool_calls</span> <b>故意不在这里问</b>：'
            "一个用光了工具额度的运行，仍然应该有一轮把答案写出来。",
        },
        {
            "title": "上下文闸",
            "where": "context_reason_for（ADR-080）",
            "body": "看的是<b>上一次请求实际有多大</b>，不是累计 token —— 累计值随轮数近似平方增长，用它判断窗口会越判越早。",
        },
        {
            "title": "压缩（只在上一步跳闸时）",
            "where": "runtime/compaction.py :: plan_compaction（ADR-081）",
            "body": "头一条永远留下；切点向后推到协议边界，绝不把 tool_use 和它的结果劈开；摘要以 <b>assistant</b> 身份回到对话，"
            '而不是假借用户的口。压不动就承认压不动，运行以 <span class="mono">context_limit</span> 停下，而不是硬答。',
        },
        {
            "title": "决定这一轮广告哪些工具",
            "where": "budget.tool_allowance_spent",
            "body": "额度花光时工具<b>从请求里撤下</b>，而不是留在那里等着被拒——模型看不见的工具，不会被反复提议。",
        },
        {
            "title": "模型流",
            "where": "_stream_model → _consume",
            "body": '一次调用，一条流，整段套在 <span class="mono">asyncio.timeout(deadline)</span> 里。'
            "deadline 取「运行截止」与「运行时信封」中更内层的那个，并且记住是哪一个赢了：前者是 "
            '<span class="mono">budget_exceeded</span>，后者是可重试的 <span class="mono">provider_error</span>。',
        },
        {
            "title": "终态判定",
            "where": "_terminal_for_turn（8 条分支）",
            "body": "没有工具调用就是完成；有工具调用就往下走一批。三个终态只有 completed / failed / cancelled，"
            '<span class="mono">stop_reason</span> 共 9 个值，没有「看起来成功」这一档。',
        },
        {
            "title": "准入",
            "where": "ToolGateway.propose + 两个断路器",
            "body": "每一个被提议的调用都先留痕，包括下一步就要被拒的。然后工具额度切一刀，超出的直接拒；"
            "同名同参数第 3 次出现也拒——签名计数<b>在其它检查之前</b>就加，因为一次拒绝对模型很便宜，"
            "它会一直重提到把步数烧光。",
        },
        {
            "title": "网关",
            "where": "runtime/tool_gateway.py :: ToolGateway",
            "body": "prepare（解析 · 大小 · schema · 钩子）与 authorize（最多 3 轮策略决定，之后才谈审批）。"
            "详见下一节。",
        },
        {
            "title": "调度",
            "where": "runtime/tool_scheduler.py :: plan_tool_batches",
            "body": "纯函数，两条规则：连续的只读调用凑成一组、最多 4 个；写 / 外部 / 破坏性的一律独占成组。"
            '而「哪些是独占的」不是调度器的判断——<span class="mono">ToolSpec</span> 在构造期就拒绝一个'
            "非只读却声明可并行的规格。",
        },
        {
            "title": "执行",
            "where": "runtime/tool_executor.py :: ToolExecutor",
            "body": "一次一个 handler，时限取「工具声明 / 运行剩余 / 部署上限」三者最小。每 5 秒一次心跳事件——"
            "心跳<b>不带百分比</b>，因为「已过时间 ÷ 声明超时」看起来像进度，而它不是（ADR-068）。",
        },
        {
            "title": "对齐与写回",
            "where": "domain/tools.py :: align_results",
            "body": "执行是真并行的，结果却按模型自己的调用顺序回填。然后把 assistant 与 tool 两条消息写进账本，"
            "并且只给<b>被准入的</b>调用计费——被额度挡下的那些不算。",
        },
    ],
    "gates": [
        {
            "title": "预算",
            "path": "src/agent_workbench/domain/runs.py",
            "symbol": "halt_reason_for",
            "body": "三个谓词分管三处：开始一轮前、这轮 token 结算后、派发工具前。预算是<b>值</b>，请求只能收紧不能放宽。"
            "声明了成本上限却没有价目表时，运行在第一次调用之前就被拒——一个执行不了的上限不如没有。",
        },
        {
            "title": "截止",
            "path": "src/agent_workbench/runtime/budgets.py",
            "symbol": "effective_model_deadline",
            "body": "内层的那个赢，并且结果里记着是哪一个赢的。模型 profile 自己的超时刻意不在这里，它在 adapter 里，"
            "嵌在这道界限之内。",
        },
        {
            "title": "上下文",
            "path": "src/agent_workbench/runtime/compaction.py",
            "symbol": "plan_compaction",
            "body": "超过窗口 × 0.75 触发。压缩本身是一次普通的模型调用（profile 为 compact），它的 token 与成本"
            "<b>失败时照样计入</b>——供应商已经收了这笔钱；但 steps 不加，因为循环没有前进一步。",
        },
        {
            "title": "取消",
            "path": "src/agent_workbench/runtime/agent_runtime.py",
            "symbol": "_refuse_cancelled",
            "body": "一轮里查六次，其中一次专门放在压缩调用之后：一个被取消的摘要器不能被记成「上下文超限」。",
        },
        {
            "title": "重复调用",
            "path": "src/agent_workbench/runtime/agent_runtime.py",
            "symbol": "MAX_IDENTICAL_CALLS",
            "body": "两种机制。同一轮里重复的 tool_call_id 直接判整轮失败（那是供应商的错，不是模型的选择）；"
            "跨轮的同名同参数第 3 次被拒，连续被拒超过 2 次运行结束——<b>并且是在把那些拒绝写进消息之后</b>才结束，"
            "所以运行终止时手里还握着它被告知过什么。",
        },
    ],
    "gateway_intro": [
        {
            "path": "src/agent_workbench/runtime/tool_gateway.py",
            "symbol": "ToolGateway",
            "body": (
                '原生 handler、MCP 工具、LangChain 工具都以同一个 <span class="mono">ToolBinding</span> 到达这里，'
                "所以「这次能不能跑、用这些参数、在此刻」只有一个实现。默认是拒绝，依据是<b>提交时冻结</b>的授权信封。"
                "每一条离开这里的路径，不是 refuse() 就是 _record()——所以模型手上那个 tool_call_id 恰好被一个 ToolResult 关掉。"
            ),
        }
    ],
    "gateway": [
        {
            "title": "advertise —— 每次运行一次，不是每次调用一次",
            "where": "ToolGateway.advertise",
            "body": '未注册的名字抛 <span class="mono">UnknownToolError</span>；带 <span class="mono">operation_key</span> 的'
            '（会记账本的副作用）抛 <span class="mono">PolicyDeniedError</span>——ADR-075：那种工具由节点<b>签发</b>，'
            "从不摆到模型面前让它提议。",
        },
        {
            "title": "① propose —— 每一个都留痕",
            "where": "ToolProposed",
            "body": "记下参数字节数与 SHA-256。<b>包括下一步就要被拒的那些</b>：被拒的调用从事件流里消失，"
            "等于把「有人试过这件事」这条信息也一起删了。",
        },
        {
            "title": "② prepare —— 解析、检查、钩子",
            "where": "ToolGateway.prepare",
            "body": '解析绑定（找不到 → <span class="mono">unknown_tool</span>）→ 规范化后 ≤ 65,536 字节 → '
            "按 spec 的 JSON Schema 校验（支持 17 个关键字的子集，用到子集外的关键字会让<b>进程</b>起不来，"
            '而不是让这次调用失败）→ <span class="mono">before_tool</span> 钩子。'
            "钩子若改写了参数，大小与 schema <b>再查一遍</b>；钩子只能改参数，改不了工具名，也改不了 tool_call_id。",
        },
        {
            "title": "③ authorize —— 三个答案，没有第四个",
            "where": "ports/policy.py :: PolicyEngine",
            "body": 'allow / deny / allow_with_modified_input。每一轮都发一条 <span class="mono">PermissionResolved</span>，'
            "所以被拒的那次也留下了它走到哪一步。改写会重新走一遍检查<b>并再问一次</b>——否则改写就成了同时绕过两道检查的路；"
            "3 轮不收敛则拒。「需要审批」是<b>粘性</b>的：后一轮忘了重复也撤不掉。",
        },
        {
            "title": "③b 审批 —— 「allow，但要审批」从不当作 allow",
            "where": "ports/approval_gate.py",
            "body": '没有审批闸的部署直接以 <span class="mono">approval_required</span> 拒绝；有闸则 PermissionRequested + '
            "RunPaused，然后与取消赛跑地等。闸给回的答案要落在合法词表里才算数——一个不认识的词不能靠"
            "「它不等于 deny」变成许可。超时、取消、闸自己报错，都记成 deny 并留痕。",
        },
        {
            "title": "④ invoke —— 距不可逆那一行，再授权一次",
            "where": "ports/tool_executions.py",
            "body": '没有 <span class="mono">operation_key</span> 的直接派发。有的先记意图，再<b>第二次</b>问策略：'
            "只认 allow 且不再要求审批；这一次的改写不予采纳，因为已经记下的意图必须描述真正发生的那次调用。"
            "账本说这件事已经做过，就不会再做第二遍。答不上来（超时 / 取消 / 超预算）时标记<b>待人工对账</b>——"
            "对一次外部写入而言，「没收到答复」不等于「没有发生」。",
        },
    ],
    "decisions": [
        {
            "title": "allow",
            "tone": "on",
            "body": "在提交时冻结的信封之内，且原则的权限范围齐备。",
            "then": "进入调度与执行；如果这个工具会记账本，还要在派发前再问一次。",
        },
        {
            "title": "deny",
            "tone": "deny",
            "body": "未知工具 / 不在提交时的信封里 / 缺少权限范围。<b>哪一个范围缺</b>刻意不写进 reason_code。",
            "then": "留下 PermissionResolved 与 ToolFailed，模型拿到一个说明性的错误而不是沉默。",
        },
        {
            "title": "allow_with_modified_input",
            "tone": "warn",
            "body": "策略改写了参数。这是一个<b>决定</b>，不是一个副作用。",
            "then": "重新按 schema 校验改写后的参数，然后<b>再问一次</b>。最多 3 轮，不收敛就拒。",
        },
    ],
    "chat": [
        {
            "title": "幂等认领这一回合",
            "where": "application/chat.py",
            "body": '<span class="mono">Idempotency-Key</span> 推出稳定的 run_id；重放一个已提交的回合直接返回原答案，'
            "同一会话不允许并行的活跃回合。",
        },
        {
            "title": "两条臂并行召回",
            "where": "adapters/vector/",
            "body": "稠密向量 + 稀疏词法，各取 top_k×4。Qdrant 是派生副本，不是事实源。",
        },
        {
            "title": "RRF 融合",
            "where": "adapters/vector/fusion.py",
            "body": '在本进程内跑<b>一次</b>，纯函数，按 <span class="mono">(-score, chunk_id)</span> 定序——同分也可复现。'
            "「融合归属于应用」是写进配置的单值 Literal 的，改它要先写 ADR。",
        },
        {
            "title": "PostgreSQL ACL 过滤 —— 授权发生在这里",
            "where": "application/retrieval.py",
            "body": '只留 <span class="mono">source_revision</span> 完全一致的候选，并把每份文档的 revision 带出来，'
            "供发布前那次复核使用。",
        },
        {
            "title": "重排",
            "where": "adapters/reranking/",
            "body": "只对<b>已授权</b>的候选打分，15 秒超时，失败就按输入顺序放行而不假装排过。"
            "顺序决定了这一步不可能引入提问者无权读的段落。",
        },
        {
            "title": "生成",
            "where": "ports/agent_executor.py",
            "body": "渲染上下文 → 同一个 Runtime。固定形态的授权信封是空的；agentic 形态只给一个 "
            '<span class="mono">knowledge_search</span>。模型文本在发布前一直从事件流里挡住。',
        },
        {
            "title": "引用校验",
            "where": "application/citations.py",
            "body": "只认「模型点名过 <b>并且</b> 确实展示过」的段落。编造的引用被计数，但不会返回。",
        },
        {
            "title": "发布围栏 —— 这条路径的要点",
            "where": "application/answer_release.py",
            "body": "在<b>一个事务</b>里复核 revision 与 ACL，锁序是会话 → 回合 → 文档（按 id 排序）→ 事件流。"
            '撤权发生在生成之后、发布之前时，系统扣下答案（<span class="mono">AnswerWithheld</span>），'
            "而不是把它发出去。答案、助手历史、回合终态在同一个事务里提交。",
        },
    ],
    "task": [
        {
            "title": "提交",
            "where": "application/",
            "body": "租户级幂等键 + 输入指纹；<b>冻结</b> graph_version；授权信封随 Task 一起存下，之后每次恢复重新施加。",
        },
        {
            "title": "认领",
            "where": "adapters/persistence/",
            "body": '<span class="mono">FOR UPDATE SKIP LOCKED</span>，拿到一个<b>不可变</b>的 ExecutionLease 与 epoch。',
        },
        {
            "title": "认领后判定",
            "where": "application/task_recovery.py",
            "body": "只看 Registry 状态与 checkpoint 位置，没有 I/O 的纯判定——所以它可以被单独测，而不必先杀一个进程。",
        },
        {
            "title": "执行冻结的那个版本",
            "where": "adapters/langgraph/",
            "body": '检查点写入前过 fence：<span class="mono">SELECT … FOR UPDATE</span> 加 advisory lock 检查。'
            "检查点自己也有版本与升级路径（ADR-100）。",
        },
        {
            "title": "每个节点",
            "where": "workflows/agent_profiles.py",
            "body": "重取身份与信封，可用工具是「画像 ∩ 信封」。画像只做交集，没有能反转方向的参数。",
        },
        {
            "title": "审批",
            "where": "workflows/approval.py",
            "body": "全图唯一的中断点。决定写进权威账本，跨进程恢复后<b>重新施加</b>，而不是重新问一次人。"
            "不需要审批的部署里这道闸被跳过，而不是被伪造成一条通过记录。",
        },
        {
            "title": "崩溃恢复",
            "where": "workflows/execution_scope.py",
            "body": "心跳停了 → 租约到期 → 另一个 Worker 以<b>新 epoch</b> 重认领 → 从检查点续跑。"
            "节点在<b>领取时</b>拿到的那个租约下写入，不回头向 Registry 问当前 epoch——否则一个已经失去租约的 Worker "
            "会拿着顶替者的 epoch 通过账本围栏，而那正是围栏要挡的事。",
        },
    ],
}


def check_narrative() -> list[str]:
    """Paths named in NARRATIVE that no longer exist. Empty is the healthy answer."""
    problems: list[str] = []
    for section, entries in NARRATIVE.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            path = entry.get("path")
            if not path:
                continue
            target = PROJECT_ROOT / path
            if not target.exists():
                problems.append(f"{section}: {path} 不存在（NARRATIVE 里还在描述它）")
                continue
            symbol = entry.get("symbol")
            if symbol and target.is_file():
                text = target.read_text(encoding="utf-8", errors="replace")
                if symbol not in text:
                    problems.append(f"{section}: {path} 里找不到 {symbol}")
    return problems


# --------------------------------------------------------------------------
# 3. Assembling, and drawing
# --------------------------------------------------------------------------


def build_data() -> dict[str, Any]:
    packages = scan_packages()
    tests = scan_tests()
    tools = scan_tools()
    return {
        "commit": git_commit(),
        "claim": NARRATIVE["claim"],
        "layers": LAYERS,
        "packages": packages,
        "narrative": {k: v for k, v in NARRATIVE.items() if k != "claim"},
        "endpoints": scan_endpoints(),
        "tools": tools,
        "adrs": scan_adrs(),
        "profiles": scan_profiles(),
        "settings": scan_profiles_summary(),
        "guards": scan_guards(),
        "web": scan_web(),
        "tests": tests,
        "entrypoints": scan_entrypoints(),
        "dev_commands": scan_dev_commands(),
        "graphs": scan_graphs(),
        "totals": {
            "python_loc": sum(p["loc"] for p in packages),
            "python_files": sum(p["files"] for p in packages),
            "core_loc": sum(
                p["loc"]
                for p in packages
                if PACKAGE_LAYER.get(p["name"], "outer") not in {"adapters", "apps"}
            ),
            "tests": sum(t["tests"] for t in tests),
            "test_files": sum(t["files"] for t in tests),
            "endpoints": len(scan_endpoints()),
            "tools": len(tools),
            "adrs": len(scan_adrs()),
            **{f"ports_{k}": v for k, v in count_protocols().items()},
        },
    }


def load_diagrams() -> dict[str, str]:
    """The SVGs from ``docs/assets``, inlined so the page needs no second request.

    The same files are what the README embeds. One drawing, two readers: a
    diagram maintained twice is a diagram that disagrees with itself by the
    third edit, and the disagreement is invisible because nobody opens both at
    once.
    """
    out: dict[str, str] = {}
    if not ASSET_DIR.is_dir():
        return out
    for svg in sorted(ASSET_DIR.glob("*.svg")):
        text = svg.read_text(encoding="utf-8")
        # Strip the XML prolog: inlined into HTML it is illegal, and browsers
        # that tolerate it do so by ignoring everything up to the first tag.
        text = re.sub(r"^<\?xml[^>]*\?>\s*", "", text)
        out[svg.stem] = text
    return out


# The page. One file, no build step, no network: the panel has to work on a
# machine with the checkout and nothing else running, because the moment it
# needs `pnpm install` it stops being the thing you open to find your bearings
# and becomes another service to start.
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Agent Workbench · 架构面板</title>
<style>
:root {
  color-scheme: light dark;
  --aw-canvas: light-dark(#f4f4f2, #151514);
  --aw-window: light-dark(#fcfcfb, #1c1c1b);
  --aw-sidebar: light-dark(#f4f4f2, #171716);
  --aw-panel: light-dark(#f7f7f5, #232321);
  --aw-panel-soft: light-dark(#eeeeeb, #292927);
  --aw-panel-strong: light-dark(#e7e7e3, #30302d);
  --aw-raised: light-dark(#ecece8, #2c2c29);
  --aw-sunken: light-dark(#f2f2ef, #161615);
  --aw-text: light-dark(#282825, #ecece8);
  --aw-text-soft: light-dark(#595954, #b5b5ae);
  --aw-text-muted: light-dark(#6d6d67, #9c9c95);
  --aw-faint: light-dark(#85857e, #777770);
  --aw-border: light-dark(#e5e5e1, #32322f);
  --aw-border-strong: light-dark(#d6d6d0, #41413c);
  --aw-accent: light-dark(#b75d42, #d98265);
  --aw-accent-soft: light-dark(#f8eee9, #241812);
  --aw-accent-on: light-dark(#ffffff, #1d1411);
  --aw-success: light-dark(#2f7a54, #6cba8b);
  --aw-success-soft: light-dark(#edf5f0, #0b1911);
  --aw-warning: light-dark(#9a6724, #d6a45c);
  --aw-warning-soft: light-dark(#faf2e4, #1b1607);
  --aw-danger: light-dark(#b44f4b, #e58e88);
  --aw-danger-soft: light-dark(#fbeceb, #231110);
  --aw-evidence: light-dark(#3f6472, #8fb7c6);
  --aw-evidence-soft: light-dark(#eef3f5, #0c171d);
  --aw-shadow: 0 1px 2px light-dark(rgb(24 24 21 / 5%), rgb(0 0 0 / 42%));
  --aw-radius: 12px;
  --aw-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI",
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --aw-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, "JetBrains Mono",
    Consolas, monospace;
}
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--aw-canvas); color: var(--aw-text);
  font-family: var(--aw-sans); font-size: 14.5px; line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--aw-accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, .mono { font-family: var(--aw-mono); font-size: 0.88em; }

/* ---- shell ---- */
.shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; }
nav.rail {
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
  background: var(--aw-sidebar); border-right: 1px solid var(--aw-border);
  padding: 20px 12px 40px; display: flex; flex-direction: column; gap: 2px;
}
.brand { padding: 4px 10px 16px; }
.brand b { display: block; font-size: 15px; letter-spacing: -0.01em; }
.brand span { display: block; color: var(--aw-faint); font-size: 11.5px; font-family: var(--aw-mono); }
.rail a.nav {
  display: flex; align-items: center; gap: 9px; padding: 7px 10px;
  border-radius: 8px; color: var(--aw-text-soft); font-size: 13.5px;
}
.rail a.nav:hover { background: var(--aw-panel-soft); text-decoration: none; color: var(--aw-text); }
.rail a.nav.active { background: var(--aw-panel-strong); color: var(--aw-text); font-weight: 500; }
.rail a.nav .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--aw-border-strong); flex: none; }
.rail a.nav.active .dot { background: var(--aw-accent); }
.rail .group { margin-top: 14px; padding: 6px 10px 4px; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--aw-faint); }
.rail .foot { margin-top: auto; padding: 16px 10px 0; font-size: 11.5px; color: var(--aw-faint); }

main { padding: 34px 40px 96px; max-width: 1180px; }
section { scroll-margin-top: 24px; margin-bottom: 64px; }
h1 { font-size: 27px; letter-spacing: -0.02em; margin: 0 0 8px; }
h2 { font-size: 20px; letter-spacing: -0.01em; margin: 0 0 6px; }
h3 { font-size: 15px; margin: 26px 0 8px; }
.lede { color: var(--aw-text-soft); margin: 0 0 22px; max-width: 74ch; }
.eyebrow { font-size: 11px; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--aw-faint); font-weight: 600; margin-bottom: 6px; }

/* ---- primitives ---- */
.card { background: var(--aw-window); border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius); padding: 16px 18px; box-shadow: var(--aw-shadow); }
.grid { display: grid; gap: 12px; }
.stats { grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); }
.stat b { display: block; font-size: 25px; font-family: var(--aw-mono); letter-spacing: -0.02em; }
.stat span { color: var(--aw-text-muted); font-size: 12px; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px;
  border-radius: 999px; font-size: 11.5px; font-family: var(--aw-mono);
  background: var(--aw-panel-soft); color: var(--aw-text-soft); border: 1px solid var(--aw-border); }
.chip.core { background: var(--aw-accent-soft); color: var(--aw-accent); border-color: transparent; }
.chip.outer { background: var(--aw-evidence-soft); color: var(--aw-evidence); border-color: transparent; }
.chip.off { background: var(--aw-panel-soft); color: var(--aw-faint); }
.chip.on { background: var(--aw-success-soft); color: var(--aw-success); border-color: transparent; }
.chip.warn { background: var(--aw-warning-soft); color: var(--aw-warning); border-color: transparent; }
.chip.deny { background: var(--aw-danger-soft); color: var(--aw-danger); border-color: transparent; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-weight: 500; color: var(--aw-text-muted); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: 0.06em; padding: 0 10px 8px; border-bottom: 1px solid var(--aw-border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--aw-border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
.scroll { overflow-x: auto; }
.muted { color: var(--aw-text-muted); }
.faint { color: var(--aw-faint); }
.note { border-left: 3px solid var(--aw-border-strong); padding: 2px 0 2px 14px;
  color: var(--aw-text-soft); margin: 14px 0; }
.note.warn { border-color: var(--aw-warning); }
figure { margin: 18px 0 8px; }
figure svg { width: 100%; height: auto; display: block; }
figcaption { color: var(--aw-faint); font-size: 12px; margin-top: 8px; }
.searchbar { display: flex; gap: 8px; align-items: center; margin: 0 0 14px; }
.searchbar input {
  flex: 1; padding: 8px 12px; border-radius: 9px; border: 1px solid var(--aw-border-strong);
  background: var(--aw-window); color: inherit; font: inherit; font-size: 13.5px;
}
.searchbar input:focus { outline: 2px solid var(--aw-accent); outline-offset: 1px; }
button.seg { padding: 5px 11px; border-radius: 999px; border: 1px solid var(--aw-border);
  background: var(--aw-window); color: var(--aw-text-soft); font: inherit; font-size: 12.5px; cursor: pointer; }
button.seg.on { background: var(--aw-accent); color: var(--aw-accent-on); border-color: transparent; }
details.mod { border-bottom: 1px solid var(--aw-border); }
details.mod > summary { cursor: pointer; padding: 9px 4px; display: grid;
  grid-template-columns: minmax(180px, 260px) minmax(0,1fr) 62px; gap: 12px; align-items: baseline; }
details.mod > summary::-webkit-details-marker { display: none; }
details.mod > summary:hover { background: var(--aw-panel-soft); }
details.mod .path { font-family: var(--aw-mono); font-size: 12.5px; }
details.mod .doc { color: var(--aw-text-soft); font-size: 13px; }
details.mod .loc { font-family: var(--aw-mono); font-size: 12px; color: var(--aw-faint); text-align: right; }
.symbols { padding: 4px 4px 14px 20px; display: grid; gap: 4px; }
.symbols div { font-size: 12.5px; color: var(--aw-text-soft); }
.symbols .s { font-family: var(--aw-mono); color: var(--aw-text); }
.pkg-head { display: flex; align-items: baseline; gap: 10px; margin: 30px 0 4px; }
.pkg-head h3 { margin: 0; font-family: var(--aw-mono); font-size: 15px; }
.flow { display: grid; gap: 0; margin: 16px 0; }
.step { display: grid; grid-template-columns: 30px minmax(0,1fr); gap: 14px; }
.step .n { font-family: var(--aw-mono); font-size: 12px; color: var(--aw-accent);
  border: 1px solid var(--aw-border); border-radius: 999px; width: 26px; height: 26px;
  display: grid; place-items: center; background: var(--aw-window); }
.step .body { padding-bottom: 18px; border-left: 1px solid var(--aw-border);
  margin-left: -22px; padding-left: 34px; }
.step:last-child .body { border-left-color: transparent; }
.step .st { display: block; font-size: 14px; font-weight: 600; }
.step .where { font-family: var(--aw-mono); font-size: 12px; color: var(--aw-faint); }
.theme { position: fixed; top: 16px; right: 20px; z-index: 5; }
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  nav.rail { position: static; height: auto; flex-direction: row; flex-wrap: wrap; }
  .rail .group, .rail .foot { display: none; }
  main { padding: 24px 18px 80px; }
}
</style>
</head>
<body>
<div class="theme"><button class="seg" id="theme">主题</button></div>
<div class="shell">
  <nav class="rail" id="rail"></nav>
  <main id="main"></main>
</div>
<script id="panel-data" type="application/json">__DATA__</script>
<script id="panel-diagrams" type="application/json">__DIAGRAMS__</script>
<script>
const DATA = JSON.parse(document.getElementById("panel-data").textContent);
const SVGS = JSON.parse(document.getElementById("panel-diagrams").textContent);

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content; };
const n = (x) => Number(x || 0).toLocaleString("en-US");

/* A figure is only drawn when the SVG for it exists. The panel is built from a
   working tree, and a diagram file someone deleted should leave a gap in the
   page rather than an empty frame with a caption that still describes it. */
function figure(key, caption) {
  if (!SVGS[key]) return "";
  return `<figure>${SVGS[key]}${caption ? `<figcaption>${esc(caption)}</figcaption>` : ""}</figure>`;
}

function stepList(steps) {
  return `<div class="flow">` + steps.map((s, i) => `
    <div class="step">
      <div class="n">${i + 1}</div>
      <div class="body">
        <span class="st">${esc(s.title)}</span>
        ${s.where ? `<div class="where">${esc(s.where)}</div>` : ""}
        <div class="muted">${s.body || ""}</div>
      </div>
    </div>`).join("") + `</div>`;
}

/* ---------------------------------------------------------------- sections */

const SECTIONS = [];
const add = (id, label, group, render) => SECTIONS.push({ id, label, group, render });

add("overview", "概览", "全景", () => {
  const t = DATA.totals;
  const stats = [
    [n(t.python_loc), "行 Python"],
    [n(t.python_files), "个模块"],
    [n(t.ports_protocols), "个 Protocol 契约"],
    [n(t.tests), "个测试函数"],
    [n(t.endpoints), "个 HTTP 端点"],
    [n(t.tools), "个工具"],
    [n(t.adrs), "份 ADR"],
    [DATA.settings.schema_version || "—", "配置 schema"],
    [n(DATA.settings.single_valued_literals.length), "条单值不变量"],
    [n(DATA.profiles.length), "个配置画像"],
  ];
  return `
    <div class="eyebrow">Agent Workbench</div>
    <h1>一份 Runtime，两种产品形态</h1>
    <p class="lede">${esc(DATA.claim)}</p>
    <div class="grid stats">${stats.map(([a, b]) => `
      <div class="card stat"><b>${a}</b><span>${esc(b)}</span></div>`).join("")}</div>
    ${figure("arch-layers", "依赖箭头一律由外向内。核心层不认识任何框架——这不是约定，是一条会让 CI 变红的测试。")}
    <h3>怎么把它跑起来</h3>
    <div class="note"><b>这一栏是 macOS / Linux 的写法。</b>
      <span class="mono">scripts/dev.sh</span> 是 bash，Windows 上跑不了；这一页本身在 Windows 上开
      <span class="mono">scripts\\panel.cmd</span>（双击也行），或者
      <span class="mono">python scripts\\architecture_panel.py --serve</span>。
      面板不 import 标准库以外的任何东西，所以它不需要 <span class="mono">uv sync</span>、不需要虚拟环境、
      也不需要下面那些服务里的任何一个——机器上已经有的那个 Python 就够。
      表里其余的命令要真正的服务，那部分目前只有 bash 一条路。</div>
    <div class="scroll"><table><thead><tr><th>命令</th><th>它做什么</th></tr></thead><tbody>
      ${DATA.dev_commands.map((c) => `<tr><td class="mono">scripts/dev.sh ${esc(c.name)}</td><td class="muted">${esc(c.doc)}</td></tr>`).join("")}
    </tbody></table></div>`;
});

add("layers", "分层与守卫", "全景", () => {
  const byLayer = {};
  DATA.packages.forEach((p) => { (byLayer[p.layer] = byLayer[p.layer] || []).push(p); });
  const g = DATA.guards;
  return `
    <div class="eyebrow">Architecture</div>
    <h2>七个层，一条会变红的测试</h2>
    <p class="lede">每一层能依赖谁是写死的。越界不是评审时被看出来，是 CI 里一条断言失败。</p>
    <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));">
      ${DATA.layers.map((L) => {
        const pkgs = byLayer[L.id] || [];
        const loc = pkgs.reduce((a, p) => a + p.loc, 0);
        return `<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
            <h3 style="margin:0;font-family:var(--aw-mono)">${esc(L.title)}</h3>
            <span class="chip ${L.kind}">${L.kind === "core" ? "核心层" : "外层"}</span>
          </div>
          <div class="faint" style="font-size:12.5px;margin-bottom:8px">${esc(L.tagline)}</div>
          <div class="muted" style="font-size:13px">${esc(L.blurb)}</div>
          <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
            ${pkgs.map((p) => `<span class="chip">${esc(p.name)} · ${n(p.loc)}</span>`).join("")}
            <span class="chip">${n(loc)} 行</span>
          </div>
        </div>`;
      }).join("")}
    </div>
    <h3>守卫的具体内容（读自 <span class="mono">${esc(g.path || "")}</span>）</h3>
    <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));">
      <div class="card"><b>核心层第三方白名单</b>
        <p class="muted" style="font-size:13px">名单之外一律红，无论有没有人想到过要禁它。</p>
        <div>${(g.core_allowlist || []).map((x) => `<span class="chip on">${esc(x)}</span> `).join("")}</div></div>
      <div class="card"><b>具名拒绝的集成</b>
        <p class="muted" style="font-size:13px">${(g.forbidden_core || []).length} 条。白名单管「一律拒绝」，这张表管诊断信息：报错说的是「把它挪到 adapter 后面去」。</p>
        <div style="max-height:120px;overflow:auto">${(g.forbidden_core || []).map((x) => `<span class="chip deny">${esc(x)}</span> `).join("")}</div></div>
      <div class="card"><b>连方法调用一起禁</b>
        <p class="muted" style="font-size:13px">这两个属性名挂在本项目确实会建的 VectorStoreIndex 上，不需要新的 import，所以 import 形态的守卫看不见它们。</p>
        <div>${(g.forbidden_llama_attrs || []).map((x) => `<span class="chip deny">${esc(x)}()</span> `).join("")}</div></div>
      <div class="card"><b>模型流的持有者</b>
        <p class="muted" style="font-size:13px">只有这些模块可以 import <span class="mono">ports/model</span>。名单之外拿不到模型流，也就写不出第二条工具循环。</p>
        <div style="max-height:120px;overflow:auto">${(g.model_stream_owners || []).map((x) => `<span class="chip">${esc(x)}</span> `).join("")}</div></div>
    </div>`;
});

add("runtime", "Agent Runtime", "内核", () => {
  const runtime = DATA.packages.find((p) => p.name === "runtime");
  const mods = runtime ? runtime.groups.flatMap((g) => g.modules) : [];
  const nar = DATA.narrative;
  return `
    <div class="eyebrow">The only tool loop</div>
    <h2>Agent Runtime</h2>
    <p class="lede">${(nar.runtime_intro || [{}])[0]?.body || ""}</p>
    ${figure("agent-runtime-loop", "一轮循环：模型流 → 工具批 → 结果 → 回到模型。五道闸都长在这条线上，不在调用方那边。")}
    <h3>一轮循环，按顺序</h3>
    ${stepList(nar.loop || [])}
    <h3>五道闸</h3>
    <div class="scroll"><table><thead><tr><th>闸</th><th>它拦的是什么</th><th>落点</th></tr></thead><tbody>
      ${(nar.gates || []).map((x) => `<tr>
        <td><b>${esc(x.title)}</b></td>
        <td class="muted">${x.body || ""}</td>
        <td class="mono faint">${esc(x.path || "")}${x.symbol ? "<br/>" + esc(x.symbol) : ""}</td></tr>`).join("")}
    </tbody></table></div>
    <h3>runtime/ 的每个模块</h3>
    ${moduleTable(mods)}`;
});

add("gateway", "Tool Gateway", "内核", () => {
  const nar = DATA.narrative;
  return `
    <div class="eyebrow">Authorization</div>
    <h2>Tool Gateway：一次工具调用要穿过的门</h2>
    <p class="lede">${(nar.gateway_intro || [{}])[0]?.body || ""}</p>
    ${figure("tool-gateway-pipeline", "五个阶段由调用方按顺序驱动，每一条出口都产生恰好一个 ToolResult。")}
    ${stepList(nar.gateway || [])}
    <h3>三个答案，没有第四个</h3>
    <div class="scroll"><table><thead><tr><th>决定</th><th>含义</th><th>之后发生什么</th></tr></thead><tbody>
      ${(nar.decisions || []).map((x) => `<tr><td><span class="chip ${x.tone || ""}">${esc(x.title)}</span></td>
        <td class="muted">${x.body || ""}</td><td class="muted">${x.then || ""}</td></tr>`).join("")}
    </tbody></table></div>`;
});

add("flows", "两条主链路", "内核", () => {
  const nar = DATA.narrative;
  return `
    <div class="eyebrow">Request paths</div>
    <h2>一次问答，一次任务</h2>
    ${figure("chat-flow", "Chat：授权发生在 PostgreSQL 过滤那一步，重排跑在它之后；发布前再复核一次。")}
    ${stepList(nar.chat || [])}
    ${figure("task-flow", "Task：租约 + 心跳 + epoch fencing。崩溃不是数据丢失，是另一个 Worker 换新 epoch 从检查点续跑。")}
    ${stepList(nar.task || [])}
    ${figure("delegation", "一次委派是一次运行，不是第二个执行器：委派工具调用的是同一个 AgentExecutor。")}`;
});

add("graphs", "工作流图", "内核", () => {
  return `
    <div class="eyebrow">LangGraph</div>
    <h2>两张图，提交时选定并冻结</h2>
    <p class="lede">下面的节点与边不是抄来的，是从 <span class="mono">_STATIC_EDGES</span> 里读出来画的——那张表在源码里被称为「哪些节点存在」的唯一事实源。</p>
    ${DATA.graphs.map(graphCard).join("")}`;
});

function graphCard(g) {
  const L = layoutGraph(g);
  const CW = 134, RH = 58, BW = 98, BH = 32;
  const W = 60 + L.cols.length * CW + (L.hasEnd ? 62 : 0), H = 88 + L.rows * RH + (L.back.length ? 44 : 0);
  const pos = {};
  const midY = 54 + (L.rows * RH) / 2;
  L.cols.forEach((col, i) => col.forEach((node, j) => {
    pos[node] = { x: 46 + i * CW + BW / 2, y: midY + (j - (col.length - 1) / 2) * RH };
  }));
  const id = "g" + g.version.replace(/[^a-z0-9]/gi, "");
  const seg = (a, b) => {
    const mx = (a.x + b.x) / 2;
    return `M${a.x + BW / 2} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x - BW / 2 - 6} ${b.y}`;
  };
  const forward = [], dashed = [];
  Object.entries(g.edges || {}).forEach(([from, tos]) => tos.forEach((to) => {
    if (pos[from] && pos[to] && !L.backSet.has(from + ">" + to)) forward.push(seg(pos[from], pos[to]));
  }));
  Object.entries(g.conditional_edges || {}).forEach(([from, tos]) => tos.forEach((to) => {
    if (pos[from] && pos[to] && !L.backSet.has(from + ">" + to)) dashed.push(seg(pos[from], pos[to]));
  }));
  const backs = L.back.map(([from, to]) => {
    const a = pos[from], b = pos[to];
    if (!a || !b) return "";
    const dip = midY + (L.rows * RH) / 2 + 18;
    return `M${a.x} ${a.y + BH / 2} C ${a.x} ${dip}, ${b.x} ${dip}, ${b.x} ${b.y + BH / 2 + 6}`;
  }).filter(Boolean);
  const endX = 46 + L.cols.length * CW + 12;

  return `<div class="card" style="margin-bottom:16px">
    <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
      <h3 style="margin:0">${esc(g.label)}</h3>
      <span class="chip core">${esc(g.version)}</span>
      <span class="chip">${g.nodes.length} 节点</span>
      <span class="mono faint" style="margin-left:auto;font-size:12px">${esc(g.module)}</span>
    </div>
    <p class="muted" style="font-size:13px;margin:6px 0 0">${esc(g.doc)}</p>
    <div class="scroll"><svg viewBox="0 0 ${W} ${H}" style="min-width:${W}px;max-width:100%;height:auto;margin-top:6px">
      <defs><marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto">
        <path d="M0 0 L10 5 L0 10 z" fill="currentColor"/></marker></defs>
      <g stroke="currentColor" fill="none" opacity="0.4" marker-end="url(#${id})">
        ${forward.map((d) => `<path d="${d}"/>`).join("")}
        ${dashed.map((d) => `<path d="${d}" stroke-dasharray="5 4"/>`).join("")}
      </g>
      <g stroke="var(--aw-warning)" fill="none" opacity="0.85" marker-end="url(#${id})" stroke-dasharray="5 4">
        ${backs.map((d) => `<path d="${d}"/>`).join("")}
      </g>
      ${L.back.length ? `<text x="${(pos[L.back[0][0]]?.x + pos[L.back[0][1]]?.x) / 2}" y="${H - 14}"
        text-anchor="middle" font-size="11" fill="var(--aw-warning)">revise 回边 · 与另一张图共用同一份改稿额度</text>` : ""}
      ${Object.entries(pos).map(([name, pt]) => {
        const cond = (g.conditional || []).includes(name);
        const entry = name === g.entry, term = name === g.terminal;
        const fill = entry ? "var(--aw-accent-soft)" : cond ? "var(--aw-warning-soft)" : term ? "var(--aw-success-soft)" : "var(--aw-panel)";
        const stroke = entry ? "var(--aw-accent)" : cond ? "var(--aw-warning)" : term ? "var(--aw-success)" : "var(--aw-border-strong)";
        return `<g><rect x="${pt.x - BW / 2}" y="${pt.y - BH / 2}" width="${BW}" height="${BH}" rx="8" fill="${fill}" stroke="${stroke}"/>
          <text x="${pt.x}" y="${pt.y + 4}" text-anchor="middle" font-family="var(--aw-mono)" font-size="11" fill="var(--aw-text)">${esc(name)}</text></g>`;
      }).join("")}
      ${L.hasEnd ? `<g><circle cx="${endX}" cy="${midY}" r="16" fill="none" stroke="var(--aw-border-strong)" stroke-dasharray="3 3"/>
        <text x="${endX}" y="${midY + 4}" text-anchor="middle" font-size="10" fill="var(--aw-text-muted)">END</text></g>` : ""}
    </svg></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      <span class="chip core">入口 ${esc(g.entry)}</span>
      <span class="chip on">终点 ${esc(g.terminal)}</span>
      <span class="chip warn">条件节点（后继由状态决定，且不跑 agent）：${esc((g.conditional || []).join(" · "))}</span>
      ${(g.branches || []).length ? `<span class="chip">固定 fan-out：${esc(g.branches.join(" ∥ "))}，在 synthesize 处以排序并集 fan-in</span>` : ""}
    </div>
  </div>`;
}

/* Layering from the edges themselves. The edge tables carry no coordinates and
   never should -- a layout is a fact about a picture -- so the columns are
   derived: a depth-first walk from the entry node classifies every edge as
   forward or as one that closes a cycle, and only the forward ones set depth.
   Without that split the `revise` back-edge would push its own target one
   column further right on every pass and the walk would not settle. */
function layoutGraph(g) {
  const all = {};
  const push = (from, to) => { (all[from] = all[from] || []).push(to); };
  Object.entries(g.edges || {}).forEach(([f, ts]) => ts.forEach((t) => push(f, t)));
  Object.entries(g.conditional_edges || {}).forEach(([f, ts]) => ts.forEach((t) => push(f, t)));
  const hasEnd = Object.values(g.conditional_edges || {}).some((ts) => ts.includes("END"));

  const forward = {}, back = [], backSet = new Set(), seen = new Set(), stack = new Set();
  (function dfs(u) {
    seen.add(u); stack.add(u);
    (all[u] || []).forEach((v) => {
      if (v === "END") return;
      if (stack.has(v)) { back.push([u, v]); backSet.add(u + ">" + v); return; }
      (forward[u] = forward[u] || []).push(v);
      if (!seen.has(v)) dfs(v);
    });
    stack.delete(u);
  })(g.entry);

  const depth = {};
  (g.nodes || []).forEach((nn) => { depth[nn] = 0; });
  depth[g.entry] = 0;
  for (let pass = 0; pass < (g.nodes || []).length + 1; pass++) {
    Object.entries(forward).forEach(([f, ts]) => ts.forEach((t) => {
      depth[t] = Math.max(depth[t] || 0, (depth[f] || 0) + 1);
    }));
  }
  if (g.terminal in depth) depth[g.terminal] = Math.max(...Object.values(depth));
  const cols = [];
  Object.entries(depth).forEach(([nn, d]) => { (cols[d] = cols[d] || []).push(nn); });
  const filled = cols.filter(Boolean);
  return { cols: filled, rows: Math.max(...filled.map((c) => c.length)), back, backSet, hasEnd };
}

function moduleTable(mods) {
  return `<div class="modlist">` + mods.map((m) => `
    <details class="mod" data-hay="${esc((m.path + " " + m.doc + " " + m.symbols.map((s) => s.name).join(" ")).toLowerCase())}">
      <summary>
        <span class="path">${esc(m.path.replace("src/agent_workbench/", ""))}</span>
        <span class="doc">${esc(m.doc)}</span>
        <span class="loc">${n(m.loc)}</span>
      </summary>
      <div class="symbols">${m.symbols.length ? m.symbols.map((s) => `
        <div><span class="s">${esc(s.name)}</span>${s.kind === "class" ? "" : "()"} <span class="faint">${esc(s.doc)}</span></div>`).join("")
        : `<div class="faint">没有导出的顶层符号。</div>`}</div>
    </details>`).join("") + `</div>`;
}

add("modules", "模块浏览器", "细节", () => {
  return `
    <div class="eyebrow">Every module</div>
    <h2>模块浏览器</h2>
    <p class="lede">每行右边那句话是模块自己 docstring 的第一行，不是另写的注解——所以它由改这个模块的人维护，而不是由读它的人。展开看顶层符号。</p>
    <div class="searchbar">
      <input id="modq" type="search" placeholder="搜路径、摘要或符号名，例如 lease / fusion / envelope" />
      <span class="faint mono" id="modcount"></span>
    </div>
    <div id="modfilters" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <button class="seg on" data-pkg="">全部</button>
      ${DATA.packages.map((p) => `<button class="seg" data-pkg="${esc(p.name)}">${esc(p.name)}</button>`).join("")}
    </div>
    ${DATA.packages.map((p) => `
      <div class="pkgblock" data-pkg="${esc(p.name)}">
        <div class="pkg-head">
          <h3>${esc(p.path)}</h3>
          <span class="chip ${DATA.layers.find((L) => L.id === p.layer)?.kind || ""}">${esc(p.layer)}</span>
          <span class="faint mono" style="margin-left:auto">${n(p.files)} 文件 · ${n(p.loc)} 行</span>
        </div>
        ${p.groups.map((gr) => `
          ${gr.name !== "." ? `<div class="faint mono" style="margin:14px 0 2px;font-size:12px">${esc(gr.name)}/ — ${n(gr.files)} 文件 · ${n(gr.loc)} 行</div>` : ""}
          ${moduleTable(gr.modules)}`).join("")}
      </div>`).join("")}`;
});

add("api", "HTTP 接口", "细节", () => {
  const groups = {};
  DATA.endpoints.forEach((e) => { (groups[e.group] = groups[e.group] || []).push(e); });
  return `
    <div class="eyebrow">Control plane</div>
    <h2>${n(DATA.endpoints.length)} 个端点</h2>
    <p class="lede">从路由装饰器读出来的，不是从文档抄的。前缀是同一个文件里的模块常量，所以改名是一处编辑。</p>
    <div class="note warn"><b>身份边界。</b>当前 Identity Adapter 只信任请求头，<span class="mono">agent-api</span> 只能用于受控的本机开发（ADR-044）。监听地址限制在 loopback，但那是防止意外暴露的机制，不是身份认证。</div>
    ${Object.entries(groups).map(([g, list]) => `
      <h3 class="mono">${esc(g)} <span class="faint">· ${list.length}</span></h3>
      <div class="scroll"><table><tbody>
        ${list.map((e) => `<tr>
          <td style="width:64px"><span class="chip ${e.method === "GET" ? "" : e.method === "DELETE" ? "deny" : "core"}">${esc(e.method)}</span></td>
          <td class="mono" style="width:34%">${esc(e.path)}</td>
          <td class="muted">${esc(e.doc || e.handler)}</td></tr>`).join("")}
      </tbody></table></div>`).join("")}`;
});

add("tools", "工具目录", "细节", () => {
  const inproc = DATA.tools.filter((t) => t.kind === "in-process");
  const mcp = DATA.tools.filter((t) => t.kind === "mcp");
  const demo = DATA.tools.filter((t) => t.kind === "demo");
  const table = (list) => `<div class="scroll"><table><thead><tr><th>名字</th><th>声明处</th><th>模块摘要</th></tr></thead><tbody>
    ${list.map((t) => `<tr><td class="mono">${esc(t.name)}</td>
      <td class="mono faint" style="font-size:12px">${esc(t.module.replace("src/agent_workbench/", ""))}</td>
      <td class="muted">${esc(t.doc)}</td></tr>`).join("")}</tbody></table></div>`;
  return `
    <div class="eyebrow">What an agent can reach</div>
    <h2>工具目录</h2>
    <p class="lede">工具名是一份线上契约：提交时冻进授权信封，之后写进每一条事件。所以它在源码里只声明一次，这张表读的就是那个常量。</p>
    <h3>进程内工具 <span class="faint">· ${inproc.length}</span></h3>${table(inproc)}
    <h3>MCP server 侧的工具 <span class="faint">· ${mcp.length}</span></h3>
    <p class="muted" style="font-size:13px">经 MCP 接入后，Agent 看到的名字带前缀：<span class="mono">mcp_{alias}_{tool}</span>。目录在<b>进程启动时冻结一次</b>——Worker 起来之后再启动的 server，那个 Worker 这辈子都看不到它。</p>
    ${table(mcp)}
    ${demo.length ? `<h3>演示用 <span class="faint">· ${demo.length}</span></h3>${table(demo)}` : ""}`;
});

add("config", "配置画像", "细节", () => {
  const s = DATA.settings;
  return `
    <div class="eyebrow">Configuration is a contract</div>
    <h2>${DATA.profiles.length} 个 profile，一份 schema</h2>
    <p class="lede">配置声称的能力与代码不符时，进程在<b>加载阶段</b>就起不来，而不是躺在那里没人读。</p>
    <div class="scroll"><table><thead><tr><th>profile</th><th>MCP servers</th><th>行数</th><th>文件</th></tr></thead><tbody>
      ${DATA.profiles.map((p) => `<tr><td class="mono">${esc(p.name)}</td>
        <td>${p.mcp_servers.length ? p.mcp_servers.map((m) => `<span class="chip on">${esc(m)}</span> `).join("") : `<span class="chip off">无</span>`}</td>
        <td class="mono faint">${n(p.loc)}</td>
        <td class="mono faint" style="font-size:12px">${esc(p.path)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="note">profile 是<b>分开的文件而不是一个开关</b>：每一份都会把自己的工具名冻进每一个新提交的 Task 授权信封，所以一个更宽的 profile 会加宽这个部署上的每一个 Task。</div>
    <h3>写成单值 <span class="mono">Literal</span> 的不变量 <span class="faint">· ${s.single_valued_literals.length} 条</span></h3>
    <p class="muted" style="font-size:13px">这些字段在类型上只有一个合法值。改它们不是改一行配置，是先写一份 ADR。读自 <span class="mono">${esc(s.path)}</span>。</p>
    <div style="max-height:260px;overflow:auto" class="card">
      ${s.single_valued_literals.map((L) => `<div style="font-size:12.5px"><span class="mono">${esc(L.field)}</span> <span class="faint">=</span> <span class="mono" style="color:var(--aw-accent)">${esc(L.value)}</span></div>`).join("")}
    </div>`;
});

add("adrs", "决策记录", "细节", () => {
  return `
    <div class="eyebrow">One ADR per boundary change</div>
    <h2>${DATA.adrs.length} 份 ADR</h2>
    <p class="lede">凡是改动事实源、控制平面、循环归属、融合归属或恢复语义的，先有一份记录。编号连续，作废的会说清被谁取代。</p>
    <div class="searchbar"><input id="adrq" type="search" placeholder="搜标题，例如 lease / 委派 / 检查点" /><span class="faint mono" id="adrcount"></span></div>
    <div class="scroll"><table><tbody id="adrbody">
      ${DATA.adrs.map((a) => `<tr data-hay="${esc((a.id + " " + a.title).toLowerCase())}">
        <td class="mono" style="width:56px;color:var(--aw-accent)">${esc(a.id)}</td>
        <td>${esc(a.title)}${a.superseded ? ` <span class="chip warn">已被取代</span>` : ""}</td>
        <td class="mono faint" style="width:38%;font-size:12px">${esc(a.path)}</td></tr>`).join("")}
    </tbody></table></div>`;
});

add("gates", "门禁与规模", "细节", () => {
  const w = DATA.web;
  return `
    <div class="eyebrow">Evidence</div>
    <h2>门禁与规模</h2>
    <p class="lede">这一页的每个数字都是这次构建时数出来的。能力状态只按 Planned → Implemented → Tested → Demonstrated 升级，没有可链接的证据不得升级。</p>
    <h3>测试</h3>
    <div class="scroll"><table><thead><tr><th>目录</th><th>测试函数</th><th>文件</th><th>行</th></tr></thead><tbody>
      ${DATA.tests.map((t) => `<tr><td class="mono">tests/${esc(t.name)}</td><td class="mono">${n(t.tests)}</td>
        <td class="mono faint">${n(t.files)}</td><td class="mono faint">${n(t.loc)}</td></tr>`).join("")}
      <tr><td><b>合计</b></td><td class="mono"><b>${n(DATA.totals.tests)}</b></td>
        <td class="mono faint">${n(DATA.totals.test_files)}</td><td></td></tr>
    </tbody></table></div>
    <div class="note"><b>这一列数的是测试<i>函数</i>，不是 pytest 收集到的条目。</b>
      两个数字不一样，而且差得不少：<span class="mono">tests/contracts/conftest.py</span> 里有六个
      <span class="mono">params=[...]</span> 夹具把同一份契约跑在内存实现和 PostgreSQL 实现上，
      再加上各处的 <span class="mono">@pytest.mark.parametrize</span>——所以收集数明显大于函数数。
      要一次真实运行的通过数，看 <span class="mono">docs/HIGHLIGHTS.md §2</span>：那里记的是四个不同环境
      各自跑出来的结果，并且写明了它们可以分开引用、但不许相加。</div>
    <div class="note">五个目录（<span class="mono">contracts persistence api vector e2e</span>）在没有 <span class="mono">AGENT_WORKBENCH_TEST_DSN</span> 与 <span class="mono">AGENT_WORKBENCH_TEST_QDRANT_URL</span> 指向真实服务时会自己跳过。PostgreSQL 夹具拒绝任何名字不以 <span class="mono">_test</span> 结尾的库。</div>
    <h3>控制台</h3>
    <div class="scroll"><table><thead><tr><th>feature</th><th>源文件</th><th>测试文件</th><th>行</th></tr></thead><tbody>
      ${w.features.map((f) => `<tr><td class="mono">web/src/features/${esc(f.name)}</td><td class="mono">${n(f.files)}</td>
        <td class="mono faint">${n(f.tests)}</td><td class="mono faint">${n(f.loc)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="note">前端唯一出网的地方是这 ${w.network_files.length} 个文件：${w.network_files.map((f) => `<span class="mono">${esc(f.replace("web/src/", ""))}</span>`).join("、")}。别处出现 <span class="mono">fetch(</span> 就是这条边界破了。</div>
    <h3>进程入口</h3>
    <div class="scroll"><table><tbody>
      ${DATA.entrypoints.map((e) => `<tr><td class="mono" style="width:220px">${esc(e.name)}</td><td class="mono faint">${esc(e.target)}</td></tr>`).join("")}
    </tbody></table></div>`;
});

/* ------------------------------------------------------------------ shell */

const rail = document.getElementById("rail"), main = document.getElementById("main");
let lastGroup = "";
rail.appendChild(el(`<div class="brand"><b>Agent Workbench</b><span>架构面板${DATA.commit ? " · " + DATA.commit : ""}</span></div>`));
SECTIONS.forEach((s) => {
  if (s.group !== lastGroup) { rail.appendChild(el(`<div class="group">${esc(s.group)}</div>`)); lastGroup = s.group; }
  rail.appendChild(el(`<a class="nav" href="#${s.id}" data-id="${s.id}"><span class="dot"></span>${esc(s.label)}</a>`));
});
rail.appendChild(el(`<div class="foot">离线页面，数据在构建时从工作树读出。<br/>本页只监听 127.0.0.1。</div>`));
SECTIONS.forEach((s) => {
  const node = document.createElement("section");
  node.id = s.id;
  node.innerHTML = s.render();
  main.appendChild(node);
});

/* Scroll spy. IntersectionObserver rather than a scroll handler: the sections
   are page-length, and a handler that recomputed offsets on every frame was
   the one thing on this page capable of being slow. */
const links = new Map([...rail.querySelectorAll("a.nav")].map((a) => [a.dataset.id, a]));
const io = new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    links.forEach((a) => a.classList.remove("active"));
    links.get(e.target.id)?.classList.add("active");
  });
}, { rootMargin: "-10% 0px -80% 0px" });
SECTIONS.forEach((s) => io.observe(document.getElementById(s.id)));

/* Module search: filter, do not re-render. Re-rendering would close every
   <details> the reader had opened, which is the state they were searching to
   build in the first place. */
const modq = document.getElementById("modq");
const blocks = [...document.querySelectorAll(".pkgblock")];
let pkgFilter = "";
function applyModFilter() {
  const q = (modq.value || "").trim().toLowerCase();
  let shown = 0;
  blocks.forEach((b) => {
    let any = false;
    b.querySelectorAll("details.mod").forEach((d) => {
      const hit = (!q || d.dataset.hay.includes(q)) && (!pkgFilter || b.dataset.pkg === pkgFilter);
      d.style.display = hit ? "" : "none";
      if (hit) { any = true; shown++; if (q) d.open = true; }
      if (!q) d.open = false;
    });
    b.style.display = any ? "" : "none";
  });
  document.getElementById("modcount").textContent = shown + " 个模块";
}
modq.addEventListener("input", applyModFilter);
document.querySelectorAll("#modfilters .seg").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll("#modfilters .seg").forEach((b) => b.classList.remove("on"));
  btn.classList.add("on"); pkgFilter = btn.dataset.pkg; applyModFilter();
}));
applyModFilter();

const adrq = document.getElementById("adrq");
adrq.addEventListener("input", () => {
  const q = adrq.value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll("#adrbody tr").forEach((tr) => {
    const hit = !q || tr.dataset.hay.includes(q);
    tr.style.display = hit ? "" : "none"; if (hit) shown++;
  });
  document.getElementById("adrcount").textContent = shown + " 份";
});
adrq.dispatchEvent(new Event("input"));

/* Three states, not two: following the system is the default and the only one
   that holds with JavaScript disabled, so the button cycles through it rather
   than toggling between the two pinned ones. */
const themeBtn = document.getElementById("theme");
const THEMES = ["", "light", "dark"], LABELS = { "": "主题 · 跟随系统", light: "主题 · 浅色", dark: "主题 · 深色" };
let ti = 0;
try { ti = Math.max(0, THEMES.indexOf(localStorage.getItem("aw-panel-theme") || "")); } catch (e) { ti = 0; }
function applyTheme() {
  const t = THEMES[ti];
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  themeBtn.textContent = LABELS[t];
  try { localStorage.setItem("aw-panel-theme", t); } catch (e) { /* private window */ }
}
themeBtn.addEventListener("click", () => { ti = (ti + 1) % THEMES.length; applyTheme(); });
applyTheme();
</script>
</body>
</html>
"""


def render(data: dict[str, Any], diagrams: dict[str, str]) -> str:
    """Substitute, do not format.

    ``str.format`` and f-strings are both wrong here: the template is CSS and
    JavaScript, and both of those are full of braces. Two literal placeholders
    keep the template readable as the languages it actually is.
    """
    return PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace(
        "__DIAGRAMS__", json.dumps(diagrams, ensure_ascii=False)
    )


# --------------------------------------------------------------------------
# 4. Command line
# --------------------------------------------------------------------------


class _LoopbackServer(HTTPServer):
    """The panel's server, differing from the default in one flag.

    On POSIX ``SO_REUSEADDR`` only waives the TIME_WAIT delay, which is what
    makes "Ctrl-C, edit, run again" work without an "address already in use".
    On Windows the same flag means something else: a second process may bind a
    port the first is still listening on, and the two then split incoming
    requests unpredictably -- so a second ``panel`` would appear to work while
    half the page came from the older build. There the honest answer is to fail
    to bind and say so.
    """

    allow_reuse_address = sys.platform != "win32"


def serve(directory: Path, port: int, open_browser: bool) -> int:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            sys.stderr.write("panel: " + fmt % args + "\n")

    # 127.0.0.1 rather than "" or 0.0.0.0, and this is the whole reason the
    # server is written here instead of being `python -m http.server`: that
    # module's default binds every interface, and what this directory contains
    # is the source tree's docstrings.
    # The docstring above says the honest answer on Windows is to fail to bind
    # and say so. Failing was implemented; saying so was not, and an unhandled
    # OSError is a traceback rather than a sentence -- in a window that, when
    # this was double-clicked from Explorer, closes on top of it.
    try:
        httpd = _LoopbackServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"panel: 127.0.0.1:{port} 绑不上（{exc}）。", file=sys.stderr)
        print(f"       换个端口再试：--port {port + 1}", file=sys.stderr)
        # WinError 10013 is not "in use" -- it is a policy refusal (a reserved
        # range, or a firewall), and telling someone to close the other panel
        # would send them looking for a process that does not exist.
        if getattr(exc, "winerror", None) == 10013:
            print(
                "       （10013：这个端口被系统保留或被策略挡下，不是被别的进程占着）",
                file=sys.stderr,
            )
        return 1
    url = f"http://127.0.0.1:{port}/"
    # ASCII on purpose, and first: on a console that cannot render the line
    # below, this one still tells a reader where the panel is.
    print(f"panel: {url}   (Ctrl-C to stop)", file=sys.stderr)
    print(
        "架构面板已启动。浏览器没自动打开的话，手动访问上面那个地址。", file=sys.stderr
    )
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # First statement, before the parser exists: every `help=` below is Chinese,
    # and argparse writes them from inside parse_args -- so a `--help` on a
    # cp1252 console printed through an unreconfigured stdout, which is the one
    # invocation a new reader is most likely to try first.
    _speak_utf8()
    parser = argparse.ArgumentParser(
        description="Build and serve the local architecture panel."
    )
    parser.add_argument(
        "--build",
        metavar="DIR",
        nargs="?",
        const=str(DEFAULT_OUT),
        help="写出 index.html 后退出",
    )
    parser.add_argument(
        "--serve", action="store_true", help="构建并在 127.0.0.1 上提供服务"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    parser.add_argument(
        "--json", action="store_true", help="只把扫描出的数据打到 stdout"
    )
    parser.add_argument(
        "--check", action="store_true", help="校验 NARRATIVE 里点名的路径与符号仍然存在"
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check_narrative()
        for line in problems:
            print(f"panel: {line}", file=sys.stderr)
        if problems:
            print(f"panel: {len(problems)} 处描述与工作树不符", file=sys.stderr)
            return 1
        print("panel: NARRATIVE 里点名的每一处都还在", file=sys.stderr)
        return 0

    data = build_data()
    if args.json:
        try:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        except BrokenPipeError:
            # `--json | head` is the obvious way to look at this, and a
            # traceback is not what that should print. The data was produced;
            # the reader simply stopped reading.
            return 0
        return 0

    out_dir = Path(args.build) if args.build else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "index.html"
    # newline="\n" so the page is byte-identical on every platform: the default
    # translates "\n" to "\r\n" on Windows, which would make the same tree
    # produce two different files and the size printed below disagree with the
    # one a colleague sees.
    target.write_text(render(data, load_diagrams()), encoding="utf-8", newline="\n")
    size_kb = target.stat().st_size / 1024
    # `--build` accepts any directory, including one outside the checkout, so the
    # pretty relative path is a best effort rather than an assumption. It was an
    # assumption until a build into /tmp crashed on `relative_to` *after* writing
    # the file -- the work had succeeded and the report is what failed.
    try:
        shown: Path | str = target.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = target
    print(f"panel: {shown} ({size_kb:.0f} KB)", file=sys.stderr)

    if args.serve:
        return serve(out_dir, args.port, not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

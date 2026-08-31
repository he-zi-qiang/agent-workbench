"""A configuration leaf that no code reads is a promise nothing keeps.

Twice now this repository has closed a gap of exactly this shape -- A-07 and
F-26 -- by wiring one field and moving on. Both times the finding came from a
person reading the settings module by hand, and both times the mechanism that
let the field sit unread survived the fix. A third full-repository scan found
about thirty more.

So this file is not another wiring; it is the thing that was missing under the
two wirings. Every leaf of ``Settings`` must be named somewhere in ``src/`` or
``scripts/`` outside the settings module itself, unless it falls into one of
three explicitly-argued exemptions:

1. **A single-valued ``Literal``.** The field *is* the assertion -- it exists so
   that a configuration claiming otherwise fails at load. ``fusion_owner`` and
   ``runtime_loop_owner`` are the shape: ``docs/configuration.md`` §3 calls
   these the frozen architectural invariants, and a runtime reader for one
   would be a second place deciding something that cannot vary.
2. **Lifecycle ``test_only`` or ``lab``** in ``config/ownership.yaml``. The
   first is consumed by tests, which are not searched here; the second is a
   capability a validator pins off because it does not exist.
3. **Named in ``KNOWN_UNREAD_LEAVES``**, with a reason. This is the honest
   list, and it is the point of the file: each entry says either which gap
   tracks it or why no reader is the right answer.

The grep is deliberately crude -- the field's own name, as a whole word,
anywhere in the two trees. A crude search makes false *passes* possible (a
variable that happens to share the name) and false failures nearly impossible,
which is the right way round for a gate that must not cry wolf. Something
narrower would have to model attribute access through projections, and the
projection layer is precisely where these fields go to disappear.

**One false pass is known and worth naming, because it is the exact shape the
crudeness costs.** ``rag.retrieval.answer_context_k`` passes: it is projected
into ``RetrievalConfig`` and therefore appears in ``projections.py``. Nothing
then reads that field of the projection -- ADR-097 §4.3 says so and leaves A-07
open for it. A gate that caught this one would have to follow a value from the
settings object, through a frozen dataclass, into whoever unpacks it; that is a
different tool, and building it here would trade a gate that runs in two
seconds for one nobody keeps green.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, get_args, get_origin

from pydantic import BaseModel

from agent_workbench.bootstrap.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNERSHIP_FILE = PROJECT_ROOT / "config" / "ownership.yaml"
SEARCH_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts")

#: Excluded from the search because it *declares* every one of these names.
#: Counting it as a reader would make the whole gate vacuous.
DECLARING_MODULE = "settings.py"

#: Every leaf with no reader today, and why. Shrinking this table is the work;
#: growing it requires writing down which gap the new entry belongs to.
#:
#: Keep the reasons specific. "not used yet" is what the ownership manifest
#: said for months, and it is what let these accumulate.
KNOWN_UNREAD_LEAVES: dict[str, str] = {
    # --- 已知缺口 A-08「配置叶子零读者」 --------------------------------------
    "artifact_store.presigned_url_ttl_seconds": (
        "A-08。只有 local 后端被实现，预签名 URL 没有发行方"
    ),
    "coordination.claim_batch_size": "A-08。认领循环一次取一条，批量认领未实现",
    "coordination.claim_poll_jitter_ms": "A-08。轮询无抖动，单 Worker 下无惊群",
    "coordination.lease_grace_seconds": "A-08。到期判定只用 lease_expires_at",
    "coordination.max_missed_heartbeats": "A-08。同上，心跳丢失次数无人计",
    "coordination.priority_aging_seconds": "A-08。认领不排优先级，先到先得",
    "coordination.recovery_poll_seconds": (
        "A-08。回收循环用的是 claim_poll_interval_ms（见 B-08）"
    ),
    "database.guard_pool_mode": "A-08。三个池都直连，pool_mode 无分支",
    "database.listen_pool_mode": "A-08。同上",
    "database.query_pool_mode": "A-08。同上",
    "database.operational_connection_reserve": (
        "A-08。连接预留没有执行者；guard_connection_budget 是实际生效的那个"
    ),
    "event_stream.notify_payload_limit_bytes": (
        "A-08。NOTIFY 载荷只发标识符，长度由形状保证而非由这个数检查"
    ),
    "event_stream.stream_ready_channel": ("A-08。只有 task_ready_channel 有订阅者"),
    "runtime.cancellation_poll_seconds": (
        "A-08。取消是 CancellationToken 的事件驱动检查，不轮询（ADR-0085）"
    ),
    "qdrant.prefer_grpc": (
        "A-08。三处 AsyncQdrantClient 构造只传 url/api_key/timeout，"
        "而 url 是 REST 口 6333（gRPC 在 6334）—— 这个 true 即便被读也不成立"
    ),
    "qdrant.api_key_required": (
        "A-08。密钥有没有由 secrets.qdrant_api_key 是否为 None 决定"
    ),
    "rag.embedding.dense_vector_name": (
        "A-08。实际用的是 ports/vector_index.py 的模块常量 DENSE_VECTOR_NAME；"
        "改 TOML 不会改变建集合、写入或查询的任何一处"
    ),
    "rag.embedding.sparse_vector_name": "A-08。同上，SPARSE_VECTOR_NAME",
    "rag.embedding.max_input_tokens": (
        "A-08。截断由 BGE-M3 tokenizer 自己的 max_length 决定"
    ),
    "rag.graph.entity_arm_limit": "A-08。见「图谱检索臂从未被服务进程装配」",
    "rag.graph.relation_arm_limit": "A-08。同上",
    "rag.ingestion.upsert_batch_size": "A-08。写入按 embedding_batch_size 分批",
    "rag.ingestion.parser_version": "A-08。解析器版本没有进 chunk 的元数据",
    "rag.ingestion.chunker_version": "A-08。同上",
    "rag.ingestion.index_schema_version": (
        "A-08。集合的 schema 版本由 qdrant.collection_schema_version 决定"
    ),
    "workflow.human_interrupt_enabled": (
        "A-08。审批中断由图自己的 interrupt() 无条件实现，关不掉"
    ),
    # --- 评测侧，见「评测侧三处 · Task benchmark 整条链不存在」 ----------------
    "evaluation.rag_metrics": "runner 直接遍历代码里的 RETRIEVAL_METRICS",
    "evaluation.task_metrics": "Task benchmark 整条链不存在（无目录、无 runner）",
    "evaluation.multi_agent_metrics": "同上，连 validator 都没有",
    "evaluation.task_benchmark_path": "同上。指向不存在的 ./evals/tasks/cases.yaml",
    "evaluation.rag_gold_set_path": (
        "scripts/run_rag_eval.py 用自己的 --gold 参数，不读配置"
    ),
    "evaluation.ragas_enabled": (
        "A-04。ragas 未进 pyproject；由 field_validator 钉死为 False"
    ),
    "evaluation.judge.calibration_set_path": (
        "A-04。指向不存在的 judge-calibration.jsonl"
    ),
    "evaluation.judge.model_revision": "A-04。judge 未实现，无处记录它的 revision",
    # --- 未装配的能力所需的凭据 ----------------------------------------------
    "secrets.artifact_access_key": "S3 后端未实现；只有 local 一条 artifact 路径",
    "secrets.artifact_secret_key": "同上",
    "secrets.langfuse_public_key": (
        "Langfuse 未接（observability.langfuse_enabled 恒 false）"
    ),
    "secrets.langfuse_secret_key": "同上",
    "secrets.otel_exporter_headers": (
        "导出器只按 endpoint 构造，自定义 header 没有传下去的通路"
    ),
    "observability.langfuse_enabled": "同上；由 field_validator 钉死为 False",
    # --- 供应商没有这个能力 ---------------------------------------------------
    "model.main.prompt_cache_enabled": (
        "DeepSeek 的上下文硬盘缓存自动生效、不接受请求级开关；"
        "缓存命中数照常从 usage 里读出来并计价"
    ),
    "model.compact.prompt_cache_enabled": "同上",
}


def _nested_model_type(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        nested = _nested_model_type(argument)
        if nested is not None:
            return nested
    return None


def _leaves(
    model_type: type[BaseModel] = Settings, prefix: str = ""
) -> dict[str, object]:
    leaves: dict[str, object] = {}
    for field_name, field_info in model_type.model_fields.items():
        path = f"{prefix}.{field_name}" if prefix else field_name
        nested = _nested_model_type(field_info.annotation)
        if nested is None:
            leaves[path] = field_info.annotation
        else:
            leaves.update(_leaves(nested, path))
    return leaves


def _is_frozen_invariant(annotation: object) -> bool:
    return get_origin(annotation) is Literal and len(get_args(annotation)) == 1


def _lifecycles() -> dict[str, str]:
    manifest = json.loads(OWNERSHIP_FILE.read_text(encoding="utf-8"))
    return {
        field: group["lifecycle"]
        for group in manifest["groups"]
        for field in group["fields"]
    }


def _sources() -> list[str]:
    return [
        path.read_text(encoding="utf-8")
        for root in SEARCH_ROOTS
        for path in root.rglob("*.py")
        if path.name != DECLARING_MODULE
    ]


def _unread_leaves() -> set[str]:
    lifecycles = _lifecycles()
    sources = _sources()
    unread: set[str] = set()
    for path, annotation in _leaves().items():
        if _is_frozen_invariant(annotation):
            continue
        if lifecycles.get(path) in {"test_only", "lab"}:
            continue
        name = path.rsplit(".", 1)[-1]
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if not any(pattern.search(source) for source in sources):
            unread.add(path)
    return unread


def test_every_configuration_leaf_has_a_reader_or_a_written_reason() -> None:
    undeclared = sorted(_unread_leaves() - set(KNOWN_UNREAD_LEAVES))

    assert not undeclared, (
        "these settings fields are validated, documented and owned, and no "
        "code in src/ or scripts/ reads them: "
        + ", ".join(undeclared)
        + ". Wire them, delete them, or add them to KNOWN_UNREAD_LEAVES with "
        "the gap number or the argument for why no reader is correct."
    )


def test_the_exemption_table_has_no_stale_entries() -> None:
    """An entry that acquired a reader must leave the table.

    Without this the table only ever grows, and a growing list of excuses is
    indistinguishable from the manifest this file exists to stop trusting.
    """

    unread = _unread_leaves()
    stale = sorted(name for name in KNOWN_UNREAD_LEAVES if name not in unread)

    assert not stale, (
        "these fields now have a reader and no longer need an exemption: "
        + ", ".join(stale)
    )


def test_every_exempted_field_is_a_real_settings_leaf() -> None:
    leaves = set(_leaves())
    unknown = sorted(name for name in KNOWN_UNREAD_LEAVES if name not in leaves)

    assert not unknown, f"KNOWN_UNREAD_LEAVES names fields that do not exist: {unknown}"


def test_every_exemption_carries_a_reason() -> None:
    empty = sorted(name for name, reason in KNOWN_UNREAD_LEAVES.items() if not reason)

    assert not empty, f"exemptions without a reason: {empty}"

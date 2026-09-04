"""Put the retrieval weights in the cache before anything tries to use them.

A one-shot, run to completion before the API and the Workers start, for a
reason that is not "it is faster this way": **the sparse arm refuses to start
on a cold cache rather than filling it.**
``adapters/embedding/bge_sparse.py`` checks for ``sparse_linear.pt`` with
``try_to_load_from_cache`` *before* it constructs the model, and raises when it
is absent -- because FlagEmbedding's own behaviour there is to build a fresh
random ``Linear`` and carry on, which produces vectors of the right width that
mean nothing. So a container that merely started with an empty cache would not
download BGE-M3; it would exit. Since ADR-0106 that container is the encoder,
the one process that loads the weights; before it, all four retrieving
processes exited in the same way at the same moment.

Reading the ids and revisions from the loaded settings rather than naming
``BAAI/bge-m3`` here is the other half. The identity of an index is the model
id and the revision together (ADR-013); a prefetch that hardcoded either one
would keep working after somebody changed the configuration, and would warm the
cache for a model this deployment has stopped using -- which looks exactly like
a warm cache until retrieval fails.

Idempotent by construction: ``snapshot_download`` and ``hf_hub_download`` are
cache reads when the files are already there, so a restart costs a few HTTP
HEADs rather than several gigabytes.
"""

from __future__ import annotations

import os
import sys

from agent_workbench.bootstrap.settings import load_settings

# Compose has no syntax for "omit this variable", so an unset `HF_ENDPOINT` on
# the host arrives here as the empty string -- and huggingface_hub reads an
# empty endpoint as an endpoint, producing request URLs with nothing in front
# of the path. Dropped before the library is imported, so "nobody chose a
# mirror" and "there is no mirror" are the same thing to it.
if not os.environ.get("HF_ENDPOINT"):
    os.environ.pop("HF_ENDPOINT", None)

#: BGE-M3 keeps its trained lexical head beside the base weights rather than
#: inside them, and `snapshot_download` of the repository does bring it -- but
#: it is named explicitly afterwards so that a partial or interrupted snapshot
#: fails *here*, with the file named, instead of at the Worker's first start.
SPARSE_HEAD = "sparse_linear.pt"


def _endpoint() -> str:
    """Where the weights are coming from, said out loud.

    ``HF_ENDPOINT`` is huggingface_hub's own variable; this only reports it.
    The default is upstream: pointing every deployment's model weights at a
    third-party mirror is a supply-chain decision, not a convenience, and it
    belongs to whoever runs the stack. `docs/windows-quickstart.md` names the
    mirror that makes this finish on a mainland-China connection, as one line
    somebody types on purpose.
    """

    return os.environ.get("HF_ENDPOINT") or "https://huggingface.co"


def main() -> int:
    try:
        from huggingface_hub import (  # pyright: ignore[reportMissingImports]
            hf_hub_download,
            snapshot_download,
        )
    except ImportError:
        # The image is built with `--extra embedding`, so this is not a
        # configuration a caller can reach by accident -- it means the image
        # was built without it, and every retrieval capability is absent.
        # Saying so here is better than the encoder discovering it at load.
        print(
            "fetch-weights: no huggingface_hub, so this image has no embedding "
            "extra and this stack has no retrieval. Rebuild the image.",
            file=sys.stderr,
        )
        return 2

    settings = load_settings()
    embedding = settings.rag.embedding
    reranker = settings.rag.reranker

    print(f"fetch-weights: endpoint {_endpoint()}", file=sys.stderr)

    plan = (
        (embedding.model_id, embedding.revision, True),
        (reranker.model_id, reranker.revision, False),
    )
    for model_id, revision, needs_sparse_head in plan:
        print(f"fetch-weights: {model_id}@{revision}", file=sys.stderr)
        try:
            snapshot_download(model_id, revision=revision)
            if needs_sparse_head:
                hf_hub_download(model_id, SPARSE_HEAD, revision=revision)
        except Exception as error:
            # Named remedies rather than a traceback. Every realistic failure
            # here is one of three, and none of them is a bug in this project:
            # no route to the endpoint, a revision that does not exist, or a
            # disk that filled up mid-download.
            print(
                f"fetch-weights: {model_id}@{revision} did not arrive from "
                f"{_endpoint()} ({type(error).__name__}: {error}).\n"
                "  If this is a network failure, the usual fix on a mainland-"
                "China connection is a mirror:\n"
                "    HF_ENDPOINT=https://hf-mirror.com\n"
                "  See docs/windows-quickstart.md.",
                file=sys.stderr,
            )
            return 1

    print("fetch-weights: cache is warm", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - container entry point
    raise SystemExit(main())

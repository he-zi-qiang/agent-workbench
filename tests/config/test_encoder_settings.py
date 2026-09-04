"""Two leaves that move the weights out of a process (ADR-0106).

`rag.embedding.service_url` and `rag.reranker.service_url` are defaulted
leaves under sections every profile already has, so a configuration written
before they existed loads unchanged and means what it meant -- the weights
load here. That is the whole reason they are leaves rather than a
`[rag.encoder]` table, and the first test pins it.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent_workbench.apps.encoder.main import loads_in_process
from agent_workbench.bootstrap.projections import (
    project_api,
    project_encoder_service,
    project_ingestion_worker,
    project_task_worker,
)
from agent_workbench.bootstrap.settings import Settings
from tests.config.test_settings import valid_payload


def _payload(embedding: str = "", reranker: str = "") -> dict:
    payload = deepcopy(valid_payload())
    if embedding:
        payload["rag"]["embedding"]["service_url"] = embedding
    if reranker:
        payload["rag"]["reranker"]["service_url"] = reranker
    return payload


def test_a_configuration_written_before_the_leaves_loads_and_loads_locally() -> None:
    settings = Settings(**valid_payload())

    assert settings.rag.embedding.service_url == ""
    assert settings.rag.reranker.service_url == ""
    # No schema bump for a defaulted leaf (docs/configuration.md §2).
    assert settings.app.config_schema_version == "1.19"


def test_the_url_reaches_every_process_that_would_otherwise_load_weights() -> None:
    settings = Settings(**_payload("http://encoder:8769", "http://encoder:8769"))

    api = project_api(settings)
    worker = project_task_worker(settings)
    ingestion = project_ingestion_worker(settings)
    assert api.embedding.service_url == "http://encoder:8769"
    assert api.reranker.service_url == "http://encoder:8769"
    assert worker.embedding is not None
    assert worker.embedding.service_url == "http://encoder:8769"
    assert ingestion.embedding.service_url == "http://encoder:8769"


def test_the_encoder_projects_the_same_two_sections_and_the_pool() -> None:
    settings = Settings(**valid_payload())

    encoder = project_encoder_service(settings)

    assert encoder.embedding.model_id == settings.rag.embedding.model_id
    assert encoder.reranker.model_id == settings.rag.reranker.model_id
    assert encoder.blocking_calls.slots == settings.coordination.blocking_call_slots


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@encoder:8769",
        "http://encoder:8769/?token=x",
        "ftp://encoder:8769",
        " http://encoder:8769",
    ],
)
def test_a_url_that_could_carry_a_credential_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**_payload(embedding=url))
    with pytest.raises(ValidationError):
        Settings(**_payload(reranker=url))


def test_plain_http_to_a_named_host_is_allowed_as_it_is_for_qdrant() -> None:
    """Unlike an MCP endpoint, and on purpose: tenant text on a private network,
    the same material the Qdrant connection already carries in the clear."""

    settings = Settings(**_payload("http://encoder:8769"))

    assert settings.rag.embedding.service_url == "http://encoder:8769"


def test_the_encoder_loads_in_process_even_when_its_profile_names_itself() -> None:
    """The Compose profile is one file read by five processes, four of them
    clients. The fifth used to refuse to start on seeing its own address --
    on the one deployment it exists for, taking every service that waits on
    its health down with it. Caught by review before it ever ran."""

    settings = Settings(**_payload("http://encoder:8769", "http://encoder:8769"))

    own = loads_in_process(project_encoder_service(settings))

    assert own.embedding.service_url == ""
    assert own.reranker.service_url == ""
    # Nothing else moved.
    assert own.embedding.model_id == settings.rag.embedding.model_id
    assert own.reranker.model_id == settings.rag.reranker.model_id

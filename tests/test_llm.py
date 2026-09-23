import json
from pathlib import Path

import httpx
import pytest

from mimir_pipeline.config import LLMConfig
from mimir_pipeline.llm import (
    _CLASSIFY_PROMPT,
    _PROPOSE_PROMPT,
    BudgetExceeded,
    LLMDeadlineExceeded,
    LLMError,
    MockLLM,
    StructuredLLM,
    make_llm,
)
from mimir_pipeline.models import Issue


@pytest.fixture
def target():
    return Issue(
        key="one", rule="python:S1481", path="module.py", line=2, message="Unused variable"
    )


@pytest.fixture
def context():
    return {"files": {"module.py": "def run():\n    unused = 1  # DEMO_UNUSED\n    return 2\n"}}


def response_data(**updates):
    data = {
        "id": "resp_example",
        "model": "frozen-model-version",
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "label": "pertinent",
                                "rationale": "Unused",
                                "evidence": ["module.py:2"],
                            }
                        ),
                    }
                ],
            }
        ],
    }
    data.update(updates)
    return data


def client(tmp_path, monkeypatch, handler, **kwargs):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key-value")
    monkeypatch.setattr("mimir_pipeline.llm.time.sleep", lambda _: None)
    config = LLMConfig(model="requested-model", **kwargs)
    return StructuredLLM(config, tmp_path, transport=httpx.MockTransport(handler))


def test_responses_schema_and_accounting_are_persisted(tmp_path, monkeypatch, target, context):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_data(), headers={"x-request-id": "req_1"})

    llm = client(
        tmp_path, monkeypatch, handler, input_price_per_million=2, output_price_per_million=8
    )
    assert llm.classify(target, context).label == "pertinent"
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert "temperature" not in payload
    assert llm.usage["calls"] == 1
    assert llm.usage["total_tokens"] == 120
    assert llm.usage["cost_usd"] == pytest.approx(0.00036)
    record = json.loads((tmp_path / "0001-classify.json").read_text(encoding="utf-8"))
    assert record["response"]["model"] == "frozen-model-version"
    assert record["request_id"] == "req_1"
    assert record["prompt_sha256"]
    assert "private-test-key-value" not in json.dumps(record)
    llm.close()


def test_compatible_chat_format(tmp_path, monkeypatch, target, context):
    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["temperature"] == 0
        return httpx.Response(
            200,
            json={
                "model": "compatible-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"label":"inconclusive","rationale":"limited","evidence":[]}',
                        },
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 8},
            },
        )

    llm = client(tmp_path, monkeypatch, handler, provider="openai_compatible", temperature=0)
    assert llm.classify(target, context).label == "inconclusive"
    assert llm.usage["cost_usd"] is None
    assert llm.usage["total_tokens"] == 17
    llm.close()


def test_hard_call_limit_includes_retries(tmp_path, monkeypatch, target, context):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    llm = client(tmp_path, monkeypatch, handler, max_calls=2, retries=5)
    with pytest.raises(BudgetExceeded):
        llm.classify(target, context)
    assert len(calls) == 2
    assert llm.usage["input_tokens"] is None
    assert llm.usage["unknown_usage_calls"] == 2
    assert len(list(tmp_path.glob("*.json"))) == 2
    llm.close()


def test_retry_then_success_does_not_invent_missing_usage(tmp_path, monkeypatch, target, context):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("Timed out", request=request)
        return httpx.Response(200, json=response_data())

    llm = client(
        tmp_path,
        monkeypatch,
        handler,
        retries=1,
        input_price_per_million=2,
        output_price_per_million=8,
    )
    assert llm.classify(target, context).label == "pertinent"
    assert llm.usage["calls"] == 2
    assert llm.usage["known_input_tokens"] == 100
    assert llm.usage["input_tokens"] is None
    assert llm.usage["cost_usd"] is None
    llm.close()


@pytest.mark.parametrize(
    "data",
    [
        response_data(status="incomplete"),
        response_data(output=[{"content": [{"type": "refusal", "refusal": "No"}]}]),
        response_data(
            output=[{"content": [{"type": "output_text", "text": '{"label":"unexpected"}'}]}]
        ),
    ],
)
def test_incomplete_refusal_and_invalid_schema_do_not_retry(
    tmp_path, monkeypatch, target, context, data
):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=data))
    with pytest.raises(LLMError):
        llm.classify(target, context)
    assert llm.usage["calls"] == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    llm.close()


def test_unauthorized_is_not_retried(tmp_path, monkeypatch, target, context):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(LLMError, match="401"):
        llm.classify(target, context)
    assert llm.usage["calls"] == 1
    llm.close()


def test_budget_preflight_blocks_before_network(tmp_path, monkeypatch, target, context):
    def handler(_):
        pytest.fail("Orçamento insuficiente não pode chamar a API")

    llm = client(
        tmp_path,
        monkeypatch,
        handler,
        max_cost_usd=0.00001,
        input_price_per_million=2,
        output_price_per_million=8,
    )
    with pytest.raises(BudgetExceeded):
        llm.propose(target, context)
    assert llm.usage["calls"] == 0
    llm.close()


def test_secret_key_and_source_literals_redacted_before_send(
    tmp_path, monkeypatch, target, context
):
    context["files"]["secret_literal.py"] = 'API_KEY = "private-test-key-value"\n'

    def handler(request):
        assert b"private-test-key-value" not in request.content
        return httpx.Response(200, json=response_data())

    llm = client(tmp_path, monkeypatch, handler)
    llm.classify(target, context)
    assert "private-test-key-value" not in (tmp_path / "0001-classify.json").read_text()
    llm.close()


def test_missing_usage_stays_unknown(tmp_path, monkeypatch, target, context):
    llm = client(
        tmp_path, monkeypatch, lambda _: httpx.Response(200, json=response_data(usage=None))
    )
    llm.classify(target, context)
    assert llm.usage["total_tokens"] is None
    assert llm.usage["unknown_usage_calls"] == 1
    llm.close()


def test_mock_removes_exact_line_and_classifies_dynamic(tmp_path, target, context):
    llm = make_llm(LLMConfig(provider="mock", max_calls=3), tmp_path)
    assert isinstance(llm, MockLLM)
    assert llm.propose(target, context).edits[0].old_text == "    unused = 1  # DEMO_UNUSED\n"
    context["files"]["module.py"] = (
        "def run():\r\n    used = 1  # DEMO_DYNAMIC\r\n    return locals()['used']\r\n"
    )
    assert llm.classify(target, context).label == "false_positive"
    assert llm.propose(target, context).edits[0].old_text.endswith("\r\n")
    with pytest.raises(BudgetExceeded):
        llm.propose(target, context)
    assert llm.usage["simulated"] is True
    assert llm.usage["input_tokens"] is None


def test_versioned_prompt_files_match_packaged_defaults():
    root = Path(__file__).resolve().parents[1]
    assert (root / "prompts/classify.md").read_text(encoding="utf-8") == _CLASSIFY_PROMPT
    assert (root / "prompts/propose.md").read_text(encoding="utf-8") == _PROPOSE_PROMPT


def test_rate_limit_retries_then_succeeds(tmp_path, monkeypatch, target, context):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json=response_data())

    llm = client(tmp_path, monkeypatch, handler, retries=1)
    assert llm.classify(target, context).label == "pertinent"
    assert llm.usage["calls"] == 2
    assert llm.usage["total_tokens"] is None
    assert (tmp_path / "0002-classify.json").is_file()
    llm.close()


@pytest.mark.parametrize(
    "data",
    [
        response_data(output=None),
        response_data(output=[None]),
        response_data(output=[{"content": "bad"}]),
        response_data(output=[{"content": [None]}]),
        response_data(output=[{"content": [{"type": "output_text", "text": 42}]}]),
        response_data(output=[{"content": [{"type": "output_text", "text": "{bad-json"}]}]),
    ],
)
def test_malformed_responses_fail_with_auditable_error(
    tmp_path, monkeypatch, target, context, data
):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=data))
    with pytest.raises(LLMError):
        llm.classify(target, context)
    record = json.loads((tmp_path / "0001-classify.json").read_text(encoding="utf-8"))
    assert record["error"]
    assert "validated" not in record
    assert llm.usage["calls"] == 1
    llm.close()


@pytest.mark.parametrize(
    "data",
    [
        {"choices": {"bad": "shape"}},
        {"choices": [None]},
        {"choices": [{"message": "bad"}]},
        {"choices": [{"message": {"refusal": "No"}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
    ],
)
def test_chat_refusal_incomplete_and_malformed_envelope(
    tmp_path, monkeypatch, target, context, data
):
    llm = client(
        tmp_path,
        monkeypatch,
        lambda _: httpx.Response(200, json=data),
        provider="openai_compatible",
    )
    with pytest.raises(LLMError):
        llm.classify(target, context)
    assert llm.usage["calls"] == 1
    llm.close()


def patch_data(edits):
    return response_data(
        output=[
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "edits": edits,
                                "explanation": "Remove unused assignment",
                                "expected_effect": "One fewer issue",
                                "risks": [],
                            }
                        ),
                    }
                ]
            }
        ]
    )


@pytest.mark.parametrize(
    "edit",
    [
        {"path": "unseen.py", "old_text": "value = 1", "new_text": "value = 2"},
        {"path": "../module.py", "old_text": "def run():", "new_text": "def run(arg):"},
        {"path": "module.py", "old_text": "unseen code", "new_text": ""},
        {"path": "module.py", "old_text": "", "new_text": "value = 2"},
        {"path": "module.py", "old_text": " ", "new_text": ""},
    ],
)
def test_patch_must_bind_to_exact_unique_context(tmp_path, monkeypatch, target, context, edit):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=patch_data([edit])))
    with pytest.raises(LLMError, match="contexto"):
        llm.propose(target, context)
    record = json.loads((tmp_path / "0001-propose.json").read_text(encoding="utf-8"))
    assert "validated" not in record
    llm.close()


def test_patch_nested_schema_and_exact_binding(tmp_path, monkeypatch, target, context):
    edit = {"path": "module.py", "old_text": "    unused = 1  # DEMO_UNUSED\n", "new_text": ""}

    def handler(request):
        schema = json.loads(request.content)["text"]["format"]["schema"]
        for obj in [schema, *schema["$defs"].values()]:
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])
        return httpx.Response(200, json=patch_data([edit]))

    llm = client(tmp_path, monkeypatch, handler)
    assert llm.propose(target, context).edits[0].model_dump() == edit
    llm.close()


def test_patch_is_bound_to_redacted_not_original_context(tmp_path, monkeypatch, target, context):
    context["files"]["module.py"] = 'password = "private-test-key-value"\n'
    edit = {"path": "module.py", "old_text": context["files"]["module.py"], "new_text": ""}
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=patch_data([edit])))
    with pytest.raises(LLMError, match="contexto"):
        llm.propose(target, context)
    assert "private-test-key-value" not in (tmp_path / "0001-propose.json").read_text()
    llm.close()


def test_deadline_blocks_before_network_and_mock(tmp_path, monkeypatch, target, context):
    monkeypatch.setattr("mimir_pipeline.llm.time.monotonic", lambda: 10.0)

    def handler(_):
        pytest.fail("A chamada não pode iniciar após o deadline")

    llm = client(tmp_path, monkeypatch, handler)
    llm.deadline = 9.0
    with pytest.raises(LLMDeadlineExceeded, match="tempo"):
        llm.classify(target, context)
    assert llm.usage["calls"] == 0
    llm.close()
    mock = MockLLM(LLMConfig(provider="mock"), tmp_path / "mock")
    mock.deadline = 9.0
    with pytest.raises(LLMDeadlineExceeded, match="tempo"):
        mock.classify(target, context)
    assert mock.usage["calls"] == 0


def test_remaining_deadline_bounds_network_timeout(tmp_path, monkeypatch, target, context):
    monkeypatch.setattr("mimir_pipeline.llm.time.monotonic", lambda: 10.0)

    def handler(request):
        assert request.extensions["timeout"]["read"] == 3.0
        assert request.extensions["timeout"]["connect"] == 3.0
        return httpx.Response(200, json=response_data())

    llm = client(tmp_path, monkeypatch, handler)
    llm.deadline = 13.0
    assert llm.classify(target, context).label == "pertinent"
    llm.close()


def test_expired_deadline_after_response_records_cost_without_accepting(
    tmp_path, monkeypatch, target, context
):
    now = [10.0]
    monkeypatch.setattr("mimir_pipeline.llm.time.monotonic", lambda: now[0])

    def handler(_):
        now[0] = 20.0
        return httpx.Response(200, json=response_data())

    llm = client(tmp_path, monkeypatch, handler)
    llm.deadline = 13.0
    with pytest.raises(LLMDeadlineExceeded, match="tempo"):
        llm.classify(target, context)
    assert llm.usage["total_tokens"] == 120
    assert llm.usage["calls"] == 1
    record = json.loads((tmp_path / "0001-classify.json").read_text(encoding="utf-8"))
    assert "validated" not in record
    llm.close()


def test_retry_does_not_outlive_deadline(tmp_path, monkeypatch, target, context):
    monkeypatch.setattr("mimir_pipeline.llm.time.monotonic", lambda: 10.0)
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(429, json={"error": "wait"}))
    llm.deadline = 10.5
    with pytest.raises(LLMDeadlineExceeded, match="Tempo restante"):
        llm.classify(target, context)
    assert llm.usage["calls"] == 1
    llm.close()


def test_invalid_usage_does_not_become_zero(tmp_path, monkeypatch, target, context):
    llm = client(
        tmp_path, monkeypatch, lambda _: httpx.Response(200, json=response_data(usage="invalid"))
    )
    assert llm.classify(target, context).label == "pertinent"
    assert llm.usage["total_tokens"] is None
    assert llm.usage["unknown_usage_calls"] == 1
    llm.close()

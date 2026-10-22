"""The adapter must not use forced tool_choice (HTTP 400 on claude-sonnet-5-5) and must surface refusals/truncation as errors."""
from types import SimpleNamespace

import pytest

from app.services.llm import LLM, LLMError
from app.services.offers.schema import PageExtraction


class StubMessages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        resp = self.resp

        class Ctx:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: resp)

            def __exit__(self, *a):
                return False

        return Ctx()


def make(resp, db):
    llm = LLM.__new__(LLM)
    llm.client = SimpleNamespace(messages=StubMessages(resp))
    llm.model, llm.db, llm.job_run_id, llm.calls = "claude-sonnet-5-5", db, None, 0
    return llm


def resp(parsed=None, stop="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop, stop_details=None, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_request_uses_schema_output_not_forced_tools(seeded):
    llm = make(resp(PageExtraction()), seeded)
    out = llm.structured(system="s", user="u", schema=PageExtraction, effort="low")
    kw = llm.client.messages.kwargs
    assert out == PageExtraction()
    assert "tool_choice" not in kw and "tools" not in kw  # forced tool use is rejected by this model
    assert kw["output_format"] is PageExtraction and kw["output_config"] == {"effort": "low"} and kw["model"] == "claude-sonnet-5-5"
    assert kw["max_tokens"] >= 16000  # headroom: adaptive thinking tokens count against max_tokens
    assert "untrusted web content" in kw["system"]


@pytest.mark.parametrize("r,msg", [(resp(None), "did not return"), (resp(PageExtraction(), "max_tokens"), "cut off"), (resp(None, "refusal"), "declined")])
def test_bad_responses_raise_a_clear_error(seeded, r, msg):
    with pytest.raises(LLMError, match=msg):
        make(r, seeded).structured(system="s", user="u", schema=PageExtraction)


def test_out_of_credit_is_explained_in_plain_english():
    from app.services.llm import friendly_error

    e = RuntimeError("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}")
    assert "out of credit" in friendly_error(e) and "Plans & Billing" in friendly_error(e)
    assert friendly_error(ValueError("something else")) == "ValueError: something else"

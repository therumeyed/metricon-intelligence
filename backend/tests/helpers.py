from __future__ import annotations

from app.services import fetcher


def page(body: str, banner: str = "", footer_extra: str = "") -> str:
    """SYNTHETIC test page: template chrome (nav, clock, copyright, tokens) around the given main-content HTML."""
    return f"""<html><head><title>Offers</title></head><body>
<header>{banner}</header>
<nav><ul><li><a href="/">Home</a></li><li><a href="/offers">Offers</a></li></ul></nav>
<main>{body}</main>
<footer>(c) 2026 Test Builder. Page rendered {footer_extra}</footer></body></html>"""


def ok_result(url: str, html: str) -> fetcher.FetchResult:
    return fetcher.FetchResult(outcome="ok", requested_url=url, final_url=url, status=200, content_type="text/html", body=html.encode())


def fail_result(url: str, outcome="timeout", err="timeout") -> fetcher.FetchResult:
    return fetcher.FetchResult(outcome=outcome, requested_url=url, error=err)


class FakeLLM:
    """Stands in for the Anthropic client in tests only. Returns whatever the test scripted."""

    model = "test-model"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def structured(self, *, system, user, schema, tool_name="submit", max_tokens=0):
        self.calls += 1
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return out if not callable(out) else out(user)

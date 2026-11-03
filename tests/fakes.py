"""Test doubles for the LLM seam (recon.llm.LLMClient) and for the Groq SDK client."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Callable, Optional, Union

from recon.llm import LLMClient, LLMUsage

Reply = Union[dict, Callable[[int], dict]]


class FakeLLM:
    """Canned replies, no network. `page`: the extraction reply (or a function of the call
    number); `summary`: the summary text (or a function of the facts); `profile`: the profile
    draft (or a function of the draft call number, 1-based); `fail`: raised by every call."""

    def __init__(self, page: Optional[Reply] = None, summary: Union[str, Callable[[dict], str]] = "",
                 fail: Optional[Exception] = None, available: bool = True, text_model: str = "fake-text",
                 profile: Optional[Reply] = None):
        self.page = page
        self.summary = summary
        self.profile = profile
        self.fail = fail
        self._available = available
        self.text_model = text_model
        self.usage = LLMUsage()
        self.calls: list[str] = []
        self.profile_requests: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    def _call(self, purpose: str) -> dict:
        self.calls.append(purpose)
        self.usage.calls += 1
        if self.fail is not None:
            raise self.fail
        return {"purpose": purpose, "model": self.text_model}

    def _page(self) -> dict:
        reply = self.page(len(self.calls)) if callable(self.page) else self.page
        return reply if reply is not None else {}

    def extract_page_image(self, png: bytes) -> tuple[dict, dict]:
        info = self._call("vision extraction")
        return self._page(), info

    def extract_page_text(self, page_text: str) -> tuple[dict, dict]:
        info = self._call("text extraction")
        return self._page(), info

    def write_summary(self, facts: dict) -> tuple[str, dict]:
        info = self._call("summary")
        return (self.summary(facts) if callable(self.summary) else self.summary), info

    def draft_profile(self, request: str) -> tuple[dict, dict]:
        info = self._call("profile draft")
        self.profile_requests.append(request)
        reply = self.profile(len(self.profile_requests)) if callable(self.profile) else self.profile
        return (reply if reply is not None else {}), info


_conforms: LLMClient = FakeLLM()     # mypy: the fake implements the whole seam


class FakeGroqClient:
    """Stands in for groq.Groq: `client.chat.completions.create(**kw)` returns `content` with fixed token usage.
    `fail`: model -> exception raised for calls to that model (e.g. a rate limit)."""

    def __init__(self, content: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                 fail: Optional[dict[str, Exception]] = None):
        self.requests: list[dict] = []

        def create(**kw):
            self.requests.append(kw)
            if fail and kw["model"] in fail:
                raise fail[kw["model"]]
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

"""
Claude API wrapper: extended thinking, server-side web search, robust JSON extraction
with one self-repair retry, token/cost accounting, and mock mode for offline testing.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import MOCKS_DIR, MODEL_PRICES, Config, PROMPTS_DIR
from .util import extract_json, log
from .failures import is_permanent_provider_error


@dataclass
class LLMResult:
    text: str
    parsed: dict | None = None
    usage: dict = field(default_factory=dict)


def load_prompt(name: str, replacements: dict[str, str]) -> str:
    """Load prompts/<name>.md and substitute {{PLACEHOLDER}} tokens."""
    path = PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    for key, val in replacements.items():
        text = text.replace("{{" + key + "}}", val if val is not None else "")
    return text


def prompt_version(name: str) -> str:
    """First line of a prompt file may carry '<!-- prompt-version: N -->'."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return "?"
    first = path.read_text(encoding="utf-8").splitlines()[0] if path.stat().st_size else ""
    if "prompt-version:" in first:
        return first.split("prompt-version:")[1].strip(" ->!<")
    return "1"


def _estimate_cost(model: str, in_tok: int, out_tok: int, searches: int = 0) -> float:
    price = (3.0, 15.0)
    for key, p in MODEL_PRICES.items():
        if model.startswith(key):
            price = p
            break
    return round(in_tok / 1e6 * price[0] + out_tok / 1e6 * price[1] + searches * 0.01, 4)


RESPONSES_DIR = Path(__file__).resolve().parent.parent / "llm_responses"


def _archive_response(name: str, model: str, text: str, usage: dict) -> None:
    """Write a paid-for response to disk the moment it arrives.

    The ledger records what a call COST but never what it returned, so a crash
    anywhere downstream of the call destroys the result and leaves only the
    bill. That is not hypothetical: a weekly mining run returned a full
    playbook rewrite, saved its mined lessons, then died printing one of them
    on a console that could not encode an arrow. The rewrite -- the entire
    point of the run, and the most expensive single call this system makes --
    was gone, and the next attempt failed on an exhausted credit balance.

    So the response lands on disk before any parsing, any validation, and any
    caller touches it. Best effort by construction: archiving must never be
    able to fail the call it is protecting.
    """
    try:
        RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = RESPONSES_DIR / f"{stamp}-{name}.json"
        path.write_text(json.dumps({
            "prompt_name": name, "model": model, "saved_at": stamp,
            "usage": usage, "text": text,
        }, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — never fail a call over its own backup
        pass


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None
        if not cfg.mock:
            if not cfg.anthropic_api_key:
                raise SystemExit(
                    "[llm] No Anthropic API key. Add it to credentials.json "
                    "(anthropic_api_key) or set ANTHROPIC_API_KEY. "
                    "Or run with --mock for a dry pipeline test."
                )
            from anthropic import Anthropic
            self._client = Anthropic(api_key=cfg.anthropic_api_key)

    # ------------------------------------------------------------------ mock
    def _mock_response(self, mock_name: str) -> LLMResult:
        path = MOCKS_DIR / f"{mock_name}.json"
        if not path.exists():
            raise SystemExit(f"[llm] mock file missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        text = data["text"] if isinstance(data, dict) and "text" in data else json.dumps(data)
        return LLMResult(text=text, usage={"input_tokens": 0, "output_tokens": 0,
                                           "web_searches": 0, "cost_usd": 0.0, "mock": True})

    # ------------------------------------------------------------------ call
    def call(
        self,
        *,
        system: str,
        user: str,
        model: str,
        mock_name: str,
        thinking: bool = False,
        web_search: bool = False,
        expect_json: bool = True,
        max_tokens: int | None = None,
    ) -> LLMResult:
        if self.cfg.mock:
            result = self._mock_response(mock_name)
            if expect_json:
                result.parsed = extract_json(result.text)
            return result

        max_tokens = max_tokens or int(self.cfg.get("max_output_tokens", 16000))
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if thinking and self.cfg.get("extended_thinking"):
            budget = int(self.cfg.get("thinking_budget_tokens", 8000))
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max(max_tokens, budget + 4000)
        if web_search and self.cfg.get("enable_web_search"):
            kwargs["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": int(self.cfg.get("web_search_max_uses", 6)),
            }]

        resp = self._create_with_retry(kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = self._usage(resp, model)
        _archive_response(mock_name, model, text, usage)

        result = LLMResult(text=text, usage=usage)
        if expect_json:
            try:
                result.parsed = extract_json(text)
            except ValueError:
                log("llm", "JSON parse failed — asking model to repair its output")
                repair_kwargs = dict(kwargs)
                repair_kwargs.pop("thinking", None)
                repair_kwargs.pop("tools", None)
                repair_kwargs["max_tokens"] = max_tokens
                repair_kwargs["messages"] = kwargs["messages"] + [
                    {"role": "assistant", "content": text[-8000:]},
                    {"role": "user", "content":
                        "Your previous reply could not be parsed as JSON. Reply with ONLY the "
                        "corrected, complete JSON object — no prose, no code fences."},
                ]
                resp2 = self._create_with_retry(repair_kwargs)
                text2 = "".join(b.text for b in resp2.content if getattr(b, "type", "") == "text")
                u2 = self._usage(resp2, model)
                for k in ("input_tokens", "output_tokens", "cost_usd"):
                    usage[k] = usage.get(k, 0) + u2.get(k, 0)
                result.parsed = extract_json(text2)
                result.text = text2
        return result

    def _create_with_retry(self, kwargs: dict, attempts: int = 3):
        last = None
        for i in range(attempts):
            try:
                return self._client.messages.create(**kwargs)
            except TypeError:
                # older SDK without thinking/tools support — degrade gracefully
                kwargs.pop("thinking", None)
                kwargs.pop("tools", None)
                return self._client.messages.create(**kwargs)
            except Exception as e:  # rate limits, overloaded, transient network
                last = e
                # A permanent provider condition -- an exhausted credit balance,
                # a bad key -- cannot clear inside the retry window. Backing off
                # 15+30+45s to re-earn the identical 400 was 90 of the 128
                # seconds each failed run on 2026-09-22 spent, out of a 300s
                # serverless budget. Fail fast and say why.
                if is_permanent_provider_error(e):
                    log("llm", f"API error ({type(e).__name__}): {e} -- "
                               f"permanent, not retrying")
                    raise RuntimeError(
                        f"Anthropic API failed permanently (not retried): {e}") from e
                wait = 15 * (i + 1)
                log("llm", f"API error ({type(e).__name__}): {e} — retrying in {wait}s")
                time.sleep(wait)
        raise RuntimeError(f"Anthropic API failed after {attempts} attempts: {last}")

    @staticmethod
    def _usage(resp, model: str) -> dict:
        u = getattr(resp, "usage", None)
        in_tok = getattr(u, "input_tokens", 0) or 0
        out_tok = getattr(u, "output_tokens", 0) or 0
        searches = 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            searches = getattr(stu, "web_search_requests", 0) or 0
        return {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "web_searches": searches,
            "cost_usd": _estimate_cost(model, in_tok, out_tok, searches),
        }


def merge_usage(*usages: dict) -> dict:
    out = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
    for u in usages:
        for k in out:
            out[k] += u.get(k, 0)
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out

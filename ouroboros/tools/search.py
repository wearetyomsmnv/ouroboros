"""Web search tool — powered by Perplexity via OpenRouter."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _web_search(ctx: ToolContext, query: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "OPENROUTER_API_KEY not set; web_search unavailable."})

    try:
        from openai import OpenAI

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://colab.research.google.com/",
                "X-Title": "Ouroboros",
            },
        )

        model = os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "perplexity/sonar")

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a research assistant. Answer the user's query with "
                        "accurate, up-to-date information. Include relevant facts, "
                        "numbers, and cite sources where possible."
                    ),
                },
                {"role": "user", "content": query},
            ],
            max_tokens=2048,
        )

        resp_dict = resp.model_dump()
        choices = resp_dict.get("choices") or [{}]
        message = (choices[0] if choices else {}).get("message") or {}
        answer = message.get("content") or "(no answer)"

        # Perplexity returns citations in a top-level "citations" field
        citations = resp_dict.get("citations") or []

        result: Dict[str, Any] = {"answer": answer}
        if citations:
            result["sources"] = citations

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "web_search",
            {
                "name": "web_search",
                "description": (
                    "Search the web via OpenAI Responses API. Returns JSON with answer + sources."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            _web_search,
        ),
    ]

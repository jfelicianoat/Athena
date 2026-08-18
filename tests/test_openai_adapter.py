from __future__ import annotations

from athena.adapters import OpenAICompatibleModelProvider
from athena.models import ModelProvider


def test_openai_compatible_adapter_parses_correlated_tool_calls() -> None:
    provider = OpenAICompatibleModelProvider("http://localhost:1234/v1", "local")

    response = provider._parse_response(
        {
            "model": "local",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
    )

    assert isinstance(provider, ModelProvider)
    assert response.tool_calls[0].call_id == "call-1"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage.input_tokens == 10

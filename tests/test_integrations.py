"""Exercise the real SDK/adapters against local responses, without paid API calls."""

import asyncio
import json
from datetime import date

import httpx
import pytest
from langchain_core.tools import StructuredTool

from outreach.brain import GeminiBrain, OpenAIBrain
from outreach.models import Evidence, Quality
from outreach.research import MCPResearch


@pytest.mark.parametrize(
    "schema,output",
    [
        (
            Quality,
            {
                "grounded": True,
                "product_relevant": True,
                "recipient_relevant": True,
                "personalized": True,
                "issues": [],
            },
        ),
        (
            Evidence,
            {
                "id": "signal",
                "document_id": "source",
                "title": "Office opening",
                "claim": "Aster Labs opened a Berlin office.",
                "quote": "Aster Labs opened its first European office in Berlin.",
                "kind": "news",
                "event_date": None,
                "date_quote": "",
                "company_match": True,
                "current": True,
                "confidence": "high",
            },
        ),
    ],
)
def test_native_gemini_structured_output_uses_real_adapter(monkeypatch, schema, output):
    from google.genai import types

    calls = []
    brain = GeminiBrain("local-test-key")

    async def generate_content(**kwargs):
        calls.append(kwargs)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model", parts=[types.Part(text=json.dumps(output))]
                    ),
                    finish_reason="STOP",
                )
            ]
        )

    monkeypatch.setattr(brain.model.client.aio.models, "generate_content", generate_content)
    answer = asyncio.run(
        brain.complete(schema, "Extract supported facts only.", {"source": "Untrusted source text"})
    )
    assert isinstance(answer, schema)
    assert calls[0]["model"] == "gemini-3.8-flash"
    call_config = calls[0]["config"]
    config_dict = call_config if isinstance(call_config, dict) else call_config.model_dump()
    assert config_dict["response_mime_type"] == "application/json"
    assert config_dict.get("response_json_schema") or config_dict.get("response_schema")
    assert brain.model.client.vertexai is False


def test_actual_openai_sdk_parses_strict_structured_response():
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local-test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "grounded": True,
                                    "product_relevant": True,
                                    "recipient_relevant": True,
                                    "personalized": True,
                                    "issues": [],
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def check():
        from langchain_openai import ChatOpenAI

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            brain = OpenAIBrain.__new__(OpenAIBrain)
            brain.model = ChatOpenAI(
                api_key="local-test-key", model="gpt-5-mini", http_async_client=client
            )
            answer = await brain.complete(Quality, "Check the draft.", {"draft": "Sample"})
            assert answer.grounded and answer.personalized

    asyncio.run(check())
    schema = requests[0]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == set(Quality.model_fields)


def test_research_schema_is_accepted_by_strict_output_contract():
    requests = []
    sample = Evidence(
        id="sample",
        document_id="source",
        title="New office",
        claim="Aster Labs opened an office.",
        quote="Aster Labs opened its first European office.",
        kind="news",
        event_date=None,
        date_quote="",
        company_match=True,
        current=True,
        confidence="high",
    )

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local-test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": sample.model_dump_json(),
                        },
                    }
                ],
            },
        )

    async def check():
        from langchain_openai import ChatOpenAI

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            brain = OpenAIBrain.__new__(OpenAIBrain)
            brain.model = ChatOpenAI(
                api_key="local-test-key", model="gpt-5-mini", http_async_client=client
            )
            answer = await brain.complete(Evidence, "Extract only supported facts.", {})
            assert answer.id == "sample"

    asyncio.run(check())
    schema = requests[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(Evidence.model_fields)
    assert "default" not in json.dumps(schema)


def test_real_langchain_tool_invocation_and_mcp_configuration(monkeypatch):
    configurations, arguments = [], []

    async def search(
        query: str, max_results: int, search_depth: str, start_date: str, end_date: str
    ) -> str:
        """Local stand-in for the Tavily MCP search tool."""
        arguments.append({"query": query, "start_date": start_date, "end_date": end_date})
        return json.dumps(
            {
                "results": [
                    {
                        "url": "https://aster-labs.example/news",
                        "title": "Expansion",
                        "content": "Discovery only",
                    }
                ]
            }
        )

    class Client:
        def __init__(self, connections):
            configurations.append(connections)

        async def get_tools(self):
            return [StructuredTool.from_function(coroutine=search, name="tavily-search")]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", Client)
    docs = asyncio.run(MCPResearch("test-key", None)._search("Aster company news", 90))
    assert docs[0].provenance == "search_snippet"
    assert arguments[0]["end_date"] == date.today().isoformat()
    assert configurations[0]["tavily"]["transport"] == "http"
    assert configurations[0]["tavily"]["headers"]["Authorization"] == "Bearer test-key"

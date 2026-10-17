import asyncio

from outreach.models import Candidate, Candidates, OutreachRequest
from outreach.research import MCPResearch


class FixtureResearch(MCPResearch):
    def __init__(self):
        self.calls_seen = []
        super().__init__("local-test-key", self)

    async def complete(self, schema, instruction, data):
        return Candidates(
            candidates=[
                Candidate(
                    name="Aster Labs", domain="aster-labs.example", description="Software company"
                )
            ]
        )

    async def _call(self, name, args):
        self.calls_seen.append((name, args))
        self.calls += 1
        if name == "tavily_extract":
            return {
                "results": [
                    {
                        "url": url,
                        "raw_content": "Aster Labs company page. [About](/about) [News](/newsroom) [Other](https://other.example/news) [Website](https://aster-labs.example/)",
                    }
                    for url in args["urls"]
                ]
            }
        return {
            "results": [
                {
                    "url": "https://aster-labs.example/",
                    "title": "Aster Labs",
                    "content": "Official software company.",
                }
            ]
        }


def test_exact_supplied_page_and_first_party_links_precede_external_searches():
    provider = FixtureResearch()
    request = OutreachRequest(
        company_name="Aster Labs", company_url="https://aster-labs.example/company/start"
    )

    async def run():
        candidates = await provider.resolve(request)
        return await provider.fetch(request, candidates[0], 90, [])

    asyncio.run(run())
    assert provider.calls_seen[0] == (
        "tavily_extract",
        {"urls": [request.company_url], "extract_depth": "advanced"},
    )
    first_links = provider.calls_seen[1][1]["urls"]
    assert "https://aster-labs.example/about" in first_links
    assert "https://aster-labs.example/newsroom" in first_links
    assert all("other.example" not in url for url in first_links)
    first_search = next(
        i for i, (name, _) in enumerate(provider.calls_seen) if name == "tavily_search"
    )
    assert first_search > 1
    queries = [args["query"] for name, args in provider.calls_seen if name == "tavily_search"]
    assert queries[0].startswith("site:aster-labs.example") and queries[1].startswith(
        "site:aster-labs.example"
    )


def test_linkedin_company_page_resolves_official_domain_separately():
    provider = FixtureResearch()
    request = OutreachRequest(
        company_name="Aster Labs", company_url="https://www.linkedin.com/company/aster-labs/"
    )
    candidates = asyncio.run(provider.resolve(request))
    assert provider.calls_seen[0][1]["urls"] == [request.company_url]
    assert candidates[0].domain == "aster-labs.example"
    assert any(name == "tavily_search" for name, _ in provider.calls_seen)


def test_inaccessible_supplied_page_requires_identity_clarification():
    class Inaccessible(FixtureResearch):
        async def _call(self, name, args):
            self.calls_seen.append((name, args))
            return {"results": [], "failed_results": [{"url": args["urls"][0]}]}

    provider = Inaccessible()
    candidates = asyncio.run(
        provider.resolve(
            OutreachRequest(
                company_name="Aster Labs", company_url="https://aster-labs.example/about"
            )
        )
    )
    assert not candidates
    assert provider.gaps

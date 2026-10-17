"""MCP retrieval, source normalization and evidence admission rules."""

import asyncio
import hashlib
import ipaddress
import json
import re
from datetime import date, timedelta
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from outreach.brain import RESEARCH, RESOLVE, Brain
from outreach.models import (
    Candidate,
    Candidates,
    Document,
    Evidence,
    OutreachRequest,
    ResearchReport,
)


def public_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("A public website URL is required.")
    parts = urlsplit(value if "://" in value else "https://" + value)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme not in {"https", "http"} or not host or parts.username or parts.password:
        raise ValueError("Use a public HTTP(S) URL without credentials.")
    if "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Use a public website hostname.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private network addresses are not supported.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def domain(value: str) -> str:
    return urlsplit(public_url(value)).hostname.removeprefix("www.")


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def tool_payload(result):
    """Support adapter text blocks, structured content and ToolMessage responses."""
    if hasattr(result, "status") and result.status == "error":
        raise RuntimeError("Research tool returned an error.")
    if hasattr(result, "content"):
        result = result.content
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, list):
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block.get("text", ""))
                except json.JSONDecodeError:
                    continue
        raise ValueError("Research tool did not return structured search data.")
    if isinstance(result, dict):
        if result.get("isError"):
            raise RuntimeError("Research tool returned an error.")
        if "structuredContent" in result:
            return result["structuredContent"]
        if "content" in result and isinstance(result["content"], list):
            return tool_payload(result["content"])
        return result
    raise ValueError("Unsupported research tool response.")


class ResearchProvider(Protocol):
    async def resolve(self, request: OutreachRequest) -> list[Candidate]: ...

    async def fetch(
        self, request: OutreachRequest, company: Candidate, window: int, supplied: list[Document]
    ) -> tuple[list[Document], list[str]]: ...


class MCPResearch:
    """Each instance belongs to one invocation/event loop; no cached async sessions."""

    def __init__(self, api_key: str, brain: Brain):
        self.api_key = api_key
        self.brain = brain
        self.calls = 0
        self.gaps: list[str] = []
        self._tools = None

    async def _call(self, name: str, args: dict):
        from langchain_mcp_adapters.client import MultiServerMCPClient

        for attempt in range(3):
            if self.calls >= 12:
                self.gaps.append("Research call budget reached (12 attempts this pass).")
                return {}
            self.calls += 1
            try:
                async with asyncio.timeout(40):
                    if self._tools is None:
                        client = MultiServerMCPClient(
                            {
                                "tavily": {
                                    "url": "https://mcp.tavily.com/mcp/",
                                    "transport": "http",
                                    "headers": {"Authorization": f"Bearer {self.api_key}"},
                                }
                            }
                        )
                        self._tools = await client.get_tools()
                    tool = next(t for t in self._tools if t.name.replace("-", "_").endswith(name))
                    return tool_payload(await tool.ainvoke(args))
            except (TimeoutError, ConnectionError, OSError):
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as exc:
                # Retry only transport/rate-limit/server failures. Never expose key-bearing errors.
                status = getattr(exc, "status_code", None)
                transient = status == 429 or (isinstance(status, int) and status >= 500)
                transient |= type(exc).__name__ in {"ReadTimeout", "ConnectTimeout", "ConnectError"}
                if transient and attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            self.gaps.append(f"{name} could not complete; retry research or supply a source.")
            return {}
        return {}

    async def _search(self, query: str, days: int | None = None) -> list[Document]:
        args = {"query": query, "max_results": 4, "search_depth": "advanced"}
        if days:
            args["start_date"] = (date.today() - timedelta(days=days)).isoformat()
            args["end_date"] = date.today().isoformat()
        payload = await self._call("tavily_search", args)
        documents = []
        for item in payload.get("results", []):
            try:
                documents.append(
                    Document(
                        url=public_url(item["url"]),
                        title=item.get("title", "Search result"),
                        content=item.get("content", ""),
                        published_at=item.get("published_date") or "",
                        provenance="search_snippet",
                    )
                )
            except (KeyError, ValueError):
                continue
        return documents

    async def _extract(self, urls: list[str], snippets: list[Document]) -> list[Document]:
        valid = []
        for url in urls:
            try:
                valid.append(public_url(url))
            except ValueError:
                continue
        if not valid:
            return []
        payload = await self._call("tavily_extract", {"urls": valid, "extract_depth": "advanced"})
        documents = []
        metadata = {doc.url: doc for doc in snippets}
        for item in payload.get("results", []):
            try:
                url = public_url(item["url"])
                content = item.get("raw_content") or item.get("content") or ""
                if len(content.strip()) < 40:
                    continue
                prior = metadata.get(url)
                documents.append(
                    Document(
                        url=url,
                        title=prior.title if prior else url,
                        content=content[:24000],
                        published_at=prior.published_at if prior else "",
                        provenance="extracted",
                    )
                )
            except (KeyError, ValueError):
                continue
        for failure in payload.get("failed_results", []):
            try:
                failed_url = public_url(failure.get("url", ""))
                self.gaps.append(
                    f"Could not read {failed_url}; paste the source text if available."
                )
            except ValueError:
                self.gaps.append("One source was inaccessible.")
        return documents

    async def resolve(self, request: OutreachRequest) -> list[Candidate]:
        self.calls = 0
        if request.company_url:
            return [
                Candidate(
                    name=request.company_name,
                    domain=domain(request.company_url),
                    description="Company website supplied by you",
                )
            ]
        docs = await self._search(f'"{request.company_name}" official company website')
        if not docs:
            return []
        answer = await self.brain.complete(
            Candidates,
            RESOLVE,
            {
                "company_name": request.company_name,
                "results": [d.model_dump() for d in docs],
            },
        )
        allowed_hosts = {domain(d.url) for d in docs}
        candidates = []
        for candidate in answer.candidates:
            try:
                host = domain(candidate.domain)
                if host in allowed_hosts and host not in {c.domain for c in candidates}:
                    candidates.append(candidate.model_copy(update={"domain": host}))
            except ValueError:
                continue
        return candidates[:4]

    async def fetch(self, request, company, window, supplied):
        self.calls = 0
        self.gaps = [
            "LinkedIn coverage is limited to publicly accessible content; absence is not proof of no activity."
        ]
        documents = list(supplied)
        urls = [f"https://{company.domain}/"]
        urls += [d.url for d in supplied if d.url and d.provenance == "search_snippet"]
        if request.recipient_url:
            urls.append(request.recipient_url)
        documents.extend(await self._extract(list(dict.fromkeys(urls))[:4], []))
        queries = [
            f"site:{company.domain} about company founded employees leadership CEO",
            f'"{company.name}" company news CEO announcement',
            f'site:linkedin.com/posts "{company.name}" announcement award expansion',
            f"site:{company.domain} blog news articles announcement",
            f'"{request.recipient_name}" "{company.name}" "{request.recipient_role}" interview',
        ]
        for query in queries:
            if self.calls >= 12:
                break
            snippets = await self._search(query, window)
            # Snippets aid discovery only. Only extracted text can support a signal.
            documents.extend(await self._extract([d.url for d in snippets[:2]], snippets))
        return deduplicate_documents(documents), list(dict.fromkeys(self.gaps))


def deduplicate_documents(documents: list[Document]) -> list[Document]:
    unique = {}
    for doc in documents:
        key = hashlib.sha256(compact(doc.content).encode()).hexdigest()
        if key not in unique or unique[key].provenance == "search_snippet":
            unique[key] = doc
    return list(unique.values())


def ground_report(report: ResearchReport, documents: list[Document], company: Candidate):
    """Do not trust model-generated citations, verification labels or source URLs."""
    docs = {doc.id: doc for doc in documents if doc.provenance != "search_snippet"}
    report.company.name, report.company.domain = company.name, company.domain
    for name in ("industry", "employee_size", "founding_year", "ceo"):
        fact = getattr(report.company, name)
        fact.document_ids = [key for key in fact.document_ids if key in docs]
        if (
            not fact.document_ids
            or not fact.quote
            or not any(
                compact(fact.quote) in compact(docs[key].content) for key in fact.document_ids
            )
        ):
            fact.value = None
            fact.document_ids = []
            fact.quote = ""
    seen = set()
    evidence = []
    for item in report.evidence:
        doc = docs.get(item.document_id)
        if (
            not doc
            or len(compact(item.quote)) < 20
            or compact(item.quote) not in compact(doc.content)
        ):
            report.gaps.append(f"Excluded unsupported source quote: {item.title}")
            continue
        key = compact(item.claim)
        quote_key = compact(item.quote)
        if key in seen or quote_key in seen:
            continue
        seen.update((key, quote_key))
        item.id = hashlib.sha256((doc.url + quote_key).encode()).hexdigest()[:16]
        item.verification = doc.provenance
        item.source_url = doc.url
        item.published_at, item.retrieved_at = doc.published_at, doc.retrieved_at
        if item.event_date and (
            not item.date_quote or compact(item.date_quote) not in compact(doc.content)
        ):
            item.event_date = None
        evidence.append(item)
    report.evidence = evidence
    return report


def eligible_evidence(evidence: list[dict], today: date | None = None) -> list[Evidence]:
    today = today or date.today()
    eligible = []
    seen = set()
    for raw in evidence:
        item = Evidence.model_validate(raw)
        if (
            item.verification not in {"extracted", "user_provided", "demo"}
            or not item.company_match
            or not item.current
            or item.confidence != "high"
        ):
            continue
        timestamp = item.event_date or item.published_at[:10]
        try:
            age = (today - date.fromisoformat(timestamp)).days if timestamp else None
        except ValueError:
            age = None
        if age is not None and age < 0:
            continue
        if item.kind in {"news", "linkedin", "recipient"} and (age is None or age > 365):
            continue
        fingerprint = compact(item.quote)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        eligible.append(item)
    return eligible


async def extract_report(
    brain: Brain, company: Candidate, documents: list[Document], gaps: list[str]
):
    report = await brain.complete(
        ResearchReport,
        RESEARCH,
        {
            "confirmed_company": company.model_dump(),
            "today": date.today().isoformat(),
            "documents": [d.model_dump() for d in documents],
        },
    )
    report.gaps = list(dict.fromkeys(gaps + report.gaps))
    return ground_report(report, documents, company)

"""MCP retrieval, source normalization and evidence admission rules."""

import asyncio
import hashlib
import ipaddress
import json
import re
from datetime import date, timedelta
from time import perf_counter
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from outreach.brain import RESEARCH, RESOLVE, Brain
from outreach.models import (
    Candidate,
    Candidates,
    Document,
    Evidence,
    OutreachRequest,
    ResearchReport,
)
from outreach.telemetry import emit

PLATFORM_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "crunchbase.com",
    "wikipedia.org",
}


def platform_host(host: str) -> bool:
    return any(host == site or host.endswith("." + site) for site in PLATFORM_HOSTS)


def page_links(documents: list[Document], official_domain: str | None = None) -> list[str]:
    links = []
    for doc in documents:
        for target in re.findall(r"\]\(([^\s)]+)", doc.content):
            try:
                url = public_url(urljoin(doc.url, target.strip("<>")))
                if official_domain and domain(url) != official_domain:
                    continue
                if official_domain and not re.search(
                    r"about|team|leadership|news|press|blog|article|insight|company",
                    urlsplit(url).path,
                    re.I,
                ):
                    continue
                if url not in links and url != doc.url:
                    links.append(url)
            except ValueError:
                continue
    return links


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
        self._seeds: dict[str, list[Document]] = {}
        self._seed_gaps: list[str] = []
        self._reading_seed = False

    async def _call(self, name: str, args: dict):
        from langchain_mcp_adapters.client import MultiServerMCPClient

        for attempt in range(3):
            if self.calls >= 12:
                self.gaps.append("Research call budget reached (12 attempts this pass).")
                return {}
            self.calls += 1
            started = perf_counter()
            activity = (
                "searching"
                if name.endswith("search")
                else "reading_page"
                if self._reading_seed
                else "reading_sources"
            )
            emit(activity, attempt=attempt + 1)
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
                    payload = tool_payload(await tool.ainvoke(args))
                    emit(
                        activity,
                        "completed",
                        duration_ms=round((perf_counter() - started) * 1000),
                        count=len(payload.get("results", [])),
                    )
                    return payload
            except (TimeoutError, ConnectionError, OSError):
                if attempt < 2:
                    emit("retrying", attempt=attempt + 2)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as exc:
                # Retry only transport/rate-limit/server failures. Never expose key-bearing errors.
                status = getattr(exc, "status_code", None)
                transient = status == 429 or (isinstance(status, int) and status >= 500)
                transient |= type(exc).__name__ in {"ReadTimeout", "ConnectTimeout", "ConnectError"}
                if transient and attempt < 2:
                    emit("retrying", attempt=attempt + 2)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            self.gaps.append(f"{name} could not complete; retry research or supply a source.")
            emit(
                activity,
                "failed",
                duration_ms=round((perf_counter() - started) * 1000),
                error_type="ResearchUnavailable",
            )
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

    async def _starting_page(self, url):
        url = public_url(url)
        if url not in self._seeds:
            self._reading_seed = True
            try:
                self._seeds[url] = await self._extract([url], [])
            finally:
                self._reading_seed = False
        return self._seeds[url]

    async def resolve(self, request: OutreachRequest) -> list[Candidate]:
        self.calls = 0
        docs = []
        if request.company_url:
            docs = await self._starting_page(request.company_url)
            self._seed_gaps = list(self.gaps)
            if not docs:
                return []
        if not docs or platform_host(domain(request.company_url)):
            docs += await self._search(f'"{request.company_name}" official company website')
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
        allowed_hosts = {domain(d.url) for d in docs} | {domain(link) for link in page_links(docs)}
        candidates = []
        for candidate in answer.candidates:
            try:
                host = domain(candidate.domain)
                if (
                    host in allowed_hosts
                    and not platform_host(host)
                    and host not in {c.domain for c in candidates}
                ):
                    candidates.append(candidate.model_copy(update={"domain": host}))
            except ValueError:
                continue
        return candidates[:4]

    async def fetch(self, request, company, window, supplied):
        self.calls = 0
        self.gaps = [
            "LinkedIn coverage is limited to publicly accessible content; absence is not proof of no activity."
        ] + self._seed_gaps
        documents = list(supplied)
        first_url = request.company_url or f"https://{company.domain}/"
        seed = await self._starting_page(first_url)
        documents.extend(seed)
        related = page_links(seed, company.domain)
        root = f"https://{company.domain}/"
        if root != public_url(first_url):
            related.append(root)
        documents.extend(await self._extract(list(dict.fromkeys(related))[:3], []))
        urls = []
        urls += [d.url for d in supplied if d.url and d.provenance == "search_snippet"]
        if request.recipient_url:
            urls.append(request.recipient_url)
        documents.extend(await self._extract(list(dict.fromkeys(urls))[:3], []))
        queries = [
            (f"site:{company.domain} about company founded employees leadership CEO", None),
            (f"site:{company.domain} blog news articles announcement", window),
            (f'"{company.name}" company news CEO announcement', window),
            (f'site:linkedin.com/posts "{company.name}" announcement award expansion', window),
            (
                f'"{request.recipient_name}" "{company.name}" "{request.recipient_role}" interview',
                window,
            ),
        ]
        for query, days in queries:
            if self.calls >= 12:
                break
            snippets = await self._search(query, days)
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

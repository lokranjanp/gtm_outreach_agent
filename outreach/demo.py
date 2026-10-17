"""Offline, fictional fixtures. Never present these as live company research."""

from datetime import date, timedelta

from outreach.models import (
    Candidate,
    Candidates,
    CompanyProfile,
    Document,
    Draft,
    Evidence,
    Fact,
    ProductProfile,
    Quality,
    ResearchReport,
    Signal,
    SignalSelection,
)

DEMO_PRODUCT = ProductProfile(
    id="demo-spendbridge",
    name="SpendBridge (demo)",
    value_proposition="Give finance teams one place to route purchase approvals and track supplier onboarding.",
    buyer_roles="CFO, Head of Finance, procurement leaders",
    benefits="Route purchase requests to the right approver; track supplier onboarding across teams; keep an approval audit trail.",
    proof_points="",
    website="https://spendbridge.example/",
)

SCENARIOS = {
    "Aster Labs": {
        "domain": "aster-labs.example",
        "industry": "B2B software",
        "size": "85 employees",
        "founded": "2021",
        "ceo": "Maya Rao",
        "recipient": "Alex Morgan",
        "role": "Head of Finance",
        "hook": "opened its first European office in Berlin",
        "second": "is hiring a finance operations lead to coordinate purchasing across regions",
        "connection": "Cross-region purchasing makes approval ownership a relevant question for finance.",
    },
    "Harbor Manufacturing": {
        "domain": "harbor-manufacturing.example",
        "industry": "Industrial manufacturing",
        "size": "3,200 employees",
        "founded": "1978",
        "ceo": "Daniel Lee",
        "recipient": "Sam Patel",
        "role": "Director of Procurement",
        "hook": "announced a supplier onboarding program across six production sites",
        "second": "published plans to standardize purchase approvals across its production sites",
        "connection": "A multi-site supplier program directly connects to procurement approval coordination.",
    },
}


class DemoResearch:
    async def resolve(self, request):
        return [
            Candidate(
                name=name, domain=row["domain"], description=f"Fictional {row['industry']} company"
            )
            for name, row in SCENARIOS.items()
            if request.company_name.casefold() == name.casefold()
        ]

    async def fetch(self, request, company, window, supplied):
        row = SCENARIOS[company.name]
        day = (date.today() - timedelta(days=14)).isoformat()
        profile = (
            f"{company.name} operates in {row['industry']}. Founded in {row['founded']}, "
            f"the company has {row['size']}. Its CEO is {row['ceo']}."
        )
        docs = [
            Document(
                id="demo-profile",
                url=f"https://{company.domain}/about",
                title="Company profile (fictional)",
                content=profile,
                provenance="demo",
            )
        ]
        for index, hook in enumerate((row["hook"], row["second"]), 1):
            docs.append(
                Document(
                    id=f"demo-event-{index}",
                    url=f"https://{company.domain}/news/{index}",
                    title=f"{company.name} update {index} (fictional)",
                    content=f"On {day}, {company.name} {hook}.",
                    published_at=day,
                    provenance="demo",
                )
            )
        return docs + supplied, [
            "DEMO: all companies, people, sources and events in this run are fictional.",
            "LinkedIn retrieval is not performed in offline demo mode.",
        ]


class DemoBrain:
    async def complete(self, schema, instruction, data):
        if schema is Candidates:
            return Candidates(candidates=[])
        if schema is ResearchReport:
            company = data["confirmed_company"]
            row = SCENARIOS[company["name"]]

            def facts(value):
                profile = next(d for d in data["documents"] if d["id"] == "demo-profile")
                return Fact(value=value, document_ids=["demo-profile"], quote=profile["content"])

            evidence = []
            for doc in data["documents"]:
                if not doc["id"].startswith("demo-event-"):
                    continue
                evidence.append(
                    Evidence(
                        id=doc["id"],
                        document_id=doc["id"],
                        title=doc["title"],
                        claim=doc["content"],
                        quote=doc["content"],
                        kind="news",
                        event_date=doc["published_at"],
                        date_quote=doc["published_at"],
                        company_match=True,
                        current=True,
                        confidence="high",
                    )
                )
            return ResearchReport(
                company=CompanyProfile(
                    name=company["name"],
                    domain=company["domain"],
                    industry=facts(row["industry"]),
                    employee_size=facts(row["size"]),
                    founding_year=facts(row["founded"]),
                    ceo=facts(row["ceo"]),
                ),
                evidence=evidence,
                gaps=[],
            )
        if schema is SignalSelection:
            row = SCENARIOS[data["company"]["name"]]
            ids = data.get("requested_ids") or [e["id"] for e in data["evidence"]][:1]
            return SignalSelection(
                recommendations=[
                    Signal(
                        evidence_id=key,
                        product_connection=row["connection"],
                        recipient_relevance=f"Purchasing coordination is relevant to the {data['request']['recipient_role']} role.",
                        company_context=f"{row['industry']}; {row['size']}; founded {row['founded']}. {row['connection']}",
                        strength="strong",
                    )
                    for key in ids
                ],
                rationale="A concrete operating change connects to the approved purchasing benefit.",
                rejected_reasons=[
                    "Do not add another hook unless it strengthens this same purchasing angle."
                ],
            )
        if schema is Draft:
            req, product = data["request"], data["product"]
            row = SCENARIOS[data["company"]["name"]]
            first = req["recipient_name"].split()[0]
            name = product["name"].removesuffix(" (demo)")
            opener = f"Hi {first},\n\nI saw that {data['company']['name']} {row['hook']}."
            if req["previously_contacted"]:
                opener += " I wanted to follow up with that development in mind."
            if len(data["selected"]) == 2:
                opener += f" I also noticed the company {row['second']}."
            pitch = f"{name} gives finance and procurement teams a shared place to route purchase approvals and track supplier onboarding."
            sentences = [
                "How are you coordinating that work across the teams involved?",
                "The announcement made me wonder how you are thinking about approval ownership as the program takes shape.",
                "A shared approval trail could be useful when different teams need visibility into the same supplier request.",
                "I would be interested to understand how that fits your priorities and whether a shared process would help.",
                "You would be best placed to judge whether that is relevant for your team at this stage.",
                "We could focus the conversation on your existing workflow and where approvals currently sit across the organization.",
                "That would give us a concrete starting point for discussing whether the approach fits the way your team works.",
                "If useful, I can walk through how a purchase request moves from submission to an approval decision.",
                "We could look at who submits a request, who approves it and how onboarding status gets shared with the people involved.",
                "It would also be useful to understand which handoffs you want to preserve, rather than assuming that everything needs to change.",
                "A short discussion could help us establish whether a shared view of those steps would be useful for your finance and procurement colleagues.",
            ]
            body = opener + "\n\n" + pitch
            cta = "\n\nWould a brief conversation about your approval workflow be useful?"
            for sentence in sentences:
                if len((body + cta).split()) >= req["min_words"]:
                    break
                if len((body + " " + sentence + cta).split()) <= req["max_words"]:
                    body += " " + sentence
            if len((body + cta).split()) > req["max_words"]:
                body = f"Hi {first},\n\nI saw that {data['company']['name']} {row['hook']}. "
                body += (
                    f"{name} routes purchase approvals and tracks supplier onboarding across teams."
                )
                cta = "\n\nWould comparing approval workflows be useful?"
                if len((body + cta).split()) < req["min_words"]:
                    body += " How are those requests handled today?"
            return Draft(
                subject=f"Purchasing coordination at {data['company']['name']}",
                body=body + cta,
                evidence_ids=[s["evidence_id"] for s in data["selected"]],
            )
        if schema is Quality:
            draft = data["draft"]
            row = SCENARIOS[data["company"]["name"]]
            body = draft["body"].casefold()
            grounded = not any(
                word in body
                for word in ("guarantee", "50%", "ignore previous", "we met", "you replied")
            )
            personalized = row["hook"].casefold() in body
            if len(data["selected"]) == 2:
                personalized = personalized and row["second"].casefold() in body
            relevant = "approv" in body and "supplier" in body
            return Quality(
                grounded=grounded,
                personalized=personalized,
                product_relevant=relevant,
                recipient_relevant=relevant,
                issues=[]
                if grounded and personalized and relevant
                else [
                    "Demo check: retain the specific company hook and purchasing connection; remove unsupported claims."
                ],
            )
        raise ValueError(f"Unsupported demo schema: {schema.__name__}")

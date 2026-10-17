"""Explicit, resumable research → signal selection → drafting state graph."""

import re
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from outreach.brain import CHECK, DRAFT, SELECT, Brain
from outreach.models import (
    Candidate,
    Document,
    Draft,
    FitAssessment,
    OutreachRequest,
    ProductProfile,
    Quality,
    ReviewResponse,
    SignalSelection,
    now,
)
from outreach.research import (
    ResearchProvider,
    deduplicate_documents,
    eligible_evidence,
    extract_report,
    public_url,
)
from outreach.signals import active_evidence, edit_signals, recipient_warnings
from outreach.telemetry import observed_node


class State(TypedDict, total=False):
    request: dict
    product: dict
    company: dict
    candidates: list[dict]
    documents: list[dict]
    supplied: list[dict]
    evidence: list[dict]
    gaps: list[str]
    selection: dict
    selected: list[dict]
    requested_ids: list[str]
    confirming_selection: bool
    research_window: int
    research_pass: int
    draft: dict
    versions: list[dict]
    quality: dict
    quality_issues: list[str]
    repair_count: int
    feedback: str
    phase: str
    status: str
    review_error: str
    pending_approval: bool
    removed_signals: list[dict]
    fit: dict
    quality_warnings: list[str]


def word_count(body: str) -> int:
    return len(re.findall(r"\S+", body))


def context(state: State) -> dict:
    selected_ids = {s["evidence_id"] for s in state.get("selected", [])}
    return {
        "today": date.today().isoformat(),
        "request": state["request"],
        "product": state["product"],
        "company": state.get("company", {}),
        "selected": state.get("selected", []),
        "evidence": [
            e
            for e in active_evidence(state.get("evidence", []), state.get("removed_signals", []))
            if e["id"] in selected_ids
        ],
        "fit": state.get("fit", {}),
        "feedback": state.get("feedback", ""),
        "previous_draft": state.get("draft"),
        "repair_issues": state.get("quality_issues", []),
    }


def review(kind: str, state: State, **payload) -> ReviewResponse:
    answer = interrupt({"kind": kind, "error": state.get("review_error", ""), **payload})
    return ReviewResponse.model_validate(answer)


def add_source(state: State, answer: ReviewResponse) -> list[dict]:
    supplied = list(state.get("supplied", []))
    if answer.source_url or answer.source_text:
        url = public_url(answer.source_url) if answer.source_url else ""
        supplied.append(
            Document(
                url=url,
                title=answer.source_title,
                content=answer.source_text[:24000],
                provenance="user_provided" if answer.source_text else "search_snippet",
            ).model_dump()
        )
    return supplied


def build_graph(brain: Brain, research: ResearchProvider, checkpointer):
    def collect_inputs(state: State):
        request = OutreachRequest.model_validate(state["request"])
        ProductProfile.model_validate(state["product"])
        missing = [
            label
            for field, label in (
                ("company_name", "Company name"),
                ("recipient_name", "Recipient name"),
                ("recipient_role", "Recipient role"),
            )
            if not getattr(request, field)
        ]
        if request.previously_contacted and not request.skip_history:
            if not request.previous_message:
                missing.append("Previous message or summary (or explicitly skip history)")
            if not request.contacted_date:
                missing.append("Approximate contact date (or explicitly skip history)")
        return Command(
            update={"phase": "Inputs", "review_error": "", "status": "running"},
            goto="input_review" if missing else "resolve_company",
        )

    def input_review(state: State):
        answer = review("inputs", state, request=state["request"])
        if answer.action == "cancel":
            return Command(update={"status": "cancelled"}, goto=END)
        request = OutreachRequest.model_validate(state["request"])
        for key in (
            "company_name",
            "recipient_name",
            "recipient_role",
            "previous_message",
            "contacted_date",
            "previous_reply",
            "skip_history",
        ):
            setattr(request, key, getattr(answer, key))
        return Command(update={"request": request.model_dump()}, goto="collect_inputs")

    async def resolve_company(state: State):
        candidates = await research.resolve(OutreachRequest.model_validate(state["request"]))
        updates = {"candidates": [c.model_dump() for c in candidates], "phase": "Company identity"}
        if (
            len(candidates) == 1
            and candidates[0].name.casefold() == state["request"]["company_name"].casefold()
        ):
            updates["company"] = candidates[0].model_dump()
            return Command(update=updates, goto="research_company")
        return Command(update=updates, goto="company_review")

    def company_review(state: State):
        answer = review("company", state, candidates=state.get("candidates", []))
        if answer.action == "cancel":
            return Command(update={"status": "cancelled"}, goto=END)
        chosen = next(
            (c for c in state.get("candidates", []) if c["domain"] == answer.candidate_domain), None
        )
        if not chosen and answer.source_url:
            from outreach.research import domain

            try:
                chosen = Candidate(
                    name=answer.company_name or state["request"]["company_name"],
                    domain=domain(answer.source_url),
                    description="Confirmed by user",
                ).model_dump()
            except ValueError as exc:
                return Command(update={"review_error": str(exc)}, goto="company_review")
        if chosen:
            updates = {"company": chosen, "review_error": ""}
            if answer.source_url:
                updates["request"] = {
                    **state["request"],
                    "company_url": public_url(answer.source_url),
                }
            return Command(update=updates, goto="research_company")
        return Command(
            update={"review_error": "Choose a company or enter its official website."},
            goto="company_review",
        )

    async def research_company(state: State):
        company = Candidate.model_validate(
            {
                key: state["company"].get(key, "Confirmed company")
                for key in ("name", "domain", "description")
            }
        )
        docs, gaps = await research.fetch(
            OutreachRequest.model_validate(state["request"]),
            company,
            state.get("research_window", 90),
            [Document.model_validate(d) for d in state.get("supplied", [])],
        )
        docs = deduplicate_documents(
            [Document.model_validate(d) for d in state.get("documents", [])] + docs
        )
        report = await extract_report(brain, company, docs, gaps)
        return {
            "company": report.company.model_dump(),
            "documents": [d.model_dump() for d in docs],
            "evidence": active_evidence(
                [e.model_dump() for e in report.evidence], state.get("removed_signals", [])
            ),
            "gaps": report.gaps,
            "selected": [],
            "requested_ids": [],
            "confirming_selection": False,
            "draft": {},
            "pending_approval": False,
            "research_pass": state.get("research_pass", 0) + 1,
            "phase": "Research complete",
        }

    async def select_signals(state: State):
        evidence = eligible_evidence(
            active_evidence(state.get("evidence", []), state.get("removed_signals", []))
        )
        requested = list(dict.fromkeys(state.get("requested_ids", [])))
        if requested:
            evidence = [e for e in evidence if e.id in requested]
        selection = (
            await brain.complete(
                SignalSelection,
                SELECT,
                {
                    "company": state["company"],
                    "product": state["product"],
                    "request": state["request"],
                    "evidence": [e.model_dump() for e in evidence],
                    "requested_ids": requested,
                    "today": date.today().isoformat(),
                },
            )
            if evidence
            else SignalSelection(
                recommendations=[],
                rationale="No credible, current evidence is eligible.",
                rejected_reasons=[],
            )
        )
        allowed = {e.id for e in evidence}
        selected = []
        for signal in selection.recommendations:
            if (
                signal.evidence_id in allowed
                and signal.strength == "strong"
                and signal.product_connection
                and signal.recipient_relevance
                and signal.company_context
                and signal.evidence_id not in {s.evidence_id for s in selected}
            ):
                selected.append(signal)
        selected = selected[:2]
        fit = selection.fit or FitAssessment(
            company_fit="strong" if selected else "unknown",
            recipient_fit="uncertain",
            explanation="Recipient fit has not yet been assessed.",
        )
        if fit.company_fit != "strong":
            selected = []
        selection.recommendations = selected
        updates = {
            "selection": selection.model_dump(),
            "phase": "Signal selection",
            "selected": [],
            "pending_approval": False,
            "fit": fit.model_dump(),
            "quality_warnings": recipient_warnings(fit.model_dump()),
        }
        if (
            not selected
            and state.get("research_window", 90) == 90
            and not requested
            and not state.get("removed_signals")
        ):
            return Command(update={**updates, "research_window": 365}, goto="research_company")
        if (
            state.get("confirming_selection")
            and requested
            and {s.evidence_id for s in selected} == set(requested)
            and len(requested) <= 2
        ):
            return Command(
                update={
                    **updates,
                    "selected": [s.model_dump() for s in selected],
                    "confirming_selection": False,
                    "repair_count": 0,
                    "review_error": "",
                },
                goto="draft_email",
            )
        if state.get("confirming_selection"):
            updates["review_error"] = (
                "Those signals do not all have a defensible company/product connection. Choose again or add evidence."
            )
        return Command(update=updates, goto="signal_review")

    def signal_review(state: State):
        answer = review("signals", state)
        if answer.action == "cancel":
            return Command(update={"status": "cancelled"}, goto=END)
        if answer.action in {"remove", "restore"}:
            return Command(
                update=edit_signals(state, answer.action, answer.evidence_ids),
                goto="select_signals",
            )
        if answer.action == "research":
            try:
                supplied = add_source(state, answer)
            except ValueError as exc:
                return Command(update={"review_error": str(exc)}, goto="signal_review")
            return Command(
                update={
                    "supplied": supplied,
                    "review_error": "",
                    "feedback": answer.feedback,
                    "research_window": 365,
                },
                goto="research_company",
            )
        if not 1 <= len(set(answer.evidence_ids)) <= 2:
            return Command(
                update={"review_error": "Choose one or two signals before approving."},
                goto="signal_review",
            )
        return Command(
            update={
                "requested_ids": answer.evidence_ids,
                "confirming_selection": True,
                "feedback": answer.feedback,
                "review_error": "",
            },
            goto="select_signals",
        )

    async def draft_email(state: State):
        draft = await brain.complete(Draft, DRAFT, context(state))
        version = {
            "created_at": now(),
            "origin": "generated",
            "draft": draft.model_dump(),
            "feedback": state.get("feedback", ""),
        }
        return {
            "draft": draft.model_dump(),
            "versions": state.get("versions", []) + [version],
            "phase": "Drafting",
            "pending_approval": False,
        }

    async def check_quality(state: State):
        draft = Draft.model_validate(state["draft"])
        quality = await brain.complete(
            Quality, CHECK, {**context(state), "draft": draft.model_dump()}
        )
        issues = list(quality.issues)
        fit = quality.fit or FitAssessment.model_validate(
            state.get("fit")
            or {
                "company_fit": "strong"
                if quality.product_relevant and state.get("selected")
                else "unknown",
                "recipient_fit": "aligned" if quality.recipient_relevant else "uncertain",
            }
        )
        warnings = list(quality.warnings) + recipient_warnings(fit.model_dump())
        if not quality.recipient_relevant:
            warnings.append(
                "Recipient fit: this person may not be relevant to the purchasing decision; the email is based on company fit."
            )
        if fit.company_fit != "strong":
            issues.append("The company does not have a defensible product fit.")
        req = OutreachRequest.model_validate(state["request"])
        count = word_count(draft.body)
        if not req.min_words <= count <= req.max_words:
            issues.append(f"Body has {count} words; target is {req.min_words}–{req.max_words}.")
        selected_ids = {s["evidence_id"] for s in state["selected"]}
        if set(draft.evidence_ids) != selected_ids:
            issues.append("Draft must use exactly the human-approved signals.")
        for key in ("grounded", "product_relevant", "personalized"):
            if not getattr(quality, key):
                issues.append(f"Quality criterion failed: {key.replace('_', ' ')}.")
        updates = {
            "quality": quality.model_dump(),
            "quality_issues": list(dict.fromkeys(issues)),
            "phase": "Draft review",
            "review_error": "",
            "quality_warnings": list(dict.fromkeys(warnings)),
            "fit": fit.model_dump(),
        }
        if not issues and state.get("pending_approval"):
            return Command(update={**updates, "status": "approved", "phase": "Complete"}, goto=END)
        # User edits are never silently overwritten; surface issues for review.
        if issues and not state.get("pending_approval") and state.get("repair_count", 0) < 2:
            return Command(
                update={**updates, "repair_count": state.get("repair_count", 0) + 1},
                goto="draft_email",
            )
        return Command(update=updates, goto="draft_review")

    def draft_review(state: State):
        answer = review("draft", state)
        if answer.action == "cancel":
            return Command(update={"status": "cancelled"}, goto=END)
        if answer.action in {"remove", "restore"}:
            updates = edit_signals(state, answer.action, answer.evidence_ids)
            return Command(
                update=updates,
                goto="select_signals" if updates.get("draft") == {} else "draft_review",
            )
        if answer.action == "signals":
            return Command(
                update={
                    "selected": [],
                    "requested_ids": [],
                    "confirming_selection": False,
                    "pending_approval": False,
                    "review_error": "",
                },
                goto="select_signals",
            )
        if answer.action == "revise":
            return Command(
                update={
                    "feedback": answer.feedback,
                    "repair_count": 0,
                    "quality_issues": [],
                    "pending_approval": False,
                },
                goto="draft_email",
            )
        try:
            draft = Draft(
                subject=answer.subject,
                body=answer.body,
                evidence_ids=[s["evidence_id"] for s in state["selected"]],
            )
        except ValueError:
            return Command(
                update={"review_error": "Subject and body are required."}, goto="draft_review"
            )
        versions = state.get("versions", [])
        if draft.model_dump() != state["draft"]:
            versions = versions + [
                {
                    "created_at": now(),
                    "origin": "human edit",
                    "draft": draft.model_dump(),
                    "feedback": "",
                }
            ]
        return Command(
            update={
                "draft": draft.model_dump(),
                "versions": versions,
                "pending_approval": True,
                "review_error": "",
            },
            goto="check_quality",
        )

    graph = StateGraph(State)
    nodes = {
        "collect_inputs": collect_inputs,
        "input_review": input_review,
        "resolve_company": resolve_company,
        "company_review": company_review,
        "research_company": research_company,
        "select_signals": select_signals,
        "signal_review": signal_review,
        "draft_email": draft_email,
        "check_quality": check_quality,
        "draft_review": draft_review,
    }
    for name, node in nodes.items():
        graph.add_node(name, observed_node(name, node))
    graph.add_edge(START, "collect_inputs")
    graph.add_edge("research_company", "select_signals")
    graph.add_edge("draft_email", "check_quality")
    return graph.compile(checkpointer=checkpointer)

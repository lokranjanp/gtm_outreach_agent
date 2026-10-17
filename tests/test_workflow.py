from dataclasses import dataclass

import pytest

from outreach.demo import DEMO_PRODUCT, SCENARIOS, DemoBrain, DemoResearch
from outreach.models import Candidate, OutreachRequest, Quality, ReviewResponse, SignalSelection
from outreach.runner import Runner
from outreach.storage import Store
from outreach.workflow import word_count


class CountingResearch(DemoResearch):
    def __init__(self):
        self.resolve_calls = 0
        self.fetch_calls = 0

    async def resolve(self, request):
        self.resolve_calls += 1
        return await super().resolve(request)

    async def fetch(self, *args):
        self.fetch_calls += 1
        return await super().fetch(*args)


@dataclass
class Harness:
    runner: Runner
    research: CountingResearch
    store: Store

    def start(self, company="Aster Labs", **kwargs):
        row = SCENARIOS[company]
        request = OutreachRequest(
            company_name=company,
            recipient_name=row["recipient"],
            recipient_role=row["role"],
            **kwargs,
        )
        run_id = self.runner.create(request, DEMO_PRODUCT, "demo")
        result = self.runner.start(run_id, request, DEMO_PRODUCT)
        return run_id, result

    def draft(self, run_id, result, count=1):
        ids = [s["evidence_id"] for s in result["state"]["selection"]["recommendations"]]
        if count == 2:
            ids = [e["id"] for e in result["state"]["evidence"]][:2]
        return self.runner.resume(run_id, ReviewResponse(action="approve", evidence_ids=ids))


@pytest.fixture
def harness(tmp_path):
    store, research, brain = Store(tmp_path), CountingResearch(), DemoBrain()
    return Harness(Runner(store, service_factory=lambda mode: (brain, research)), research, store)


@pytest.mark.parametrize("company", list(SCENARIOS))
@pytest.mark.parametrize("bounds", [(40, 44), (60, 90), (90, 140), (140, 200), (225, 250)])
def test_real_graph_two_review_gates_and_approval(harness, company, bounds):
    run_id, result = harness.start(company, min_words=bounds[0], max_words=bounds[1])
    assert result["interrupts"][0]["kind"] == "signals"
    assert not result["state"].get("draft")
    result = harness.draft(run_id, result)
    assert result["interrupts"][0]["kind"] == "draft"
    state = result["state"]
    assert not state["quality_issues"]
    assert bounds[0] <= word_count(state["draft"]["body"]) <= bounds[1]
    assert SCENARIOS[company]["hook"] in state["draft"]["body"]
    result = harness.runner.resume(
        run_id,
        ReviewResponse(
            action="approve",
            **{
                "subject": state["draft"]["subject"],
                "body": state["draft"]["body"],
            },
        ),
    )
    assert result["state"]["status"] == "approved"
    assert not result["interrupts"] and not result["next"]
    assert harness.store.run(run_id)["status"] == "approved"


def test_resume_after_restart_does_not_repeat_research(harness):
    run_id, result = harness.start()
    runner = Runner(Store(harness.store.directory), service_factory=harness.runner.service_factory)
    snapshot = runner.snapshot(run_id)
    assert snapshot["interrupts"][0]["kind"] == "signals"
    assert harness.research.fetch_calls == 1
    ids = [s["evidence_id"] for s in result["state"]["selection"]["recommendations"]]
    result = runner.resume(run_id, ReviewResponse(action="approve", evidence_ids=ids))
    assert result["interrupts"][0]["kind"] == "draft"
    assert harness.research.fetch_calls == 1


def test_missing_input_and_followup_context_interrupt_before_research(harness):
    req = OutreachRequest(company_name="Aster Labs", previously_contacted=True)
    run_id = harness.runner.create(req, DEMO_PRODUCT, "demo")
    result = harness.runner.start(run_id, req, DEMO_PRODUCT)
    assert result["interrupts"][0]["kind"] == "inputs"
    assert harness.research.resolve_calls == 0
    result = harness.runner.resume(
        run_id,
        ReviewResponse(
            action="submit",
            company_name="Aster Labs",
            recipient_name="Alex Morgan",
            recipient_role="Head of Finance",
        ),
    )
    assert result["interrupts"][0]["kind"] == "inputs"
    result = harness.runner.resume(
        run_id,
        ReviewResponse(
            action="submit",
            company_name="Aster Labs",
            recipient_name="Alex Morgan",
            recipient_role="Head of Finance",
            skip_history=True,
        ),
    )
    result = harness.draft(run_id, result)
    body = result["state"]["draft"]["body"].lower()
    assert "follow up" in body
    assert "you replied" not in body and "we met" not in body


def test_company_ambiguity_pauses_before_research(harness):
    async def ambiguous(request):
        return [
            Candidate(name=name, domain=row["domain"], description=row["industry"])
            for name, row in SCENARIOS.items()
        ]

    harness.research.resolve = ambiguous
    run_id, result = harness.start()
    assert result["interrupts"][0]["kind"] == "company"
    assert harness.research.fetch_calls == 0
    result = harness.runner.resume(
        run_id, ReviewResponse(action="submit", candidate_domain="aster-labs.example")
    )
    assert result["state"]["company"]["name"] == "Aster Labs"
    assert result["interrupts"][0]["kind"] == "signals"


def test_no_relevant_signals_expands_then_pauses_without_generic_draft(harness):
    class WeakBrain(DemoBrain):
        async def complete(self, schema, instruction, data):
            if schema is SignalSelection:
                return SignalSelection(
                    recommendations=[],
                    rationale="Award does not relate to this product.",
                    rejected_reasons=[],
                )
            return await super().complete(schema, instruction, data)

    harness.runner.service_factory = lambda mode: (WeakBrain(), harness.research)
    run_id, result = harness.start()
    assert harness.research.fetch_calls == 2
    assert result["state"]["research_window"] == 365
    assert result["interrupts"][0]["kind"] == "signals"
    assert not result["state"]["draft"]
    result = harness.runner.resume(
        run_id, ReviewResponse(action="approve", evidence_ids=["made-up-id"])
    )
    assert result["interrupts"][0]["kind"] == "signals"
    assert not result["state"]["draft"]


def test_two_signals_and_returning_to_selection_invalidates_approval(harness):
    run_id, result = harness.start()
    result = harness.draft(run_id, result, count=2)
    assert len(result["state"]["selected"]) == 2
    assert len(result["state"]["draft"]["evidence_ids"]) == 2
    result = harness.runner.resume(run_id, ReviewResponse(action="signals"))
    assert not result["state"]["selected"]
    assert not result["state"]["pending_approval"]
    assert result["interrupts"][0]["kind"] == "signals"
    assert harness.research.fetch_calls == 1


def test_user_edits_checked_and_not_silently_overwritten(harness):
    run_id, result = harness.start()
    result = harness.draft(run_id, result)
    draft = result["state"]["draft"]
    edited = draft["body"] + " We guarantee 50% savings."
    result = harness.runner.resume(
        run_id, ReviewResponse(action="approve", subject=draft["subject"], body=edited)
    )
    assert result["interrupts"][0]["kind"] == "draft"
    assert result["state"]["draft"]["body"] == edited
    assert result["state"]["quality_issues"]
    assert result["state"]["status"] != "approved"
    assert result["state"]["versions"][-1]["origin"] == "human edit"
    result = harness.runner.resume(
        run_id, ReviewResponse(action="approve", subject=draft["subject"], body=draft["body"])
    )
    assert result["state"]["status"] == "approved"


def test_automatic_quality_repairs_are_bounded(harness):
    class FailingQualityBrain(DemoBrain):
        async def complete(self, schema, instruction, data):
            if schema is Quality:
                return Quality(
                    grounded=False,
                    product_relevant=True,
                    recipient_relevant=True,
                    personalized=False,
                    issues=["Unsupported opening."],
                )
            return await super().complete(schema, instruction, data)

    harness.runner.service_factory = lambda mode: (FailingQualityBrain(), harness.research)
    run_id, result = harness.start()
    result = harness.draft(run_id, result)
    assert len(result["state"]["versions"]) == 3
    assert result["state"]["repair_count"] == 2
    assert result["interrupts"][0]["kind"] == "draft"


def test_regeneration_preserves_history_and_approved_signals(harness):
    run_id, result = harness.start()
    result = harness.draft(run_id, result)
    selected = result["state"]["selected"]
    result = harness.runner.resume(
        run_id, ReviewResponse(action="revise", feedback="Use a softer CTA.")
    )
    assert result["state"]["selected"] == selected
    assert len(result["state"]["versions"]) == 2
    assert result["state"]["versions"][-1]["feedback"] == "Use a softer CTA."


def test_product_profiles_persist_and_existing_runs_snapshot_profile(harness):
    harness.store.save_product(DEMO_PRODUCT)
    run_id, result = harness.start()
    changed = DEMO_PRODUCT.model_copy(
        update={"value_proposition": "A different approved value proposition."}
    )
    harness.store.save_product(changed)
    assert (
        Store(harness.store.directory).products()[0].value_proposition == changed.value_proposition
    )
    assert (
        harness.runner.snapshot(run_id)["state"]["product"]["value_proposition"]
        == DEMO_PRODUCT.value_proposition
    )


def test_cancellation_retains_research(harness):
    run_id, result = harness.start()
    result = harness.runner.resume(run_id, ReviewResponse(action="cancel"))
    assert result["state"]["status"] == "cancelled"
    assert result["state"]["evidence"]
    assert not result["next"]

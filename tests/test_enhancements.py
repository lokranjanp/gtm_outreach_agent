import asyncio
import logging
from logging.handlers import RotatingFileHandler

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from outreach.demo import DEMO_PRODUCT, SCENARIOS, DemoBrain, DemoResearch
from outreach.models import FitAssessment, OutreachRequest, Quality, ReviewResponse
from outreach.runner import Runner
from outreach.signals import active_evidence
from outreach.storage import Store
from outreach.telemetry import configure_logging, emit, logger
from outreach.workflow import build_graph


def setup(tmp_path, brain=None, research=None, role="Head of Finance"):
    brain, research = brain or DemoBrain(), research or DemoResearch()
    runner = Runner(Store(tmp_path), service_factory=lambda mode: (brain, research))
    request = OutreachRequest(
        company_name="Aster Labs", recipient_name="Alex Morgan", recipient_role=role
    )
    run_id = runner.create(request, DEMO_PRODUCT, "demo")
    return runner, request, run_id


def draft(runner, request, run_id):
    result = runner.start(run_id, request, DEMO_PRODUCT)
    ids = [s["evidence_id"] for s in result["state"]["selection"]["recommendations"]]
    return runner.resume(run_id, ReviewResponse(action="approve", evidence_ids=ids))


def approve(runner, run_id, result):
    email = result["state"]["draft"]
    return runner.resume(
        run_id, ReviewResponse(action="approve", subject=email["subject"], body=email["body"])
    )


def test_progress_arrives_while_research_is_still_running(tmp_path):
    events = []

    class SlowResearch(DemoResearch):
        async def fetch(self, *args):
            await asyncio.sleep(0.02)
            assert any(e["stage"] == 1 and e["status"] == "running" for e in events)
            return await super().fetch(*args)

    runner, request, run_id = setup(tmp_path, research=SlowResearch())
    result = runner.start(run_id, request, DEMO_PRODUCT, events.append)
    assert result["interrupts"][0]["kind"] == "signals"
    assert events[-1]["stage"] == 2 and events[-1]["status"] == "waiting"
    assert all(e["run_id"] == run_id for e in events)
    assert any(e["status"] == "completed" and e["duration_ms"] is not None for e in events)


def test_removal_survives_research_retry_restart_and_restores(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = runner.start(run_id, request, DEMO_PRODUCT)
    removed_id = result["state"]["evidence"][0]["id"]
    result = runner.resume(run_id, ReviewResponse(action="remove", evidence_ids=[removed_id]))
    assert removed_id not in {e["id"] for e in result["state"]["evidence"]}
    result = runner.resume(run_id, ReviewResponse(action="research"))
    assert removed_id not in {e["id"] for e in result["state"]["evidence"]}
    new_runner = Runner(Store(tmp_path), service_factory=runner.service_factory)
    state = new_runner.snapshot(run_id)["state"]
    assert state["removed_signals"][0]["signal"]["id"] == removed_id
    result = new_runner.resume(run_id, ReviewResponse(action="restore", evidence_ids=[removed_id]))
    assert not result["state"]["removed_signals"]
    assert removed_id in {e["id"] for e in result["state"]["evidence"]}


def test_changed_document_ids_and_quotes_do_not_restore_same_claim(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = runner.start(run_id, request, DEMO_PRODUCT)
    item = result["state"]["evidence"][0]
    changed = {
        **item,
        "id": "different-id",
        "document_id": "different-document",
        "quote": "A new excerpt for the same underlying company claim.",
    }
    assert not active_evidence([changed], [{"signal": item}])


def test_removing_selected_signal_invalidates_draft_and_excludes_it_from_next_email(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = draft(runner, request, run_id)
    removed_id = result["state"]["selected"][0]["evidence_id"]
    result = runner.resume(run_id, ReviewResponse(action="remove", evidence_ids=[removed_id]))
    assert not result["state"]["draft"] and not result["state"]["pending_approval"]
    assert result["state"]["versions"][0]["outdated"]
    assert result["interrupts"][0]["kind"] == "signals"
    ids = [s["evidence_id"] for s in result["state"]["selection"]["recommendations"]]
    result = runner.resume(run_id, ReviewResponse(action="approve", evidence_ids=ids))
    body = result["state"]["draft"]["body"]
    assert SCENARIOS["Aster Labs"]["hook"] not in body
    assert SCENARIOS["Aster Labs"]["second"] in body
    assert not result["state"]["quality_issues"]


def test_removing_unselected_signal_keeps_current_draft(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = draft(runner, request, run_id)
    old_draft = result["state"]["draft"]
    unused = next(
        e["id"] for e in result["state"]["evidence"] if e["id"] not in old_draft["evidence_ids"]
    )
    result = runner.resume(run_id, ReviewResponse(action="remove", evidence_ids=[unused]))
    assert result["interrupts"][0]["kind"] == "draft"
    assert result["state"]["draft"] == old_draft


def test_remove_all_signals_waits_instead_of_researching_or_drafting(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = runner.start(run_id, request, DEMO_PRODUCT)
    ids = [e["id"] for e in result["state"]["evidence"]]
    result = runner.resume(run_id, ReviewResponse(action="remove", evidence_ids=ids))
    assert not result["state"]["evidence"] and not result["state"].get("draft")
    assert result["state"]["research_pass"] == 1
    assert result["interrupts"][0]["kind"] == "signals"


def test_approved_run_reopens_without_repeating_research(tmp_path):
    runner, request, run_id = setup(tmp_path)
    result = approve(runner, run_id, draft(runner, request, run_id))
    assert result["state"]["status"] == "approved"
    result = runner.reopen(run_id)
    assert result["state"]["status"] != "approved"
    assert result["state"]["research_pass"] == 1
    assert result["interrupts"][0]["kind"] == "signals"


def test_misaligned_recipient_is_a_warning_not_a_drafting_or_approval_blocker(tmp_path):
    runner, request, run_id = setup(tmp_path, role="Head of Engineering")
    result = draft(runner, request, run_id)
    state = result["state"]
    assert state["fit"]["company_fit"] == "strong"
    assert state["fit"]["recipient_fit"] == "misaligned"
    assert state["quality_warnings"] and not state["quality_issues"]
    assert len(state["versions"]) == 1  # No repair loop for a recipient warning.
    assert "engineering" not in state["draft"]["body"].lower()
    assert approve(runner, run_id, result)["state"]["status"] == "approved"


@pytest.mark.parametrize("company_fit", ["weak", "unknown"])
def test_company_fit_still_blocks_approval(tmp_path, company_fit):
    class BadFit(DemoBrain):
        async def complete(self, schema, instruction, data):
            answer = await super().complete(schema, instruction, data)
            if schema is Quality:
                answer.fit = FitAssessment(company_fit=company_fit, recipient_fit="misaligned")
            return answer

    runner, request, run_id = setup(tmp_path, brain=BadFit())
    result = draft(runner, request, run_id)
    assert result["state"]["quality_issues"]
    assert approve(runner, run_id, result)["state"]["status"] != "approved"


def test_invented_role_claims_still_fail_grounding(tmp_path):
    class BadGrounding(DemoBrain):
        async def complete(self, schema, instruction, data):
            answer = await super().complete(schema, instruction, data)
            if schema is Quality:
                answer.grounded = False
                answer.issues = ["Unsupported claim that the recipient owns procurement."]
            return answer

    runner, request, run_id = setup(tmp_path, brain=BadGrounding(), role="Head of Engineering")
    result = draft(runner, request, run_id)
    assert result["state"]["quality_issues"]
    assert approve(runner, run_id, result)["state"]["status"] != "approved"


def test_failures_log_only_safe_categories_and_show_failed_progress(tmp_path):
    class Broken(DemoResearch):
        async def fetch(self, *args):
            raise RuntimeError(
                "SECRET-KEY-EXAMPLE private correspondence https://example.com/?token=private"
            )

    runner, request, run_id = setup(tmp_path, research=Broken())
    events = []
    with pytest.raises(RuntimeError):
        runner.start(run_id, request, DEMO_PRODUCT, events.append)
    text = (tmp_path / "logs" / "outreach.log").read_text()
    assert "RuntimeError" in text and run_id in text
    assert (
        "SECRET-KEY-EXAMPLE" not in text and "correspondence" not in text and "token=" not in text
    )
    assert events[-1]["status"] == "failed" and events[-1]["stage"] == 1


def test_logging_handlers_are_idempotent_and_files_rotate(tmp_path):
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    assert len(logger.handlers) == 2
    handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
    assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3
    handler.maxBytes = 300
    for _ in range(10):
        emit("research_company", "completed", count=2)
    assert (tmp_path / "logs" / "outreach.log.1").exists()
    assert not logger.propagate and logger.level == logging.INFO


def test_old_saved_draft_without_new_fields_remains_reviewable(tmp_path):
    runner, request, run_id = setup(tmp_path)
    state = draft(runner, request, run_id)["state"]
    for key in ("removed_signals", "fit", "quality_warnings"):
        state.pop(key, None)
    legacy_id = runner.create(request, DEMO_PRODUCT, "demo")

    async def save_old_checkpoint():
        async with AsyncSqliteSaver.from_conn_string(str(runner.store.checkpoints)) as saver:
            graph = build_graph(DemoBrain(), DemoResearch(), saver)
            await graph.aupdate_state(
                {"configurable": {"thread_id": legacy_id}}, state, as_node="draft_email"
            )

    asyncio.run(save_old_checkpoint())
    result = runner.retry(legacy_id)
    assert result["interrupts"][0]["kind"] == "draft"
    assert not result["state"]["quality_issues"]
    assert approve(runner, legacy_id, result)["state"]["status"] == "approved"

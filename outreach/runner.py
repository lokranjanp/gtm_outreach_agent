"""Open loop-local resources per graph segment; persist across Streamlit reruns."""

import asyncio
from collections.abc import Callable
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from outreach.brain import GeminiBrain, OpenAIBrain
from outreach.config import resolve_config
from outreach.demo import DemoBrain, DemoResearch
from outreach.models import OutreachRequest, ProductProfile, ReviewResponse
from outreach.research import MCPResearch
from outreach.storage import Store
from outreach.telemetry import NODE_STAGES, configure_logging, emit, log_configuration, run_scope
from outreach.workflow import build_graph


class MissingCredentials:
    def __init__(self, message):
        self.message = message

    async def complete(self, *args, **kwargs):
        raise ValueError(self.message)

    async def resolve(self, *args, **kwargs):
        raise ValueError(self.message)

    async def fetch(self, *args, **kwargs):
        raise ValueError(self.message)


class Runner:
    def __init__(self, store: Store, credentials: dict | None = None, service_factory=None):
        self.store = store
        self.credentials = credentials or {}
        self.service_factory = service_factory
        configure_logging(store.directory)

    def services(self, mode):
        if self.service_factory:
            return self.service_factory(mode)
        if mode == "demo":
            return DemoBrain(), DemoResearch()
        config = resolve_config(self.credentials)
        if not config.ready:
            unavailable = MissingCredentials(config.problem)
            return unavailable, unavailable
        provider = GeminiBrain if config.provider == "gemini" else OpenAIBrain
        brain = provider(config.model_key, config.model)
        return brain, MCPResearch(config.tavily_key, brain)

    def create(self, request: OutreachRequest, product: ProductProfile, mode: str) -> str:
        if mode not in {"demo", "live"}:
            raise ValueError("Unknown run mode.")
        run_id = str(uuid4())
        self.store.create_run(run_id, request.company_name, mode)
        return run_id

    async def _segment(
        self,
        run_id,
        graph_input=None,
        progress: Callable | None = None,
        read_only=False,
        reopen=False,
    ):
        with run_scope(run_id):
            return await self._execute_segment(run_id, graph_input, progress, read_only, reopen)

    async def _execute_segment(self, run_id, graph_input, progress, read_only, reopen):
        run = self.store.run(run_id)
        brain, research = self.services(run["mode"])
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
        async with AsyncSqliteSaver.from_conn_string(str(self.store.checkpoints)) as saver:
            graph = build_graph(brain, research, saver)
            if reopen:
                previous = await graph.aget_state(config)
                if previous.values.get("status") != "approved":
                    raise ValueError("Only an approved run can be reopened.")
                await graph.aupdate_state(
                    config,
                    {
                        "status": "running",
                        "pending_approval": False,
                        "requested_ids": [],
                        "confirming_selection": False,
                        "review_error": "",
                    },
                    as_node="research_company",
                )
                emit("reopened", "completed", stage=2)
            if not read_only:
                last_event = {"stage": 0}
                try:
                    runtime = resolve_config(self.credentials)
                    log_configuration(
                        "demo" if run["mode"] == "demo" else runtime.provider,
                        "offline" if run["mode"] == "demo" else runtime.model,
                    )
                    async for mode, update in graph.astream(
                        graph_input, config, stream_mode=["updates", "custom"]
                    ):
                        if mode == "custom":
                            last_event = update
                            if progress:
                                progress(update)
                except Exception as exc:
                    failed = emit(
                        "failure",
                        "failed",
                        stage=last_event["stage"],
                        error_type=type(exc).__name__,
                    )
                    if progress:
                        progress(failed)
                    self.store.update_run(run_id, "error")
                    raise
            snapshot = await graph.aget_state(config)
            pauses = [item.value for task in snapshot.tasks for item in task.interrupts]
            result = {
                "state": snapshot.values,
                "interrupts": pauses,
                "next": list(snapshot.next),
                "run": run,
            }
            if not read_only:
                status = snapshot.values.get("status", "running")
                if pauses:
                    status = "awaiting_review"
                self.store.update_run(
                    run_id, status, snapshot.values.get("request", {}).get("company_name")
                )
                next_node = snapshot.next[0] if snapshot.next else "check_quality"
                final = emit(
                    next_node,
                    "waiting"
                    if pauses
                    else status
                    if status in {"approved", "cancelled"}
                    else "completed",
                    stage=NODE_STAGES.get(next_node, 4),
                )
                if progress:
                    progress(final)
            return result

    def start(self, run_id: str, request: OutreachRequest, product: ProductProfile, progress=None):
        return asyncio.run(
            self._segment(
                run_id,
                {
                    "request": request.model_dump(),
                    "product": product.model_dump(),
                    "status": "running",
                    "research_window": 90,
                    "research_pass": 0,
                    "versions": [],
                    "supplied": [],
                    "documents": [],
                    "evidence": [],
                    "repair_count": 0,
                    "review_error": "",
                    "removed_signals": [],
                    "quality_warnings": [],
                },
                progress,
            )
        )

    def resume(self, run_id: str, response: ReviewResponse, progress=None):
        return asyncio.run(self._segment(run_id, Command(resume=response.model_dump()), progress))

    def retry(self, run_id: str, progress=None):
        return asyncio.run(self._segment(run_id, None, progress))

    def snapshot(self, run_id: str):
        return asyncio.run(self._segment(run_id, read_only=True))

    def reopen(self, run_id: str, progress=None):
        return asyncio.run(self._segment(run_id, progress=progress, reopen=True))

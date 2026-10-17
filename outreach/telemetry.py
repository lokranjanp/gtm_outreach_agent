"""Content-free operational events shared by live progress and local logs."""

import inspect
import json
import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Literal

from langgraph.config import get_stream_writer
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel, Field

from outreach.models import now

STAGES = ("Company identity", "Company research", "Signal selection", "Drafting", "Review")
NODE_STAGES = {
    "collect_inputs": 0,
    "input_review": 0,
    "resolve_company": 0,
    "company_review": 0,
    "research_company": 1,
    "select_signals": 2,
    "signal_review": 2,
    "draft_email": 3,
    "check_quality": 4,
    "draft_review": 4,
}
ACTIVITIES = {
    "collect_inputs": "Checking outreach inputs",
    "input_review": "Waiting for missing inputs",
    "resolve_company": "Identifying the company",
    "company_review": "Waiting for company confirmation",
    "research_company": "Researching the company",
    "select_signals": "Choosing relevant signals",
    "signal_review": "Waiting for signal approval",
    "draft_email": "Writing the email",
    "check_quality": "Checking the draft",
    "draft_review": "Waiting for draft review",
    "reading_page": "Reading the supplied company page",
    "reading_sources": "Reading source pages",
    "searching": "Searching public sources",
    "retrying": "Retrying a research operation",
    "model": "Processing evidence with the model",
    "reopened": "Reopened for editing",
    "failure": "Workflow step failed",
}
run_context = ContextVar("outreach_run", default="")
stage_context = ContextVar("outreach_stage", default=0)
logger = logging.getLogger("outreach.operations")
_lock = threading.Lock()


class ProgressEvent(BaseModel):
    run_id: str = ""
    stage: int = Field(ge=0, le=4)
    activity: str
    status: Literal["running", "completed", "waiting", "failed", "approved", "cancelled"]
    timestamp: str = Field(default_factory=now)
    duration_ms: int | None = None
    count: int | None = None
    attempt: int | None = None
    error_type: str | None = None
    milestone: bool = False


def configure_logging(data_dir: str | Path):
    path = Path(data_dir).resolve() / "logs" / "outreach.log"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        if existing and existing[0].baseFilename == str(path):
            return
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        file_handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        console = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for handler in (file_handler, console):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


@contextmanager
def run_scope(run_id):
    token = run_context.set(run_id)
    try:
        yield
    finally:
        run_context.reset(token)


def log_configuration(provider, model):
    safe_model = model if re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", model) else "custom"
    logger.info(
        json.dumps(
            {
                "event": "run_configuration",
                "run_id": run_context.get(),
                "provider": provider if provider in {"gemini", "openai", "demo"} else "unknown",
                "model": safe_model,
            }
        )
    )


def emit(
    activity: str,
    status: str = "running",
    *,
    stage: int | None = None,
    duration_ms: int | None = None,
    count: int | None = None,
    attempt: int | None = None,
    error_type: str | None = None,
) -> dict:
    # Only predefined activity labels enter events. Never log tool arguments or exceptions.
    event = ProgressEvent(
        run_id=run_context.get(),
        stage=stage_context.get() if stage is None else stage,
        activity=ACTIVITIES.get(activity, "Processing workflow step"),
        status=status,
        duration_ms=duration_ms,
        count=count,
        attempt=attempt,
        error_type=re.sub(r"[^A-Za-z0-9_]", "", error_type or "")[:80] or None,
        milestone=activity in NODE_STAGES,
    ).model_dump()
    level = logging.ERROR if status == "failed" else logging.INFO
    logger.log(level, json.dumps(event, ensure_ascii=False))
    try:
        get_stream_writer()(event)
    except RuntimeError:
        pass  # Providers can also be used outside a graph (e.g. connection checks).
    return event


def observed_node(name, node):
    @wraps(node)
    async def observed(state):
        token = stage_context.set(NODE_STAGES[name])
        started = perf_counter()
        emit(name)
        try:
            result = node(state)
            if inspect.isawaitable(result):
                result = await result
            emit(name, "completed", duration_ms=round((perf_counter() - started) * 1000))
            return result
        except GraphInterrupt:
            emit(name, "waiting")
            raise
        except Exception as exc:
            emit(
                name,
                "failed",
                duration_ms=round((perf_counter() - started) * 1000),
                error_type=type(exc).__name__,
            )
            raise
        finally:
            stage_context.reset(token)

    return observed

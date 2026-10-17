"""Serializable contracts shared by the graph, research providers and UI."""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ProductProfile(Model):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=150)
    value_proposition: str = Field(min_length=10, max_length=3000)
    buyer_roles: str = Field(min_length=2, max_length=1000)
    benefits: str = Field(min_length=10, max_length=3000)
    proof_points: str = Field(default="", max_length=3000)
    website: str = ""


class OutreachRequest(Model):
    company_name: str = ""
    company_url: str = ""
    recipient_name: str = ""
    recipient_role: str = ""
    recipient_url: str = ""
    previously_contacted: bool = False
    previous_message: str = ""
    contacted_date: str = ""
    previous_reply: str = ""
    skip_history: bool = False
    min_words: int = Field(default=90, ge=40, le=250)
    max_words: int = Field(default=140, ge=40, le=250)

    @model_validator(mode="after")
    def word_range(self):
        if self.min_words > self.max_words:
            raise ValueError("Minimum length must not exceed maximum length.")
        return self


class Candidate(Model):
    name: str
    domain: str
    description: str


class Candidates(Model):
    candidates: list[Candidate]


class Document(Model):
    id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    title: str
    content: str
    published_at: str = ""
    retrieved_at: str = Field(default_factory=now)
    provenance: Literal["extracted", "user_provided", "search_snippet", "demo"]


class Fact(Model):
    value: str | None
    document_ids: list[str]
    quote: str


class CompanyProfile(Model):
    name: str
    domain: str
    industry: Fact
    employee_size: Fact
    founding_year: Fact
    ceo: Fact


class Evidence(Model):
    id: str
    document_id: str
    title: str
    claim: str
    quote: str
    kind: Literal["news", "linkedin", "article", "website", "recipient"]
    event_date: str | None
    date_quote: str
    company_match: bool
    current: bool
    confidence: Literal["high", "medium", "low"]
    verification: Literal["extracted", "user_provided", "unverified", "demo"] = "unverified"
    source_url: str = ""
    published_at: str = ""
    retrieved_at: str = ""

    @field_validator("event_date")
    @classmethod
    def date_format(cls, value):
        if value:
            date.fromisoformat(value)
        return value


class ResearchReport(Model):
    company: CompanyProfile
    evidence: list[Evidence]
    gaps: list[str]


class Signal(Model):
    evidence_id: str
    product_connection: str
    recipient_relevance: str
    company_context: str
    strength: Literal["strong", "weak"]


class SignalSelection(Model):
    recommendations: list[Signal]
    rationale: str
    rejected_reasons: list[str]


class Draft(Model):
    subject: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=10000)
    evidence_ids: list[str]


class Quality(Model):
    grounded: bool
    product_relevant: bool
    recipient_relevant: bool
    personalized: bool
    issues: list[str]


class ReviewResponse(Model):
    action: Literal["approve", "revise", "signals", "research", "submit", "cancel", "retry"]
    feedback: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    source_url: str = ""
    source_text: str = ""
    source_title: str = "User-provided source"
    candidate_domain: str = ""
    company_name: str = ""
    recipient_name: str = ""
    recipient_role: str = ""
    previous_message: str = ""
    contacted_date: str = ""
    previous_reply: str = ""
    skip_history: bool = False


LENGTHS = {"Short": (60, 90), "Medium": (90, 140), "Long": (140, 200)}

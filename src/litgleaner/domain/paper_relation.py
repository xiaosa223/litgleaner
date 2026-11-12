"""文库首次入库记录、冻结核实批次和带双侧原文的关系判断。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.language import LanguageCode

RelationKind = Literal[
    "extends", "uses_method_of", "compares_with", "contradicts", "similar_approach", "same_problem"
]
RelationStatus = Literal["valid", "superseded", "rejected_by_user"]
RelationAction = Literal["confirm", "reject", "change"]
SYMMETRIC_KINDS = frozenset({"contradicts", "similar_approach", "same_problem"})
Identity = Annotated[str, Field(min_length=1, pattern=r"\S")]


@dataclass(frozen=True)
class MemoryAdmission:
    id: int
    paper_id: str
    configuration: Mapping[str, Any]
    created_at: str
    baseline: bool = False


class RelationEvidence(FrozenModel):
    paper_id: Identity
    doc_key: Identity
    page: int = Field(ge=0, strict=True)
    quote: str = Field(min_length=10, max_length=3000)


class RelationProposal(FrozenModel):
    paper_a: Identity
    paper_b: Identity
    kind: RelationKind
    evidence_a: RelationEvidence
    evidence_b: RelationEvidence
    explanation: str = Field(min_length=1, max_length=2000)
    source_hashes: dict[str, str]

    @model_validator(mode="after")
    def _identities(self) -> "RelationProposal":
        if self.paper_a == self.paper_b:
            raise ValueError("relation_self_link")
        if self.evidence_a.paper_id != self.paper_a or self.evidence_b.paper_id != self.paper_b:
            raise ValueError("relation_evidence_identity")
        if set(self.source_hashes) != {self.paper_a, self.paper_b} or any(
            not item.strip() for item in self.source_hashes.values()
        ):
            raise ValueError("relation_source_identity")
        if not self.explanation.strip():
            raise ValueError("relation_explanation_empty")
        return self


def canonical_relation(proposal: RelationProposal) -> RelationProposal:
    """对称关系固定双端顺序，并随之交换原文证据。"""
    if proposal.kind not in SYMMETRIC_KINDS or proposal.paper_a < proposal.paper_b:
        return proposal
    return proposal.model_copy(
        update={
            "paper_a": proposal.paper_b,
            "paper_b": proposal.paper_a,
            "evidence_a": proposal.evidence_b,
            "evidence_b": proposal.evidence_a,
        }
    )


class PaperRelation(RelationProposal):
    id: Identity
    batch_id: Identity
    input_hash: Identity
    producer_version: Identity
    created_at: Identity
    status: RelationStatus = "valid"
    source: Literal["model", "user"] = "model"
    feedback_id: Identity | None = None
    superseded_by: Identity | None = None

    @model_validator(mode="after")
    def _state(self) -> "PaperRelation":
        if (self.status == "superseded") != (self.superseded_by is not None):
            raise ValueError("relation_supersession_state")
        if self.superseded_by == self.id:
            raise ValueError("relation_self_supersession")
        if self.source == "user" and self.feedback_id is None:
            raise ValueError("relation_user_feedback_required")
        return self


class RelateBatch(FrozenModel):
    id: Identity
    paper_id: Identity
    input_hash: Identity
    producer_version: Identity
    model: Identity
    reading_language: LanguageCode
    token_budget: int = Field(ge=512, le=64000, strict=True)
    scope_id: Identity
    created_at: Identity
    source_hashes: dict[str, str]
    considered_paper_ids: tuple[Identity, ...]
    input_paper_ids: tuple[Identity, ...]
    inputs: str
    relations: tuple[RelationProposal, ...] = ()
    warnings: tuple[str, ...] = ()
    configuration: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _scope(self) -> "RelateBatch":
        sent, considered = set(self.input_paper_ids), set(self.considered_paper_ids)
        if len(sent) != len(self.input_paper_ids) or len(considered) != len(
            self.considered_paper_ids
        ):
            raise ValueError("relate_duplicate_input")
        if not sent <= considered or set(self.source_hashes) != sent:
            raise ValueError("relate_source_scope")
        pairs = set()
        for proposal in self.relations:
            pair = frozenset((proposal.paper_a, proposal.paper_b))
            if self.paper_id not in pair or not pair <= sent or pair in pairs:
                raise ValueError("relate_relation_scope")
            if any(
                self.source_hashes[key] != value for key, value in proposal.source_hashes.items()
            ):
                raise ValueError("relate_relation_source")
            pairs.add(pair)
        return self

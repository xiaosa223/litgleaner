"""关系方向与双侧身份由领域合同固定。"""

import pytest
from pydantic import ValidationError
from tests.integration.test_relation_repo import batch, proposal

from litgleaner.domain.paper_relation import (
    PaperRelation,
    RelateBatch,
    RelationEvidence,
    canonical_relation,
)
from litgleaner.ports.feedback import FeedbackRecord
from litgleaner.ports.store import FeedbackRecord as LegacyFeedbackRecord


@pytest.mark.parametrize("kind", ["contradicts", "similar_approach", "same_problem"])
def test_symmetric_canonicalization_exchanges_evidence_with_endpoints(kind):
    reverse = proposal("B", "A", kind)
    canonical = canonical_relation(reverse)
    assert canonical.paper_a == "A" and canonical.paper_b == "B"
    assert canonical.evidence_a == reverse.evidence_b and canonical.evidence_b == reverse.evidence_a
    assert canonical_relation(canonical) == canonical


@pytest.mark.parametrize("kind", ["extends", "uses_method_of", "compares_with"])
def test_directed_relations_retain_model_direction(kind):
    reverse = proposal("B", "A", kind)
    assert canonical_relation(reverse) == reverse


@pytest.mark.parametrize("value", [-1, True, 1.0, "0"])
def test_evidence_page_is_strict_zero_based_integer(value):
    with pytest.raises(ValidationError):
        RelationEvidence(paper_id="A", doc_key="doc", page=value, quote="Original source quote.")


@pytest.mark.parametrize(
    "changes",
    [
        {"paper_b": "A"},
        {"source_hashes": {}},
        {"explanation": " "},
        {
            "evidence_a": {
                "paper_id": "B",
                "doc_key": "doc",
                "page": 0,
                "quote": "Original source quote.",
            }
        },
    ],
)
def test_relation_self_link_mixed_papers_and_empty_source_are_rejected(changes):
    with pytest.raises(ValidationError):
        type(proposal()).model_validate(proposal().model_dump() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_hashes": {}},
        {"input_paper_ids": ("A", "B", "B")},
        {"relations": (proposal(), proposal("B", "A"))},
        {"paper_id": "C"},
    ],
)
def test_batch_rejects_unfrozen_sources_duplicate_pairs_and_foreign_origins(changes):
    with pytest.raises(ValidationError):
        RelateBatch.model_validate(batch().model_dump() | changes)


def test_feedback_legacy_path_and_positional_constructor_remain_compatible():
    original = LegacyFeedbackRecord("id", "paper", "channel", "dismiss", None, "actor", "request")
    assert type(original) is FeedbackRecord
    assert original.relation_id is None and original.payload is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "superseded"},
        {"superseded_by": "other"},
        {"status": "superseded", "superseded_by": "relation"},
        {"source": "user"},
    ],
)
def test_relation_status_pointer_and_user_origin_are_consistent(changes):
    values = proposal().model_dump() | {
        "id": "relation",
        "batch_id": "batch",
        "input_hash": "input",
        "producer_version": "producer",
        "created_at": "t",
    }
    with pytest.raises(ValidationError):
        PaperRelation.model_validate(values | changes)

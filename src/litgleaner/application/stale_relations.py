"""关系及完成批次沿用同一只读快照，源文档复用全局缓存。"""

import json
from collections.abc import Mapping

from litgleaner.application.relate_input import configuration, producer_version, source_hash
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.evidence import verify_quote
from litgleaner.domain.paper_relation import PaperRelation, RelateBatch
from litgleaner.domain.stale import StaleItem, item


def _configuration(
    basis: StaleInputs, batch: RelateBatch, reasons: list[str], unchecked: list[str]
) -> None:
    current = dict(configuration(basis.ctx))
    saved = dict(batch.configuration)
    if not saved:
        unchecked.append("批次缺少完整冻结配置，无法完整核验")
        return
    for key in ("producer", "extract_producer"):
        if current.get(key) is None:
            current.pop(key, None)
            saved.pop(key, None)
            unchecked.append(f"当前缺少{key}路由，无法核验生产者")
    if current != saved:
        reasons.append("关系策略、语言、提取或核实配置已变化")


def _sources(
    basis: StaleInputs, hashes: Mapping[str, str], reasons: list[str], unchecked: list[str]
) -> None:
    for id_, expected in hashes.items():
        paper = basis.papers.get(id_)
        if paper is None or id_ not in basis.snapshot.library_paper_ids:
            reasons.append("已发送论文不存在或已退出文库")
            continue
        actual = source_hash(paper, basis.snapshot.fulltexts.get(id_), basis.extractions.get(id_))
        if actual != expected:
            reasons.append("已发送论文的文档或提取已变化")
        try:
            basis.document(id_)
        except ValueError as error:
            unchecked.append(str(error))


def _checks(basis: StaleInputs, batch: RelateBatch) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    unchecked: list[str] = []
    _configuration(basis, batch, reasons, unchecked)
    producer = producer_version(basis.ctx)
    if producer is not None and producer != batch.producer_version:
        reasons.append("关系核实模型或提示词已变化")
    _sources(basis, batch.source_hashes, reasons, unchecked)
    _fragments(basis, batch, reasons, unchecked)
    return reasons, unchecked


def _fragments(
    basis: StaleInputs, batch: RelateBatch, reasons: list[str], unchecked: list[str]
) -> None:
    try:
        for pair in json.loads(batch.inputs)["pairs"]:
            for record in (pair["paper_a"], pair["paper_b"]):
                id_ = record["paper_id"]
                if id_ not in batch.source_hashes:
                    raise ValueError("source identity")
                try:
                    document = basis.document(id_)
                except ValueError:
                    continue
                for fragment in record["fragments"]:
                    page, quote = fragment["page"], fragment["quote"]
                    if type(page) is not int or not isinstance(quote, str):
                        raise ValueError("fragment shape")
                    original = "\n".join(
                        "\n".join(filter(None, (block.caption, block.text)))
                        for block in document.blocks
                        if block.page == page
                    )
                    if not verify_quote(quote, original):
                        reasons.append("批次实际发送的原文片段已不匹配当前指定页")
    except (ValueError, TypeError, KeyError):
        unchecked.append("批次缺少可核对的完整冻结片段")


def _relation(
    basis: StaleInputs, relation: PaperRelation, batches: Mapping[str, RelateBatch]
) -> StaleItem:
    reasons: list[str] = []
    unchecked: list[str] = []
    batch = batches.get(relation.batch_id)
    decided = relation.id in basis.snapshot.user_decided_relation_ids
    if relation.source == "model" and not decided:
        if batch is None:
            unchecked.append("关系缺少原核实批次，无法完整核验")
        else:
            _configuration(basis, batch, reasons, unchecked)
            _fragments(basis, batch, reasons, unchecked)
    _sources(basis, relation.source_hashes, reasons, unchecked)
    for evidence in (relation.evidence_a, relation.evidence_b):
        try:
            document = basis.document(evidence.paper_id)
        except ValueError:
            continue
        original = "\n".join(
            "\n".join(filter(None, (block.caption, block.text)))
            for block in document.blocks
            if block.page == evidence.page
        )
        state = basis.snapshot.fulltexts.get(evidence.paper_id)
        if (
            state is None
            or evidence.doc_key != state.doc_key
            or not verify_quote(evidence.quote, original)
        ):
            reasons.append("关系原文不再匹配当前文档的指定页")
    return item(
        "relation",
        relation.id,
        relation.paper_a,
        None,
        reasons=reasons,
        unchecked=unchecked,
        selected=relation.status == "valid",
    )


def check_relations(basis: StaleInputs) -> tuple[StaleItem, ...]:
    batches = {batch.id: batch for batch in basis.snapshot.relate_batches}
    results = [_relation(basis, relation, batches) for relation in basis.snapshot.relations]
    for batch in basis.snapshot.relate_batches:
        reasons, unchecked = _checks(basis, batch)
        results.append(
            item(
                "relate_batch", batch.id, batch.paper_id, None, reasons=reasons, unchecked=unchecked
            )
        )
    return tuple(results)

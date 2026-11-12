"""真实 SQLite 规范搜索副本命中，原文、来源、页码及只读边界保持。"""

from dataclasses import replace

import pytest
from sqlalchemy import event, text

from litgleaner.application import memory_candidates, search
from litgleaner.application.qa_memory_retrieval import collect
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.qa import Question
from litgleaner.domain.reading import NewAnnotation
from litgleaner.domain.search_text import normalize_search_text
from tests.e2e.search_normalization_helpers import (
    TAIL,
    block_actions,
    corpus,
    document,
    paper,
)


@pytest.mark.parametrize(
    "query,kind,authorship,page,prefix",
    [
        ("paper", "paper", "author", None, "ＰＡＰＥＲ"),
        ("fulltext", "fulltext", "author", 7, "ＦＵＬＬＴＥＸＴ"),
        ("note", "note", "user", 3, "ＮＯＴＥ"),
        ("quote", "highlight", "author", 3, "ＱＵＯＴＥ"),
        ("extract", "extraction", "model", None, "ＥＸＴＲＡＣＴ"),
        ("question", "qa", "user", None, "ＱＵＥＳＴＩＯＮ"),
        ("answer", "qa", "model", None, "ＡＮＳＷＥＲ"),
        ("concept", "concept", "author", 4, "ＣＯＮＣＥＰＴ"),
    ],
)
def test_all_indexed_source_types_match_normalized_tokens_and_return_originals(
    harness, *, query, kind, authorship, page, prefix
):
    id_, *_ = corpus(harness)
    (hit,) = search.search(harness.ctx, query).hits
    assert (hit.paper_id, hit.kind, hit.authorship, hit.page) == (id_, kind, authorship, page)
    original = hit.title if kind == "paper" else hit.text
    assert prefix in original and TAIL in original
    assert hit.title == "ＰＡＰＥＲ " + TAIL


@pytest.mark.parametrize("query", ["ＳＴＲＡＳＳＥ", "STRASSE", "CAFÉ", "Cafe\u0301"])
def test_width_casefold_and_combining_forms_share_all_source_matches(harness, query):
    corpus(harness)
    hits = search.search(harness.ctx, query, limit=100).hits
    assert len(hits) == 8
    assert {hit.kind for hit in hits} == {
        "paper",
        "fulltext",
        "note",
        "highlight",
        "extraction",
        "qa",
        "concept",
    }
    assert all(
        "Straße" in original and "Cafe\u0301" in original
        for hit in hits
        for original in (hit.title if hit.kind == "paper" else hit.text,)
    )


@pytest.mark.parametrize("query", ["神", "神经", "神经网", "神经 网络"])
def test_cjk_short_and_trigram_terms_use_normalized_index_copies(harness, query):
    id_ = paper(harness, "卷积神经网络分析")
    assert [hit.paper_id for hit in search.search(harness.ctx, query).hits] == [id_]


@pytest.mark.parametrize(
    "original,query",
    [("Straße", "STRASSE"), ("Cafe\u0301", "CAFÉ"), ("oﬃce", "office"), ("ＡＢＣ", "abc")],
)
def test_late_normalized_match_excerpt_keeps_continuous_original_unicode(harness, original, query):
    id_ = paper(harness)
    content = ("Straße Cafe\u0301 padding " * 90) + original + " end. " + "tail " * 90
    if query.casefold() in {"strasse", "café"}:
        content = ("ßx\u0301 padding " * 180) + original + " end. " + "tail " * 90
    document(harness, id_, content)
    (hit,) = search.search(harness.ctx, query).hits
    excerpt = hit.text.strip("…")
    assert hit.text.startswith("…") and original in excerpt
    assert excerpt in content and len(excerpt) <= 320
    assert hit.kind == "fulltext" and hit.page == 7


def test_note_search_copies_follow_edits_and_deletion_but_preserve_author_quote(harness):
    id_ = paper(harness)
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(id_, "note", None, None, "Cafe\u0301 author quote", "Straße user note"),
        harness.ctx.now_iso(),
    )
    assert search.search(harness.ctx, "STRASSE").hits[0].text == note.body
    harness.ctx.store.reader.update_annotation(note.id, "ＡＤＪＵＳＴÉ current note", "later")
    assert not search.search(harness.ctx, "STRASSE").hits
    assert search.search(harness.ctx, "adjusté").hits[0].text == "ＡＤＪＵＳＴÉ current note"
    assert search.search(harness.ctx, "CAFÉ").hits[0].authorship == "author"
    harness.ctx.store.reader.delete_annotation(note.id, "removed")
    assert not search.search(harness.ctx, "adjusté").hits
    assert not search.search(harness.ctx, "CAFÉ").hits


def test_metadata_updates_and_latest_extraction_replace_search_copies(harness):
    id_ = paper(harness)
    harness.ctx.store.papers.fill_missing(id_, {"abstract": "Straße abstract"})
    assert search.search(harness.ctx, "STRASSE").hits[0].kind == "paper"
    repo = harness.ctx.store.papers
    repo.insert_extraction(id_, "old", "old", {"summary": "ＣＡＦÉ obsolete"}, "t")
    assert search.search(harness.ctx, "café").hits[0].authorship == "model"
    repo.insert_extraction(id_, "new", "new", {"summary": "ＮＥＷ latest"}, "t2")
    assert not search.search(harness.ctx, "café").hits
    assert search.search(harness.ctx, "new").hits[0].text.strip() == "ＮＥＷ latest"


@pytest.mark.parametrize("name,query", [("Ｓ", "s"), ("é", "e\u0301"), ("学", "学")])
def test_short_concept_search_reuses_normalized_literal_conditions(harness, name, query):
    concept = harness.ctx.store.knowledge.register(name, None, harness.ctx.now_iso())
    assert harness.ctx.store.knowledge.list(None, query, 50, 0) == (concept,)
    assert harness.ctx.store.knowledge.count(None, query) == 1


def _forge(
    harness, id_, key, content, *, kind="fulltext", source_id="0", page=7, authorship="author"
):
    harness.ctx.store._db.write(
        text(
            "INSERT INTO memory_fts"
            "(kind,id,paper_id,page,authorship,source_version,title,text,search_title,search_text) "
            "VALUES(:kind,:id,:paper,:page,:authorship,:key,'',:content,'',:normalized)"
        ).bindparams(
            kind=kind,
            id=source_id,
            paper=id_,
            page=page,
            authorship=authorship,
            key=key,
            content=content,
            normalized=normalize_search_text(content),
        )
    )


def test_qa_normalized_recall_preserves_exact_canonical_user_text_and_source_version(harness):
    id_, key, note, question, _ = corpus(harness)
    forged = note.body.replace("Straße", "STRASSE")
    _forge(
        harness,
        id_,
        "",
        forged,
        kind="note",
        source_id=note.id + ":body",
        page=3,
        authorship="user",
    )
    hits = harness.ctx.store.search.qa_hits("STRASSE CAFÉ", (id_,))
    assert len(hits) == 3 and {hit.id for hit in hits} == {"0", note.id + ":body", question.id}
    author = next(hit for hit in hits if hit.authorship == "author")
    assert author.source_version == key and author.text == "ＦＵＬＬＴＥＸＴ " + TAIL
    assert next(hit for hit in hits if hit.kind == "note").text == note.body
    assert all(hit.text != forged for hit in hits)


def test_qa_retrieval_rechecks_real_original_even_if_forged_index_text_normalizes_equal(harness):
    id_ = paper(harness, library=True)
    original = "The Straße method gives original evidence."
    key = document(harness, id_, original)
    forged = original.replace("Straße", "STRASSE")
    _forge(harness, id_, key, forged)
    question = Question("new", id_, "new", "STRASSE", None, 7, "zh-CN", harness.ctx.now_iso())
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    result = collect(harness.ctx, question, basis)
    authors = [source for source in result.sources if source.kind == "author"]
    assert authors and all(source.text in original for source in authors)
    assert all(source.text != forged and source.doc_key == key for source in authors)


def test_global_and_and_qa_or_keep_literal_query_semantics_after_normalization(harness):
    id_, _, note, *_ = corpus(harness)
    assert not search.search(harness.ctx, "ＮＯＴＥ missingword").hits
    hits = harness.ctx.store.search.qa_hits("ＮＯＴＥ missingword", (id_,))
    assert {hit.id for hit in hits} == {note.id + ":body"}
    assert not search.search(harness.ctx, '"NOTE" OR missingword').hits
    assert not harness.ctx.store.search.qa_hits("_", (id_,))


def test_candidate_recall_normalizes_both_sides_and_keeps_ten_selects(harness):
    a = paper(harness, "ＳＴＲＡＳＳＥ Cafe\u0301", library=True)
    b = paper(harness, "Straße CAFÉ", library=True)
    harness.ctx = replace(harness.ctx, embedder=None)
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    try:
        result = memory_candidates.candidates(harness.ctx, a)
    finally:
        event.remove(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    assert [hit.facts.paper.paper_id for hit in result.matches] == [b]
    assert result.matches[0].signals.text == 1
    assert sum(sql.startswith("SELECT") for sql in statements) == 10
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql in statements)


def test_all_local_search_paths_need_no_model_source_embedding_or_write(harness, monkeypatch):
    id_, _, _, _, concept = corpus(harness)
    before = harness.ctx.store.stats()
    block_actions(harness, monkeypatch)
    assert search.search(harness.ctx, "STRASSE").hits
    assert harness.ctx.store.search.qa_hits("STRASSE", (id_,))
    assert harness.ctx.store.knowledge.list(None, "CAFÉ", 50, 0) == (concept,)
    memory_candidates.candidates(harness.ctx, id_)
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")

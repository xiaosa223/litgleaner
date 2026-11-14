"""真实个人导出入口保留问答记忆来源身份、规范编号及离线论文链接。"""

from dataclasses import replace

from sqlalchemy import event

from litgleaner.application.export import mine
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.export import export_name
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.qa import Answer, Citation, NoteCitation, Question, Statement
from litgleaner.domain.reading import NewAnnotation


def _paper(harness, name):
    return resolve_record(harness.ctx.store.papers, SourceRecord(title=name), harness.ctx.now_iso())


def _question(harness, paper_id, id_, text="用户提问"):
    question = Question(id_, paper_id, id_, text, None, None, "zh-CN", harness.ctx.now_iso())
    harness.ctx.store.qa.add_question(question)
    return question


def _answer(harness, question, statements):
    harness.ctx.store.qa.add_answer(
        Answer(
            "answer:" + question.id,
            question.paper_id,
            question.id,
            statements,
            "saved-model",
            "saved-producer",
            "saved-input",
            harness.ctx.now_iso(),
        )
    )


def _markdown(ctx, paper_id):
    files = {entry.name: entry.content for entry in mine(ctx)}
    return files[export_name("papers", paper_id)].decode(), files


def _link(paper_id):
    return f"[{paper_id}](../{export_name('papers', paper_id)})"


def test_saved_memory_citations_are_readable_with_separate_author_and_user_identity(harness):
    ctx = harness.ctx
    main, memory = _paper(harness, "Main paper"), _paper(harness, "Outside library")
    note = ctx.store.reader.add_annotation(
        NewAnnotation(memory, "note", None, None, None, "用户笔记的原句"), ctx.now_iso()
    )
    historical = _question(harness, memory, "historical", "历史问题的原句")
    question = _question(harness, main, "current")
    _answer(
        harness,
        question,
        (
            Statement(
                "模型结合作者证据及用户记忆作答。",
                (Citation(main, 2, "Author original passage."),),
                False,
                (
                    NoteCitation(memory, note.id, note.body),
                    NoteCitation(memory, historical.id, historical.text, "question"),
                ),
                (main, memory),
            ),
        ),
    )
    text, files = _markdown(ctx, main)
    readable = text.split("回答原始记录", 1)[0]
    assert f"作者原文 · {_link(main)} · PDF 第 3 页" in readable
    assert f"用户笔记引文 · {_link(memory)} · 规范源 ID：{note.id}" in readable
    assert f"历史用户问题引文 · {_link(memory)} · 规范源 ID：historical" in readable
    assert f"陈述涉及论文 · {_link(main)}、{_link(memory)}" in readable
    assert "```text\n用户笔记的原句\n```" in readable
    assert "```text\n历史问题的原句\n```" in readable
    assert readable.count("PDF 第") == 1
    assert export_name("papers", memory) in files
    assert "历史问题的原句" in files[export_name("papers", memory)].decode()


def test_memory_source_ids_and_quotes_use_existing_markdown_escaping(harness):
    main = _paper(harness, "Main")
    question = _question(harness, main, "current")
    unsafe_id = "<script>\n[unsafe](link)*`"
    quote = "```\n<script>literal user words</script>\n``````"
    _answer(
        harness,
        question,
        (Statement("模型推断", (), True, (NoteCitation(main, unsafe_id, quote),), (main,)),),
    )
    text, _ = _markdown(harness.ctx, main)
    readable = text.split("回答原始记录", 1)[0]
    assert "规范源 ID：&lt;script&gt; \\[unsafe\\]\\(link\\)\\*\\`" in readable
    assert f"```````text\n{quote}\n```````" in readable
    assert "模型推断" in readable and f"陈述涉及论文 · {_link(main)}" in readable


def test_missing_paper_keeps_stable_link_without_extra_files_or_source_queries(
    harness, monkeypatch
):
    main = _paper(harness, "Main")
    question = _question(harness, main, "current")
    missing = "missing-paper"
    _answer(
        harness,
        question,
        (
            Statement(
                "保存的外部出处",
                (Citation(missing, 0, "Saved author quote."),),
                False,
                (NoteCitation(missing, "note-missing", "Saved user quote."),),
                (missing,),
            ),
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("export must only use its existing database snapshot")

    for source in harness.ctx.sources.values():
        monkeypatch.setattr(source, "fetch", forbidden)
    monkeypatch.setattr(harness.ctx.blobs, "get", forbidden)
    monkeypatch.setattr(harness.ctx.store.papers, "get", forbidden)
    before = harness.ctx.store.stats()
    queries = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement)

    engine = harness.ctx.store._db._engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=0))
        text, files = _markdown(ctx, main)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert _link(missing) in text
    assert export_name("papers", missing) not in files
    # 12 次论文与概念事实 + 4 次学习记录 + 1 次用户元数据修正，同一事务
    assert sum(sql.lstrip().upper().startswith("SELECT") for sql in queries) == 17
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in queries
    )
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0


def test_old_answer_without_memory_fields_keeps_author_citation_and_inference(harness):
    main = _paper(harness, "Main")
    question = _question(harness, main, "current")
    _answer(
        harness,
        question,
        (Statement("Saved old inference", (Citation(main, 0, "Original author words."),), True),),
    )
    text, _ = _markdown(harness.ctx, main)
    readable = text.split("回答原始记录", 1)[0]
    assert "Saved old inference" in readable and "模型推断" in readable
    assert f"作者原文 · {_link(main)} · PDF 第 1 页" in readable
    assert "用户笔记引文" not in readable and "历史用户问题引文" not in readable
    assert "陈述涉及论文" not in readable

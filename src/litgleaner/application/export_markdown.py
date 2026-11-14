"""论文与概念的离线 Markdown 投影，作者、模型及用户文字分别标明。"""

from dataclasses import asdict

from litgleaner.application.export_format import label, literal, page_label, stored_quotes
from litgleaner.domain.export import export_name
from litgleaner.domain.qa import Statement
from litgleaner.ports.export import ExportConcept, ExportPaper


def _annotations(item: ExportPaper) -> list[str]:
    sections = ["## 用户笔记和高亮"]
    for note in item.annotations:
        sections += [f"### {label(note.kind)} · {page_label(note.page)} · {label(note.id)}"]
        if note.quote is not None:
            sections += ["作者原文（用户选中文本，未单独核验）", literal(note.quote)]
        if note.body is not None:
            sections += ["用户所写", literal(note.body)]
        sections += ["位置与时间记录", literal(asdict(note))]
    return sections


def _generated(item: ExportPaper) -> list[str]:
    sections = ["## 模型生成的读后卡片与提取历史"]
    for record in item.extractions:
        sections += [f"### 提取记录 {record['id']}", "模型生成；完整记录保留输入和版本指纹。"]
        data = record["data"]
        for key, value in data.items():
            sections += [f"模型内容：{label(key)}", literal(value)]
        sections += list(stored_quotes(data))
        sections += ["原始记录（引文字段的来源按上方核验标记区分）", literal(record)]
    sections += ["## 模型判断与规则评分历史"]
    for record in item.assessments:
        generated = {key: value for key, value in record.items() if key != "scores"}
        sections += [f"### 评估记录 {record['id']}", "模型判断", literal(generated)]
        sections += list(stored_quotes(generated))
        sections += ["规则计算的评分", literal(record.get("scores", []))]
    return sections


def _paper_link(paper_id: str) -> str:
    target = "../" + export_name("papers", paper_id)
    return f"[{label(paper_id)}]({target})"


def _statement(statement: Statement) -> list[str]:
    kind = "模型推断" if statement.inference else "模型回答"
    sections = [kind, literal(statement.text)]
    if statement.mentioned_paper_ids:
        links = "、".join(_paper_link(id_) for id_ in statement.mentioned_paper_ids)
        sections += [f"陈述涉及论文 · {links}"]
    for citation in statement.citations:
        sections += [
            f"作者原文 · {_paper_link(citation.paper_id)} · {page_label(citation.page)}",
            literal(citation.quote),
        ]
    for note in statement.note_citations:
        source = "历史用户问题引文" if note.kind == "question" else "用户笔记引文"
        sections += [
            f"{source} · {_paper_link(note.paper_id)} · 规范源 ID：{label(note.source_id)}",
            literal(note.quote),
        ]
    return sections


def _questions(item: ExportPaper) -> list[str]:
    answers = {answer.question_id: answer for answer in item.answers}
    sections = ["## 用户提问与模型问答"]
    for question in item.questions:
        sections += [
            f"### 问题 {label(question.id)} · {page_label(question.page)}",
            "用户所写",
            literal(question.text),
        ]
        if question.selection is not None:
            sections += ["作者原文（用户选中文本，未单独核验）", literal(question.selection)]
        sections += ["问题原始记录", literal(asdict(question))]
        answer = answers.get(question.id)
        if answer is None:
            sections += ["尚无已保存的模型回答。"]
            continue
        for statement in answer.statements:
            sections += _statement(statement)
        sections += ["回答原始记录（含模型及输入指纹）", literal(asdict(answer))]
    return sections


def paper_markdown(item: ExportPaper, cards: str = "") -> str:
    paper = item.paper
    sections = [
        f"# {label(paper.title)}",
        "## 作者与来源元数据",
        literal(asdict(paper)),
        "标识符",
        literal([asdict(alias) for alias in item.aliases]),
    ]
    if paper.abstract is not None:
        sections += ["### 作者摘要（来源元数据原文）", literal(paper.abstract)]
    if item.reading is not None:
        sections += ["## 用户阅读进度", literal(asdict(item.reading))]
    sections += _annotations(item) + _generated(item) + _questions(item)
    sections += [
        "## 用户回答卡片",
        cards or "没有回答过这篇论文的引导问题。模型问答不作为用户回答卡片。",
    ]
    return "\n\n".join(sections) + "\n"


def concept_markdown(item: ExportConcept, learning: str = "") -> str:
    sections = [
        f"# {label(item.concept.name)}",
        "## 概念与别名记录（未记录单独的创建者来源）",
        literal(asdict(item.concept)),
        literal([asdict(alias) for alias in item.aliases]),
    ]
    if learning:  # 没有学习记录时不写等级，避免凭空给出掌握程度
        sections += ["## 掌握程度与用户回答", learning]
    for occurrence in item.occurrences:
        target = "../" + export_name("papers", occurrence.paper_id)
        sections += [
            f"## [{label(occurrence.paper_id)}]({target}) · {page_label(occurrence.page)}",
            f"模型标注的角色：{label(occurrence.role)}",
        ]
        if occurrence.verified and occurrence.definition is not None:
            sections += ["作者原文定义（已核验）", literal(occurrence.definition)]
        else:
            sections += ["没有已保存的核验定义。"]
        sections += ["位置、核验及版本记录", literal(asdict(occurrence))]
    return "\n\n".join(sections) + "\n"

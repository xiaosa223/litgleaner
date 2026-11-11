"""把短分组判断与生成时保存的表格、来源引用拼成可检查的 Markdown。"""

import html
import re

from litgleaner.domain.outline import OutlinePaper, TopicOutline


def _text(value: str) -> str:
    escaped = html.escape(value, quote=False)
    escaped = re.sub(r"([\\`*_{}\[\]()#!~])", r"\\\1", escaped)
    escaped = escaped.replace("|", "&#124;").replace(":", "&#58;").replace("@", "&#64;")
    escaped = re.sub(r"\bwww\.", "www&#46;", escaped, flags=re.IGNORECASE)
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _fence(content: str, language: str) -> str:
    length = max((len(match.group()) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, length + 1)
    return f"{fence}{language}\n{content.rstrip()}\n{fence}"


def _table(papers: tuple[OutlinePaper, ...]) -> str:
    """表格附件由对比表 renderer 生成，每篇只有相同表头及一行已转义内容。"""
    header: list[str] = []
    rows: list[str] = []
    for paper in papers:
        lines = paper.table_markdown.strip().splitlines()
        if not lines:
            continue
        if not header:
            header = lines[:2]
        rows.extend(lines[2:])
    return "\n".join(header + rows) if header else "未保存对比表附件"


def _references(paper: OutlinePaper) -> str:
    lines = ["#### " + _text(paper.title) + "（" + _text(paper.paper_id) + "）"]
    if paper.needs_verification:
        lines.append("缺少可核验标识符，需要人工核对。")
    else:
        lines.extend(("来源引用（BibTeX）：", _fence(paper.bibtex, "bibtex")))
        lines.extend(("来源引用（CSL-JSON）：", _fence(paper.csl_json, "json")))
    lines.extend("来源格式警告：" + _text(warning) for warning in paper.reference_warnings)
    return "\n\n".join(lines)


def _label(paper_id: str, papers: dict[str, OutlinePaper]) -> str:
    paper = papers.get(paper_id)
    return _text(paper.title) + "（" + _text(paper_id) + "）" if paper else _text(paper_id)


def _paper_list(ids: tuple[str, ...], papers: dict[str, OutlinePaper]) -> str:
    return "\n".join("- " + _label(id_, papers) for id_ in ids) if ids else "无"


def _groups(result: TopicOutline, papers: dict[str, OutlinePaper]) -> str:
    parts: list[str] = []
    for index, group in enumerate(result.groups, 1):
        selected = tuple(papers[id_] for id_ in group.paper_ids)
        parts.extend(
            (f"## {index}. " + _text(group.name), "模型分组说明：" + _text(group.description))
        )
        goals = "、".join(_text(id_) for id_ in group.goal_ids) or "未关联研究目标"
        parts.extend(("对应目标编号：" + goals, "### 来源对比表", _table(selected)))
        parts.append("### 来源引用")
        parts.extend(_references(paper) for paper in selected)
    return "\n\n".join(parts) if parts else "## 模型分组\n\n未形成有效分组。"


def _gaps(result: TopicOutline, papers: dict[str, OutlinePaper]) -> str:
    lines = ["## 研究空白候选（仅限本次已读输入集合）"]
    lines.append("以下为模型提出的覆盖缺口候选，需结合所列论文核对。")
    for index, gap in enumerate(result.gaps, 1):
        lines.extend((f"### 候选 {index}", "模型候选说明：" + _text(gap.description)))
        lines.append("依据论文：\n\n" + _paper_list(gap.paper_ids, papers))
    if not result.gaps:
        lines.append("无有效覆盖缺口候选；缺少提取内容不作为研究空白依据。")
    return "\n\n".join(lines)


def render_outline(result: TopicOutline) -> str:
    """只展示生成时快照，不访问来源、推断新事实或代写正文。"""
    papers = {paper.paper_id: paper for paper in result.papers}
    sent = set(result.input_paper_ids)
    omitted = tuple(id_ for id_ in result.considered_paper_ids if id_ not in sent)
    unverified = tuple(paper.paper_id for paper in result.papers if paper.needs_verification)
    warnings = "\n".join("- " + _text(warning) for warning in result.warnings) or "无"
    parts = [
        "# 综述骨架",
        "课题编号：" + _text(result.topic_id),
        "范围为本次实际发送的已读论文。分组和覆盖缺口候选属于模型判断；表格与引用保留生成时快照。",
        _groups(result, papers),
        "## 缺少可核验标识符，需人工核对\n\n" + _paper_list(unverified, papers),
        _gaps(result, papers),
        "## 未分组论文\n\n" + _paper_list(result.ungrouped, papers),
        "## 预算遗漏论文（未发送给模型）\n\n" + _paper_list(omitted, papers),
        "## 警告\n\n" + warnings,
    ]
    return "\n\n".join(parts) + "\n"

"""数据库结构。修改后必须生成 Alembic 迁移，测试会检查两者一致。"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
)

from litgleaner.infra.db.schema_citation import define_citation
from litgleaner.infra.db.schema_knowledge import define_knowledge
from litgleaner.infra.db.schema_outline import define_outline
from litgleaner.infra.db.schema_survey import define_survey
from litgleaner.infra.db.schema_topic_map import define_topic_map

metadata = MetaData()
survey_preview, survey_work, survey_round = define_survey(metadata)
topic_map = define_topic_map(metadata)
topic_outline = define_outline(metadata)

paper = Table(
    "paper",
    metadata,
    Column("id", String, primary_key=True),
    Column("title", Text, nullable=False),
    Column("title_key", Text, nullable=False, index=True),
    Column("abstract", Text),
    Column("authors", Text, nullable=False),
    Column("venue", Text),
    Column("issn", String),
    Column("published_at", String),
    Column("first_seen_at", String, nullable=False, index=True),
    Column("publication_types", Text),
    Column("citation_count", Integer),
    CheckConstraint(
        "publication_types IS NULL OR CASE WHEN json_valid(publication_types) "
        "THEN json_type(publication_types)='array' ELSE 0 END",
        name="ck_paper_publication_types",
    ),
    CheckConstraint(
        "citation_count IS NULL OR (typeof(citation_count)='integer' AND citation_count>=0)",
        name="ck_paper_citation_count",
    ),
)

paper_alias = Table(
    "paper_alias",
    metadata,
    Column("scheme", String, primary_key=True),
    Column("value", String, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
)

merge_log = Table(
    "merge_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

sighting = Table(
    "sighting",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("source", String, primary_key=True),
    Column("query_hash", String, primary_key=True),
    Column("raw_key", String),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

source_cursor = Table(
    "source_cursor",
    metadata,
    Column("source", String, primary_key=True),
    Column("query_hash", String, primary_key=True),
    Column("cursor", Text),
    Column("updated_at", String, nullable=False),
)

artifact = Table(
    "artifact",
    metadata,
    Column("sha256", String, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("blob_key", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("producer", String, nullable=False),
    Column("license_basis", String),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("expires_at", String),
    Column("created_at", String, nullable=False),
)

fulltext = Table(
    "fulltext",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("status", String, nullable=False, index=True),
    Column("pdf_sha256", String),
    Column("doc_key", String),
    Column("provider", String),
    Column("license_basis", String),
    Column("quality", String),
    Column("updated_at", String, nullable=False),
)

extraction = Table(
    "extraction",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("input_hash", String, nullable=False),
    Column("extractor_version", String, nullable=False),
    Column("data", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("paper_id", "input_hash", "extractor_version"),
)

topic_state = Table(
    "topic_state",
    metadata,
    Column("topic_id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("topic_hash", String, nullable=False),
    Column("triage_from", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

triage = Table(
    "triage",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("topic_id", String, primary_key=True),
    Column("topic_hash", String, primary_key=True),
    Column("relevant", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("model", String, nullable=False),
    Column("created_at", String, nullable=False),
)

assessment = Table(
    "assessment",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("topic_id", String, nullable=False),
    Column("topic_hash", String, nullable=False),
    Column("semantic_hash", String, nullable=False),
    Column("input_hash", String, nullable=False),
    Column("levels", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("fulltext_used", Boolean, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("goal_links", Text, nullable=False, server_default="[]"),
    CheckConstraint(
        "CASE WHEN json_valid(goal_links) THEN json_type(goal_links)='array' ELSE 0 END",
        name="ck_assessment_goal_links",
    ),
    UniqueConstraint("paper_id", "topic_id", "topic_hash", "semantic_hash", "input_hash"),
    Index("ix_assessment_topic", "topic_id", "topic_hash", "semantic_hash"),
)

score = Table(
    "score",
    metadata,
    Column("assessment_id", Integer, ForeignKey("assessment.id"), primary_key=True),
    Column("policy_hash", String, primary_key=True),
    Column("score", Integer, nullable=False),
    Column("tier", String, nullable=False),
    Column("provisional", Boolean, nullable=False),
    Column("undetermined", Text, nullable=False, server_default="[]"),
    Column("created_at", String, nullable=False),
)

rubric_snapshot = Table(
    "rubric_snapshot",
    metadata,
    Column("hash", String, primary_key=True),
    Column("rubric_id", String, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

digest = Table(
    "digest",
    metadata,
    Column("id", String, primary_key=True),
    Column("route_id", String, nullable=False),
    Column("topic_id", String, nullable=False),
    Column("channel", String, nullable=False),
    Column("period", String, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("route_id", "topic_id", "period"),
)

digest_item = Table(
    "digest_item",
    metadata,
    Column("digest_id", String, ForeignKey("digest.id"), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
    Column("card", Text, nullable=False),
    Column("provisional", Boolean, nullable=False, server_default=false()),
)

delivery = Table(
    "delivery",
    metadata,
    Column("digest_id", String, ForeignKey("digest.id"), primary_key=True),
    Column("channel", String, primary_key=True),
    Column("status", String, nullable=False, index=True),
    Column("external_id", String),
    Column("last_error", Text),
    Column("updated_at", String, nullable=False),
)

job = Table(
    "job",
    metadata,
    Column("key", String, primary_key=True),
    Column("stage", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_run_at", String, nullable=False),
    Column("last_error", Text),
    Column("first_deferred_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_job_due", "stage", "status", "next_run_at"),
)

llm_call = Table(
    "llm_call",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task", String, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("created_at", String, nullable=False, index=True),
    Column("paper_id", String, nullable=True, index=True),  # 领读器的按需任务按论文计费
    Column("scope_id", String, nullable=True, index=True),
)

# 反馈只追加。external_id 是渠道给的事件编号，同一次点击重复送达时靠它去重。
feedback = Table(
    "feedback",
    metadata,
    Column("id", String, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
    Column("digest_id", String),
    Column("channel", String, nullable=False),
    Column("action", String, nullable=False),
    Column("actor", String),
    Column("external_id", String, unique=True),
    Column("created_at", String, nullable=False),
)

# 已经写进文献库的论文。有这一行就不再写，Zotero 重启也不会重复。
library_entry = Table(
    "library_entry",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("library", String, primary_key=True),
    Column("item_key", String, nullable=False),
    Column("created", Boolean, nullable=False),
    Column("warnings", Text),
    Column("saved_at", String, nullable=False),
)

reading_state = Table(
    "reading_state",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("status", String, nullable=False, index=True),
    Column("position", Text, nullable=True),
    Column("updated_at", String, nullable=False),
)

annotation = Table(
    "annotation",
    metadata,
    Column("id", String, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("page", Integer, nullable=True),
    Column("bbox", Text, nullable=True),
    Column("quote", Text, nullable=True),
    Column("body", Text, nullable=True),
    Column("rects", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("deleted_at", String, nullable=True),
    Column("qa_question_id", String, ForeignKey("qa_message.id"), nullable=True),
)

# 段落译文缓存（第 12.3 节）。同一段原文、同一术语表、同一译者版本只翻一次。
# 按句存放，与原文分句对齐。
translation = Table(
    "translation",
    metadata,
    Column("unit", String, primary_key=True),  # block（段落译文）；双语 PDF 以后用 pdf
    Column("source_hash", String, primary_key=True),
    Column("target_lang", String, primary_key=True),
    Column("glossary_hash", String, primary_key=True),
    Column("translator_version", String, primary_key=True),
    Column("sentences", Text, nullable=False),  # JSON 数组，第 i 项是原文第 i 句的译文
    Column("model", String, nullable=False),
    Column("paper_id", String, nullable=True, index=True),
    Column("created_at", String, nullable=False),
)

# 用户添加的论文（第 12.8 节）：DOI、arXiv 编号、链接、上传的 PDF。只追加状态变化，不删除。
inbox = Table(
    "inbox",
    metadata,
    Column("id", String, primary_key=True),  # ULID
    Column("kind", String, nullable=False),  # doi | arxiv | url | pdf
    Column("value", String, nullable=False),  # 标识符、链接或文件名
    Column("pdf_sha256", String, nullable=True),  # 上传的 PDF
    Column("status", String, nullable=False, index=True),  # pending | done | failed
    Column("paper_id", String, nullable=True, index=True),
    Column("error", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# 用户提问和模型回答分别追加一行；parent_id 只能指向同篇论文的问题。
qa_message = Table(
    "qa_message",
    metadata,
    Column("id", String, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("role", String, nullable=False),
    Column("parent_id", String, ForeignKey("qa_message.id")),
    Column("request_id", String),
    Column("text", Text, nullable=False),
    Column("page", Integer),
    Column("data", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("paper_id", "request_id", name="uq_qa_question_request"),
    UniqueConstraint("parent_id", name="uq_qa_answer_parent"),
    CheckConstraint(
        "(role = 'question' AND parent_id IS NULL AND request_id IS NOT NULL "
        "AND length(trim(request_id)) > 0) OR "
        "(role = 'answer' AND parent_id IS NOT NULL AND request_id IS NULL)",
        name="ck_qa_role_fields",
    ),
    CheckConstraint("length(trim(text)) > 0", name="ck_qa_text"),
    CheckConstraint("page IS NULL OR (typeof(page) = 'integer' AND page >= 0)", name="ck_qa_page"),
    CheckConstraint("json_valid(data)", name="ck_qa_data"),
    Index("ix_qa_paper_created", "paper_id", "created_at", "id"),
)

concept, concept_alias, concept_occurrence = define_knowledge(metadata)
citation = define_citation(metadata)

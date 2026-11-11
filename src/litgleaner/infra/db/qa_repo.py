"""问答只追加：问题按请求去重，答案按问题去重，原始内容永不覆盖。"""

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.qa import Answer, Citation, Question, Statement
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import qa_message


def _question(row: Row[Any]) -> Question:
    data = json.loads(row.data)
    return Question(
        row.id,
        row.paper_id,
        row.request_id,
        row.text,
        data["selection"],
        row.page,
        data["reading_language"],
        row.created_at,
    )


def _answer(row: Row[Any]) -> Answer:
    data = json.loads(row.data)
    statements = tuple(
        Statement(
            item["text"],
            tuple(Citation(**citation) for citation in item["citations"]),
            item["inference"],
        )
        for item in data["statements"]
    )
    return Answer(
        row.id,
        row.paper_id,
        row.parent_id,
        statements,
        data["model"],
        data["producer_version"],
        data["input_hash"],
        row.created_at,
    )


class SqlQARepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add_question(self, question: Question) -> bool:
        data = {"selection": question.selection, "reading_language": question.reading_language}
        return bool(
            self._db.write(
                insert(qa_message)
                .values(
                    id=question.id,
                    paper_id=question.paper_id,
                    role="question",
                    parent_id=None,
                    request_id=question.request_id,
                    text=question.text,
                    page=question.page,
                    data=json.dumps(data, ensure_ascii=False),
                    created_at=question.created_at,
                )
                .on_conflict_do_nothing(index_elements=["paper_id", "request_id"])
            )
        )

    def question(self, question_id: str) -> Question | None:
        rows = self._db.fetch(
            select(qa_message).where(
                qa_message.c.id == question_id,
                qa_message.c.role == "question",
            )
        )
        return _question(rows[0]) if rows else None

    def by_request(self, paper_id: str, request_id: str) -> Question | None:
        rows = self._db.fetch(
            select(qa_message).where(
                qa_message.c.paper_id == paper_id,
                qa_message.c.request_id == request_id,
                qa_message.c.role == "question",
            )
        )
        return _question(rows[0]) if rows else None

    def questions(self, paper_id: str, limit: int = 50) -> tuple[Question, ...]:
        """取最近 limit 个问题，再按 created_at/id 正序返回；零或负 limit 返回空。"""
        if limit <= 0:
            return ()
        rows = self._db.fetch(
            select(qa_message)
            .where(
                qa_message.c.paper_id == paper_id,
                qa_message.c.role == "question",
            )
            .order_by(qa_message.c.created_at.desc(), qa_message.c.id.desc())
            .limit(limit)
        )
        return tuple(_question(row) for row in reversed(rows))

    def answer(self, question_id: str) -> Answer | None:
        rows = self._db.fetch(
            select(qa_message).where(
                qa_message.c.parent_id == question_id,
                qa_message.c.role == "answer",
            )
        )
        return _answer(rows[0]) if rows else None

    def add_answer(self, answer: Answer) -> bool:
        data = {
            "statements": [asdict(statement) for statement in answer.statements],
            "model": answer.model,
            "producer_version": answer.producer_version,
            "input_hash": answer.input_hash,
        }
        page = next(
            (citation.page for statement in answer.statements for citation in statement.citations),
            None,
        )
        return bool(
            self._db.write(
                insert(qa_message)
                .values(
                    id=answer.id,
                    paper_id=answer.paper_id,
                    role="answer",
                    parent_id=answer.question_id,
                    request_id=None,
                    text="\n".join(statement.text for statement in answer.statements),
                    page=page,
                    data=json.dumps(data, ensure_ascii=False),
                    created_at=answer.created_at,
                )
                .on_conflict_do_nothing(index_elements=["parent_id"])
            )
        )

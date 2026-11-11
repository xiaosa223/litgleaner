"""跑一批翻译（第 12.3 节）：编号、占位符保护、流式逐行解析、逐段校验。

一段的句子全部到齐就立即校验并给出结果，不等整批结束。校验通过才写缓存；
结构有问题（缺句、重句、占位符不对）的段落不显示译文，由调用方决定是否单独重试；
只有术语表译名不对时照常显示译文，标为未通过校验。
"""

import re
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from litgleaner.application.context import AppContext
from litgleaner.domain.digest import strip_urls
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.glossary import Glossary, check_translation
from litgleaner.domain.language import language_name
from litgleaner.domain.masking import Masked, mask, restore
from litgleaner.domain.numbered import LineParser, numbered_payload
from litgleaner.domain.paragraphs import Paragraph, slice_to_blocks
from litgleaner.ports.llm import Thinking

TASK = "translate"
_MAX_LINE = 2000
# 模型完全没写 [[n]] 时可能自己加了 [1]、“1.” 这样的序号。
# 只在按位置对齐时去掉，且要求序号后有空白，免得删掉译文开头的“2.5 倍”。
_NUMBERING = re.compile(r"^\s*(?:\[\d+\]|\d+[.、)）])\s+")


@dataclass(frozen=True)
class TranslationDelta:
    """一句完整的译文，显示在这句开头所在的块下面。"""

    index: int
    text: str


@dataclass(frozen=True)
class TranslationStatus:
    state: Literal["thinking"]  # 模型在思考，还没有正文


@dataclass(frozen=True)
class BlockTranslation:
    index: int
    sources: tuple[str, ...]  # 原文分句，与 sentences 一一对应；跨块的句子只含本块部分
    sentences: tuple[str, ...]  # 结构校验失败时为空，界面保留原文
    verified: bool
    cached: bool
    stale: bool = False  # 用旧术语表或旧模型翻的
    problems: tuple[str, ...] = ()


TranslateEvent = TranslationDelta | TranslationStatus | BlockTranslation
Save = Callable[[Paragraph, tuple[str, ...]], None]


@dataclass(frozen=True)
class _Job:
    paragraph: Paragraph
    first: int  # 本段第一句在这一批里的编号
    masked: tuple[Masked, ...]

    @property
    def last(self) -> int:
        return self.first + len(self.masked) - 1


@dataclass(frozen=True)
class _Check:
    sentences: tuple[str, ...]
    problems: tuple[str, ...]
    structural: bool


def block_results(
    paragraph: Paragraph,
    sentences: Sequence[str] | None,
    *,
    cached: bool,
    stale: bool = False,
    problems: tuple[str, ...] = (),
) -> list[BlockTranslation]:
    """段落译文切回各块。sentences 为 None 表示没有可用译文，各块只带原文。"""
    parts = slice_to_blocks(paragraph, sentences or [""] * len(paragraph.sentences))
    verified = sentences is not None and not problems
    return [
        BlockTranslation(
            index,
            part.sources,
            part.sentences if sentences is not None else (),
            verified=verified,
            cached=cached,
            stale=stale,
            problems=problems,
        )
        for index, part in parts.items()
    ]


def _jobs(paragraphs: Sequence[Paragraph]) -> list[_Job]:
    jobs: list[_Job] = []
    number = 1
    for paragraph in paragraphs:
        masked: list[Masked] = []
        for sentence in paragraph.sentences:  # 同一段里占位符接着编号，避免重号
            masked.append(mask(sentence, start=sum(len(m.tokens) for m in masked)))
        jobs.append(_Job(paragraph, number, tuple(masked)))
        number += len(masked)
    return jobs


def _clean(line: str, masked: Masked) -> tuple[str, tuple[str, ...]]:
    """去掉模型自己写的链接，再还原占位符（原文里的链接由占位符带回）。"""
    if len(line) > _MAX_LINE:
        return "", ("译文过长",)
    restored = restore(strip_urls(line) or "", masked.tokens)
    return restored.text.strip(), restored.problems


def _complete(job: _Job, lines: Mapping[int, Sequence[str]]) -> bool:
    return all(n in lines for n in range(job.first, job.last + 1))


def _check(job: _Job, lines: Mapping[int, Sequence[str]], glossary: Glossary) -> _Check:
    numbers = range(job.first, job.last + 1)
    bad = [n - job.first + 1 for n in numbers if len(lines.get(n, ())) != 1]
    if bad:
        listed = "、".join(str(n) for n in bad[:5])
        return _Check((), (f"第 {listed} 句没有译文或重复",), True)
    sentences: list[str] = []
    problems: list[str] = []
    for position, (number, masked) in enumerate(zip(numbers, job.masked, strict=True), 1):
        text, issues = _clean(lines[number][0], masked)
        sentences.append(text)
        problems += [f"第 {position} 句{issue}" for issue in issues]
    if problems:
        return _Check((), tuple(problems), True)
    glossary_problems = check_translation(job.paragraph.sentences, sentences, glossary)
    return _Check(tuple(sentences), glossary_problems, False)


def _positional(job: _Job, stray: Sequence[str]) -> dict[int, list[str]]:
    """单段且模型完全没写编号时，按行的位置对齐；行数不对就不对齐。"""
    if len(stray) != len(job.masked):
        return {}
    stray = [_NUMBERING.sub("", line) for line in stray]
    return {job.first + i: [line] for i, line in enumerate(stray)}


def _finish(job: _Job, check: _Check, save: Save) -> list[BlockTranslation]:
    if check.structural:
        return block_results(job.paragraph, None, cached=False, problems=check.problems)
    if not check.problems:
        save(job.paragraph, check.sentences)
    return block_results(job.paragraph, check.sentences, cached=False, problems=check.problems)


def _live(jobs: Sequence[_Job], number: int, text: str) -> list[TranslationDelta]:
    for job in jobs:
        if job.first <= number <= job.last:
            position = number - job.first
            cleaned, _ = _clean(text, job.masked[position])
            start = job.paragraph.spans[position][0]
            index = next(f.index for f in reversed(job.paragraph.fragments) if f.start <= start)
            return [TranslationDelta(index, cleaned)] if cleaned else []
    return []


def _count_problem(job: _Job, stray: Sequence[str]) -> _Check:
    message = f"句数不一致：原文 {len(job.masked)} 句，译文 {len(stray)} 句"
    return _Check((), (message,), True)


def run_batch(
    ctx: AppContext,
    paper_id: str,
    paragraphs: Sequence[Paragraph],
    glossary: Glossary,
    save: Save,
) -> Generator[TranslateEvent, None, list[Paragraph]]:
    """翻译一批段落，逐段给出结果。返回结构校验失败的段落。"""
    jobs = _jobs(paragraphs)
    payload = numbered_payload([m.text for job in jobs for m in job.masked])
    variables = {
        "count": str(jobs[-1].last),
        "target_language": language_name(ctx.settings.reading_language),
        "glossary": glossary.prompt_text(" ".join(p.text for p in paragraphs)),
    }
    stream = ctx.llm.stream(TASK, variables, payload, paper_id=paper_id)
    parser, lines = LineParser(), dict[int, list[str]]()
    pending, failed = list(jobs), list[Paragraph]()
    thinking_shown = False
    for piece in stream:
        if isinstance(piece, Thinking):
            if not thinking_shown:
                thinking_shown = True
                yield TranslationStatus("thinking")
            continue
        for number, text in parser.feed(piece):
            lines.setdefault(number, []).append(text)
            yield from _live(jobs, number, text)
            for job in [j for j in pending if _complete(j, lines)]:
                pending.remove(job)  # 一行遇到换行才算完整；本段每句都完整就立即校验和显示
                yield from _settle(job, _check(job, lines, glossary), save, failed)
    for number, text in parser.finish():
        lines.setdefault(number, []).append(text)
        yield from _live(jobs, number, text)
    if stream.result is None:  # 流异常结束时上面已经抛出，这里只是防御
        raise InvalidRequest("翻译没有完成，请重试")
    for job in pending:
        check = _check(job, lines, glossary)
        if check.structural and len(jobs) == 1 and not lines:
            positional = _positional(job, parser.stray)
            check = (
                _check(job, positional, glossary)
                if positional
                else _count_problem(job, parser.stray)
            )
        yield from _settle(job, check, save, failed)
    return failed


def _settle(
    job: _Job, check: _Check, save: Save, failed: list[Paragraph]
) -> list[BlockTranslation]:
    if check.structural:
        failed.append(job.paragraph)
    return _finish(job, check, save)

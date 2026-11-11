import assert from "node:assert/strict";
import { test } from "node:test";
import { findIdentifier, identify } from "./identify.ts";

test("DOI：裸写、带前缀、doi.org 链接，去掉句末标点", () => {
  assert.deepEqual(identify("10.1109/TPAMI.2016.2577031"), { kind: "doi", value: "10.1109/TPAMI.2016.2577031" });
  assert.deepEqual(identify("doi: 10.1038/nature14539."), { kind: "doi", value: "10.1038/nature14539" });
  assert.deepEqual(identify("https://doi.org/10.48550/arXiv.1706.03762"), { kind: "doi", value: "10.48550/arXiv.1706.03762" });
});

test("arXiv：裸编号、带版本、arXiv: 前缀、abs 和 pdf 链接", () => {
  assert.deepEqual(identify("1706.03762"), { kind: "arxiv", value: "1706.03762" });
  assert.deepEqual(identify("arXiv:1706.03762v7"), { kind: "arxiv", value: "1706.03762v7" });
  assert.deepEqual(identify("https://arxiv.org/abs/2303.14334"), { kind: "arxiv", value: "2303.14334" });
  assert.deepEqual(identify("https://arxiv.org/pdf/2303.14334v2.pdf"), { kind: "arxiv", value: "2303.14334v2" });
});

test("DOI 保留合法的成对括号，只裁掉句末多余括号", () => {
  for (const value of ["10.1000/a(b)", "10.1000/a[b]", "10.1000/a{b}"]) {
    assert.deepEqual(identify(value), { kind: "doi", value });
    assert.deepEqual(identify(`(${value}).`), { kind: "doi", value });
    assert.deepEqual(findIdentifier(`Title\nDOI: (${value}).`), { kind: "doi", value });
  }
});

test("其他 http 链接按链接处理", () => {
  assert.deepEqual(identify(" https://openreview.net/forum?id=abc "), { kind: "url", value: "https://openreview.net/forum?id=abc" });
});

test("认不出来的返回 null：空串、普通文字、非 http 协议", () => {
  assert.equal(identify(""), null);
  assert.equal(identify("attention is all you need"), null);
  assert.equal(identify("javascript:alert(1)"), null);
  assert.equal(identify("file:///etc/passwd"), null);
});

test("从 PDF 首页文字找本文的标识符：先 DOI，后 arXiv 戳；不看参考文献", () => {
  assert.deepEqual(findIdentifier("Title\nDOI: 10.1109/TPWRS.2020.1234567.\narXiv:2001.00001"), { kind: "doi", value: "10.1109/TPWRS.2020.1234567" });
  assert.deepEqual(findIdentifier("Attention Is All You Need\narXiv:1706.03762v7 [cs.CL] 2 Aug 2023"), { kind: "arxiv", value: "1706.03762" });
  assert.equal(findIdentifier("Title\n" + "x".repeat(5000) + "\n[1] doi:10.1000/other"), null);
  assert.equal(findIdentifier("no identifiers here"), null);
});

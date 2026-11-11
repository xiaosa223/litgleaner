import assert from "node:assert/strict";
import { test } from "node:test";
import { pageFromSearch, paperPageLink } from "./pageLink.ts";

test("检索链接保留零基页码，第一页也可定位", () => {
  assert.equal(paperPageLink("论文/1", 0), "/papers/%E8%AE%BA%E6%96%87%2F1?page=0");
  assert.equal(pageFromSearch("?page=0"), 0);
  assert.equal(pageFromSearch("?page=12&other=x"), 12);
  assert.equal(paperPageLink("x", null), "/papers/x");
});

test("不接受负数、分数、溢出或非数字页码", () => {
  for (const value of ["", "-1", "1.5", "Infinity", "NaN", "100001", "9007199254740992", "1e3"]) {
    assert.equal(pageFromSearch(`?page=${value}`), null);
  }
  assert.equal(pageFromSearch("?q=page"), null);
  assert.equal(paperPageLink("x", -1), "/papers/x");
});

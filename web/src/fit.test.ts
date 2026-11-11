import assert from "node:assert/strict";
import { test } from "node:test";
import { fitFontSize, overlayBaseSize } from "./fit.ts";

test("放得下时用最大字号", () => {
  assert.equal(fitFontSize(() => true, 12, 6), 12);
});

test("取放得下的最大字号，精度 0.25 像素", () => {
  // 字号不超过 9.3 才放得下
  const size = fitFontSize((s) => s <= 9.3, 12, 6);
  assert.ok(size <= 9.3 && size > 9.3 - 0.25, `got ${size}`);
});

test("最小字号也放不下时返回最小字号", () => {
  assert.equal(fitFontSize(() => false, 12, 6), 6);
});

test("最大字号不大于最小字号时直接返回最小字号", () => {
  assert.equal(fitFontSize(() => true, 5, 6), 6);
});

test("基准字号随页面宽度缩放，612 宽的页面约 10.5 像素", () => {
  assert.equal(Math.round(overlayBaseSize(612) * 10) / 10, 10.5);
  assert.equal(overlayBaseSize(1224), overlayBaseSize(612) * 2);
});

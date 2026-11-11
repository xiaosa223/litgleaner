import assert from "node:assert/strict";
import { test } from "node:test";
import { isMac, modKey } from "./platform.ts";

test("按平台名或 UA 判断是不是苹果系统", () => {
  assert.equal(isMac("MacIntel", ""), true);
  assert.equal(isMac("", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"), true);
  assert.equal(isMac("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), false);
  assert.equal(isMac("Linux x86_64", "X11; Linux"), false);
});

test("修饰键的写法", () => {
  assert.equal(modKey(true), "⌘");
  assert.equal(modKey(false), "Ctrl");
});

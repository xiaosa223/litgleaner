import assert from "node:assert/strict";
import { test } from "node:test";
import { api, ApiError } from "./api.ts";

test("真实问答 API 合同：CSRF、编码后的论文路径、零基页码、幂等请求和关联批注", async () => {
  const original = globalThis.fetch;
  const calls: { url: string; options?: RequestInit }[] = [];
  const result = { question: { id: "q/1" }, state: "queued", answer: null };
  globalThis.fetch = async (path, options) => {
    const url = String(path);
    calls.push({ url, options });
    return new Response(JSON.stringify(url === "/api/session" ? { csrf_token: "test-csrf" } : url.includes("?limit=") ? [] : result), { status: options?.method === "POST" ? 202 : 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const abort = new AbortController();
    await api.qaHistory("p/1", abort.signal);
    const body = { text: "问题", selection: "Author quote", page: 0, request_id: "same-request" };
    assert.deepEqual(await api.ask("p/1", body), result);
    await api.ask("p/1", body);
    await api.saveQa("p/1", "q/1");
    assert.equal(calls[0].url, "/api/papers/p%2F1/qa?limit=200");
    assert.equal(calls[0].options?.signal, abort.signal);
    const posts = calls.filter((call) => call.options?.method === "POST");
    assert.equal(posts[0].url, "/api/papers/p%2F1/qa");
    assert.equal((posts[0].options?.headers as Record<string, string>)["X-CSRF-Token"], "test-csrf");
    assert.deepEqual(JSON.parse(String(posts[0].options?.body)), body);
    assert.equal(posts[0].options?.body, posts[1].options?.body);
    assert.equal(posts[2].url, "/api/papers/p%2F1/qa/q%2F1/annotation");
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "选区必须存在于原页" }), { status: 422 });
    await assert.rejects(api.ask("p/1", body), (error: unknown) => error instanceof ApiError && error.status === 422 && error.message === "选区必须存在于原页");
  } finally { globalThis.fetch = original; }
});

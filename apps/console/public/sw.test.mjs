import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("./sw.js", import.meta.url), "utf8");
const ORIGIN = "https://lighthouse.test";

async function installWithIndex(indexStatus) {
  const handlers = new Map();
  const requests = [];
  const stored = [];
  const index = {
    default: "al132025",
    storms: [{ id: "al132025", file: "al132025.json" }],
  };
  const cache = {
    async match() {
      return undefined;
    },
    async put(request) {
      stored.push(new URL(typeof request === "string" ? request : request.url, ORIGIN).pathname);
    },
  };
  const context = {
    URL,
    Request,
    Response,
    console,
    fetch: async (request) => {
      const path = new URL(typeof request === "string" ? request : request.url, ORIGIN).pathname;
      requests.push(path);
      if (path === "/replay/index.json") {
        return new Response(indexStatus === 200 ? JSON.stringify(index) : "missing", {
          status: indexStatus,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("fixture", { status: 200 });
    },
    caches: {
      async open() {
        return cache;
      },
      async keys() {
        return [];
      },
    },
    self: {
      location: { origin: ORIGIN },
      clients: { async claim() {} },
      async skipWaiting() {},
      addEventListener(type, handler) {
        handlers.set(type, handler);
      },
    },
  };
  vm.runInNewContext(SOURCE, context, { filename: "sw.js" });

  let installWork;
  handlers.get("install")({
    waitUntil(work) {
      installWork = work;
    },
  });
  await installWork;
  return { requests, stored };
}

test("a library deployment warms the index/default without downloading legacy replay", async () => {
  const { requests, stored } = await installWithIndex(200);
  assert.ok(requests.includes("/eoc"));
  assert.ok(requests.includes("/replay/index.json"));
  assert.ok(requests.includes("/replay/al132025.json"));
  assert.ok(!requests.includes("/replay/replay.json"));
  assert.ok(stored.includes("/replay/index.json"));
  assert.ok(stored.includes("/replay/al132025.json"));
});

test("legacy replay is warmed only when the index is genuinely absent", async () => {
  const { requests, stored } = await installWithIndex(404);
  assert.ok(requests.includes("/replay/index.json"));
  assert.ok(requests.includes("/replay/replay.json"));
  assert.ok(stored.includes("/replay/replay.json"));
});

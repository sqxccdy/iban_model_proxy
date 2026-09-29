const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("src/static/monitor.html", "utf8");
const script = html.split("<script>")[1].split("</script>")[0];
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const listeners = {};
    nodes.set(id, {
      innerHTML: "", textContent: "", value: "", hidden: false, listeners,
      classList: { toggle() {} }, lastElementChild: { textContent: "" },
      addEventListener(name, callback) { listeners[name] = callback; },
      setAttribute() {}, reset() {}
    });
  }
  return nodes.get(id);
}

let eventSource;
const timers = [];
const environmentId = "a".repeat(64);
const projectId = "b".repeat(64);
class FakeEventSource {
  constructor(url) {
    assert.equal(url, "/events");
    eventSource = this;
  }
}
const counts = (success, fail, tokens, input, output, duration) => ({
  success, fail, tokens, input_tokens: input, output_tokens: output,
  duration_ms: duration, calls: success + fail,
  success_rate: success + fail ? 100 * success / (success + fail) : null,
  avg_duration_ms: success + fail ? duration / (success + fail) : null
});
const snapshot = {
  type: "stats_update", month: "2026-09", ts: 1000,
  success_count: 2, fail_count: 0, total_tokens: 17,
  authorizations: { [environmentId]: "生产环境" },
  projects: { [projectId]: { name: "订单项目", environment_id: environmentId } },
  by_authorization: { [environmentId]: { success: 2, fail: 0, tokens: 17 } },
  daily: {
    date: "2026-09-29", timezone: "Asia/Shanghai",
    global: counts(2, 0, 17, 12, 5, 4000),
    environments: { [environmentId]: counts(2, 0, 17, 12, 5, 4000) },
    projects: { [projectId]: counts(1, 0, 7, 5, 2, 1500) }
  }
};
const context = {
  document: { getElementById: node },
  EventSource: FakeEventSource,
  fetch: async (url) => ({
    ok: true,
    json: async () => url.startsWith("/stats") ? snapshot : { mappings: [] }
  }),
  setTimeout: (callback) => { timers.push(callback); return timers.length; },
  Date, Map, Set, Object, Number, String, JSON, Uint8Array, crypto: globalThis.crypto,
};

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }
async function main() {
  vm.runInNewContext(script, context);
  await flush();
  assert.equal(node("dailyDate").value, "2026-09-29");
  assert.match(node("dailyMetrics").innerHTML, /Token 总用量/);
  assert.match(node("dailyMetrics").innerHTML, /输入 Token/);
  assert.match(node("dailyMetrics").innerHTML, /输出 Token/);
  assert.match(node("dailyMetrics").innerHTML, /调用次数/);
  assert.match(node("dailyMetrics").innerHTML, /调用成功率/);
  assert.match(node("dailyMetrics").innerHTML, /平均响应时长/);
  assert.match(node("dailyStats").innerHTML, /生产环境/);
  assert.match(node("dailyStats").innerHTML, /订单项目/);
  assert.doesNotMatch(node("dailyStats").innerHTML, new RegExp(environmentId));

  node("dailyTarget").value = `environment:${environmentId}`;
  node("dailyTarget").listeners.change();
  assert.match(node("dailyMetrics").innerHTML, /2\.00 秒/);
  assert.match(node("dailyMetrics").innerHTML, />17</);

  node("dailyTarget").value = `project:${projectId}`;
  node("dailyTarget").listeners.change();
  assert.match(node("dailyMetrics").innerHTML, /1\.50 秒/);
  assert.match(node("dailyMetrics").innerHTML, />7</);

  snapshot.success_count = 3;
  snapshot.total_tokens = 20;
  snapshot.daily.global = counts(3, 0, 20, 14, 6, 5000);
  snapshot.daily.environments[environmentId] = counts(3, 0, 20, 14, 6, 5000);
  snapshot.daily.projects[projectId] = counts(2, 0, 10, 7, 3, 2500);
  eventSource.onmessage({ data: JSON.stringify({
    type: "stats_update", month: "2026-09", ts: 2000,
    success_count: 3, fail_count: 0, total_tokens: 20,
    by_authorization: { [environmentId]: { success: 3, fail: 0, tokens: 20 } }
  }) });
  assert.equal(node("tokenCount").textContent, "20");
  assert.equal(timers.length, 1);
  await timers.shift()();
  assert.match(node("dailyMetrics").innerHTML, /1\.25 秒/);
  process.stdout.write("daily key metrics and live update passed\n");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });

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
  success_count: 3, fail_count: 0, total_tokens: 17,
  authorizations: { [environmentId]: "生产环境" },
  projects: { [projectId]: { name: "订单项目", environment_id: environmentId } },
  by_authorization: { [environmentId]: { success: 3, fail: 0, tokens: 17 } },
  by_model: { "test-model": { success: 2, fail: 0, tokens: 15 }, "other-model": { success: 1, fail: 0, tokens: 2 } },
  by_project_model: { [projectId]: {
    "test-model": counts(1, 0, 5, 3, 2, 1000),
    "other-model": counts(1, 0, 2, 2, 0, 500)
  } },
  daily: {
    date: "2026-09-29", timezone: "Asia/Shanghai",
    global: counts(3, 0, 17, 12, 5, 4000),
    environments: { [environmentId]: counts(3, 0, 17, 12, 5, 4000) },
    projects: { [projectId]: counts(2, 0, 7, 5, 2, 1500) },
    models: { "test-model": counts(2, 0, 15, 10, 5, 3500), "other-model": counts(1, 0, 2, 2, 0, 500) },
    environment_models: { [environmentId]: {
      "test-model": counts(2, 0, 15, 10, 5, 3500),
      "other-model": counts(1, 0, 2, 2, 0, 500)
    } },
    project_models: { [projectId]: {
      "test-model": counts(1, 0, 5, 3, 2, 1000),
      "other-model": counts(1, 0, 2, 2, 0, 500)
    } }
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
  assert.equal(node("modelDailyDate").value, "2026-09-29");
  assert.equal(node("callCount").textContent, "3");
  assert.match(node("monthlyEnvironmentStats").innerHTML, /生产环境/);
  assert.match(node("monthlyEnvironmentStats").innerHTML, />17</);
  assert.match(node("dailyStats").innerHTML, /生产环境/);
  assert.match(node("dailyStats").innerHTML, /订单项目/);
  assert.doesNotMatch(node("dailyStats").innerHTML, new RegExp(environmentId));
  for (const label of ["Token 总用量", "输入 Token", "输出 Token", "调用次数", "调用成功率", "平均响应时长"]) {
    assert.match(node("dailyMetrics").innerHTML, new RegExp(label));
    assert.match(node("modelDailyMetrics").innerHTML, new RegExp(label));
  }
  assert.match(node("modelDailyStats").innerHTML, /test-model/);
  assert.match(node("modelDailyStats").innerHTML, /other-model/);
  assert.doesNotMatch(node("modelDailyStats").innerHTML, /订单项目/);

  for (let index = 0; index < 101; index += 1) {
    eventSource.onmessage({ data: JSON.stringify({
      type: "request_log", remote: `127.0.0.1:${50000 + index}`,
      method: index === 100 ? "POST" : "GET", path: `/request-${index}`,
      http_version: "HTTP/1.1", status: index === 100 ? 405 : 200,
      reason: index === 100 ? "Method Not Allowed" : "OK", ts: 1000 + index
    }) });
  }
  assert.match(node("requestList").innerHTML, /POST \/request-100 HTTP\/1\.1&quot; 405 Method Not Allowed/);
  assert.doesNotMatch(node("requestList").innerHTML, /request-0(?:\D|$)/);
  node("requestsTab").listeners.click();
  assert.equal(node("requestsPanel").hidden, false);
  assert.equal(node("connectionsPanel").hidden, true);

  node("dailyTarget").value = `environment:${environmentId}`;
  node("dailyTarget").listeners.change();
  assert.match(node("dailyMetrics").innerHTML, />17</);
  node("dailyTarget").value = `project:${projectId}`;
  node("dailyTarget").listeners.change();
  assert.match(node("dailyMetrics").innerHTML, />7</);

  node("modelDailyTarget").value = `environment:${environmentId}`;
  node("modelDailyTarget").listeners.change();
  assert.match(node("modelDailyMetrics").innerHTML, />17</);
  node("modelDailyTarget").value = `project:${projectId}`;
  node("modelDailyTarget").listeners.change();
  assert.match(node("modelDailyMetrics").innerHTML, />7</);
  assert.match(node("modelDailyStats").innerHTML, /test-model/);
  assert.match(node("modelDailyStats").innerHTML, /other-model/);

  snapshot.success_count = 4;
  snapshot.total_tokens = 20;
  snapshot.by_authorization[environmentId] = {success: 4, fail: 0, tokens: 20};
  snapshot.daily.global = counts(4, 0, 20, 14, 6, 5000);
  snapshot.daily.environments[environmentId] = counts(4, 0, 20, 14, 6, 5000);
  snapshot.daily.projects[projectId] = counts(3, 0, 10, 7, 3, 2500);
  snapshot.daily.models["test-model"] = counts(3, 0, 18, 12, 6, 4500);
  snapshot.daily.environment_models[environmentId]["test-model"] = counts(3, 0, 18, 12, 6, 4500);
  snapshot.daily.project_models[projectId]["test-model"] = counts(2, 0, 8, 5, 3, 2000);
  eventSource.onmessage({ data: JSON.stringify({
    type: "stats_update", month: "2026-09", ts: 2000,
    success_count: 4, fail_count: 0, total_tokens: 20,
    by_authorization: snapshot.by_authorization
  }) });
  assert.equal(node("tokenCount").textContent, "20");
  assert.equal(node("callCount").textContent, "4");
  assert.equal(timers.length, 1);
  await timers.shift()();
  assert.match(node("monthlyEnvironmentStats").innerHTML, />20</);
  assert.match(node("dailyMetrics").innerHTML, />10</);
  assert.match(node("modelDailyMetrics").innerHTML, />10</);
  node("modelDailyDate").value = "2026-09-28";
  node("modelDailyDate").listeners.change();
  assert.equal(node("dailyDate").value, "2026-09-28");
  await flush();
  node("dailyDate").value = "2026-09-27";
  node("dailyDate").listeners.change();
  assert.equal(node("modelDailyDate").value, "2026-09-27");
  process.stdout.write("environment/project and scoped model views passed\n");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });

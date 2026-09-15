const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/comfy_bridge/web/lingjing_conversion.js'), 'utf8')
  .replace(/^import .*;\s*/m, '').replace('export async function', 'async function');

(async () => {
  let extension, timer, calls = [], missing = [];
  const graph = { '1': { class_type: 'Custom', inputs: { text: 'hello' } } };
  const app = {
    graph: { _nodes: [] },
    async loadGraphData(workflow, clean, restore, name) {
      calls.push(['load', workflow, name]);
      extension.afterConfigureGraph(missing);
      return true;
    },
    async graphToPrompt() { calls.push(['convert']); return { output: graph }; },
    queuePrompt() { throw new Error('Conversion must never generate'); },
    registerExtension(value) { extension = value; }
  };
  const banner = { style: {} };
  const context = vm.createContext({
    app, URLSearchParams, Date, Promise, Error, Number, Object,
    location: { hash: '#lingjing_port=45678&lingjing_token=' + 'A'.repeat(43), pathname: '/', search: '' },
    history: { replaceState() {} },
    document: { readyState: 'complete', createElement: () => banner, body: { append() {} } },
    setTimeout: callback => { timer = callback; },
    fetch: async (url, options) => {
      calls.push(['fetch', url, options]);
      return { ok: true, json: async () => ({ nodes: [{ id: 1, type: 'Custom' }], links: [] }) };
    }
  });
  vm.runInContext(source, context);
  const result = await context.convertForLingJing(app, { nodes: [] }, 'temporary');
  assert.equal(result, graph);
  assert.deepEqual(calls.map(c => c[0]), ['load', 'convert']);
  missing = [{ type: 'MissingCustomNode' }];
  await assert.rejects(context.convertForLingJing(app, {}, 'temporary'), /MissingCustomNode/);
  missing = [];
  app.graph._nodes = [{ has_errors: true, type: 'BrokenNode' }];
  await assert.rejects(context.convertForLingJing(app, {}, 'temporary'), /BrokenNode/);
  app.graph._nodes = [];
  calls = [];
  extension.setup();
  await timer();
  const requests = calls.filter(c => c[0] === 'fetch');
  assert.equal(requests.length, 4);
  assert.equal(requests[0][1], 'http://127.0.0.1:45678/workflow');
  assert.equal(requests[3][1], 'http://127.0.0.1:45678/result');
  assert.deepEqual(JSON.parse(requests[3][2].body), { output: graph });
  assert.match(requests[0][2].headers.Authorization, /^Bearer /);
  assert.match(calls.find(c => c[0] === 'load')[2], /LingJing 临时转换/);
  console.log('PASS: native load/serialize, missing nodes, isolated workflow name, authenticated return, no generation');
})().catch(error => { console.error(error); process.exitCode = 1; });

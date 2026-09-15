import { app } from "../../scripts/app.js";

let converting = false;
let missingDuringLoad = [];

// This module never calls queuePrompt. Only the isolated conversion session runs it.
export async function convertForLingJing(app, workflow, name, progress = async () => {}) {
  converting = true;
  missingDuringLoad = [];
  try {
    const loaded = await app.loadGraphData(workflow, true, false, name, { skipAssetScans: true });
    if (loaded === false) throw new Error("ComfyUI 无法加载此工作流，请检查节点是否安装");
    const nodes = app.graph?._nodes || [];
    const missing = [...missingDuringLoad, ...nodes.filter(node => node.has_errors).map(node => node.type)];
    if (missing.length) throw new Error(`缺少或无效节点：${missing.slice(0, 12).join("、")}`);
    await progress('serializing');
    const prompt = await app.graphToPrompt();
    if (!prompt?.output || !Object.keys(prompt.output).length) {
      throw new Error("ComfyUI 未生成有效 API 结构，请检查工作流输出和节点");
    }
    return prompt.output;
  } finally { converting = false; }
}

app.registerExtension({
  name: "LingJing.NativeWorkflowConversion",
  afterConfigureGraph(missingNodeTypes) {
    if (converting && Array.isArray(missingNodeTypes)) {
      missingDuringLoad = missingNodeTypes.map(node => typeof node === "string" ? node : node.type);
    }
  },
  setup() {
    const params = new URLSearchParams(location.hash.slice(1));
    const port = Number(params.get("lingjing_port"));
    const token = params.get("lingjing_token") || "";
    if (!Number.isInteger(port) || port < 1024 || port > 65535 || !/^[A-Za-z0-9_-]{43}$/.test(token)) return;
    history.replaceState(null, "", location.pathname + location.search);
    const endpoint = `http://127.0.0.1:${port}`;
    const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
    const progress = async stage => {
      const response = await fetch(`${endpoint}/progress`, { method: 'POST', headers, redirect: 'error', body: JSON.stringify({ stage }) });
      if (!response.ok) throw new Error('客户端转换会话已结束，请重新导入');
    };
    // ComfyUI registers nodes and creates the graph before this setup hook.
    // A blank startup has no afterLoadGraph event; waiting for it deadlocks.
    setTimeout(async () => {
      try {
        const deadline = Date.now() + 30000;
        while (!app.graph || app.configuringGraph || typeof app.loadGraphData !== 'function' || typeof app.graphToPrompt !== 'function') {
          if (Date.now() > deadline) throw new Error("ComfyUI 前端初始化超时");
          await new Promise(resolve => setTimeout(resolve, 200));
        }
        const response = await fetch(`${endpoint}/workflow`, { headers, redirect: "error" });
        if (!response.ok) throw new Error("客户端转换会话已结束，请重新导入");
        const workflow = await response.json();
        await progress('loading');
        const output = await convertForLingJing(app, workflow, `LingJing 临时转换 ${token.slice(0, 8)}`, progress);
        const result = await fetch(`${endpoint}/result`, { method: "POST", headers, redirect: "error", body: JSON.stringify({ output }) });
        if (!result.ok) throw new Error("客户端未接受转换结果，请查看客户端提示");
      } catch (error) {
        try { await fetch(`${endpoint}/result`, { method: "POST", headers, redirect: "error", body: JSON.stringify({ error: String(error.message).slice(0, 1500) }) }); } catch {}
      }
    }, 1500);
  }
});

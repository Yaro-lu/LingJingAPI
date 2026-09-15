// Exercise the actual single-file page in a tiny in-memory DOM. No browser or UI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../examples/灵境造片厂示例页.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.value = ''; this.style = {}; this.dataset = {}; this.files = []; this.listeners = {}; this.classList = { toggle() {} }; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  add(item) { this.children.push(item); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute() {}
  checkValidity() { return true; }
  reportValidity() {}
  scrollIntoView(options) { this.scrolls = (this.scrolls || 0) + 1; this.scrollOptions = options; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { this.removed = true; }
  focus() {}
  click() { this.clicked = true; }
  play() { this.played = true; return Promise.resolve(); }
}
const nodes = new Map();
for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) nodes.set(match[2], new Element(match[1]));
const storage = () => { const data = new Map(); return { getItem: k => data.get(k) || null, setItem: (k, v) => data.set(k, v) }; };
const calls = [];
const details = {
  A: { id: 'A', name: '<img onerror=alert(1)>', available: true, output_type: 'image', input_schema: { inputs: [
    { name: 'cfg', type: 'number', default: 0 }, { name: 'flag', type: 'boolean', default: false },
    { name: 'caption', type: 'text', label: '<script>bad()</script>', default: 'hello' }
  ] } },
  B: { id: 'B', name: 'B', available: true, output_type: 'text', input_schema: { inputs: [] } }
};
let delayed = null;
const context = vm.createContext({
  document: { body: new Element('body'), getElementById: id => nodes.get(id), createElement: tag => new Element(tag), querySelector: () => new Element(), querySelectorAll: () => [] },
  Option: function(label, value) { const option = new Element('option'); option.textContent = label; option.value = value; return option; },
  localStorage: storage(), sessionStorage: storage(), window: {}, URL, console, TypeError,
  setTimeout: () => 0, clearTimeout() {}, performance: { now: () => 0 },
  fetch: async (url, options) => {
    calls.push({ url, options });
    let payload;
    if (url.includes('?summary=true')) payload = { workflows: [{ id: 'A', name: 'A' }, { id: 'B', name: 'B' }] };
    else {
      const id = url.match(/workflows\/(.*?)\/schema/)[1];
      if (delayed && id === delayed.id) await delayed.promise;
      payload = details[id];
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
  }
});
new vm.Script(script + '\nglobalThis.hooks = {state, connectClient, selectWorkflow, collectDynamicInputs, workflowCache, invalidateConnection, renderDynamicInputs, pollTask, saveArtwork, loadOlderHistory, renderHistory, projects, deleteProject, gatewayFetch, generate, artworkFilename};').runInContext(context);
const hooks = context.hooks;
const api = context.window.LingJingExample;

(async () => {
  assert.equal(nodes.get('baseUrl').value, 'http://127.0.0.1:18188');
  for (const ip of ['10.0.0.1', '172.16.0.1', '172.31.255.255', '192.168.1.20']) assert.equal(api.isPrivateLanHost(ip), true);
  for (const ip of ['8.8.8.8', '172.15.0.1', '172.32.0.1', '192.169.1.1', '10.0.0.999', 'example.com']) assert.equal(api.isPrivateLanHost(ip), false);
  assert.equal(api.normalizeBaseUrl('http://192.168.1.20:18188'), 'http://192.168.1.20:18188');
  assert.throws(() => api.normalizeBaseUrl('http://example.com'));
  assert.equal(api.normalizeBaseUrl('https://example.com'), 'https://example.com');

  nodes.get('apiKey').value = 'test-only';
  await hooks.connectClient();
  assert.equal(calls.length, 1, 'connect should fetch only names');
  assert.equal(hooks.state.activeModel, null, 'no eager detail selection');
  assert.equal(nodes.get('modelSelect').children.length, 3);
  nodes.get('modelSelect').value = 'A';
  await hooks.selectWorkflow();
  assert.equal(calls.length, 2);
  assert.equal(hooks.state.controls[2].input.value, 'hello');
  assert.equal(hooks.state.controls[2].wrapper.children[0].textContent, '<script>bad()</script>');
  const values = await hooks.collectDynamicInputs();
  assert.equal(values.cfg, 0);
  assert.equal(values.flag, false);
  nodes.get('modelSelect').value = 'B';
  await hooks.selectWorkflow();
  assert.equal(hooks.state.controls.length, 0);
  nodes.get('modelSelect').value = 'A';
  await hooks.selectWorkflow();
  assert.equal(calls.length, 3, 'A -> B -> A should reuse A');

  hooks.workflowCache.clear();
  let release;
  delayed = { id: 'A', promise: new Promise(resolve => { release = resolve; }) };
  const slowA = hooks.selectWorkflow();
  await Promise.resolve();
  nodes.get('modelSelect').value = 'B';
  await hooks.selectWorkflow();
  release();
  await slowA;
  assert.equal(hooks.state.activeModel.id, 'B', 'late A must not replace B');
  delayed = null;
  nodes.get('baseUrl').value = 'https://second.example';
  nodes.get('baseUrl').listeners.input();
  assert.equal(hooks.state.connected, false);
  await hooks.connectClient();
  nodes.get('modelSelect').value = 'A';
  await hooks.selectWorkflow();
  assert.equal(calls.at(-1).url, 'https://second.example/v1/workflows/A/schema');
  context.fetch = async () => { throw new TypeError('fetch failed'); };
  nodes.get('baseUrl').value = 'http://192.168.1.20:18188';
  await hooks.connectClient();
  assert.match(nodes.get('connectionState').title, /服务端离线或无法连接/);

  let loaded = 0, unblock;
  const cache = api.createWorkflowCache(async id => { loaded++; await new Promise(resolve => { unblock = resolve; }); return { id }; });
  const first = cache.get('same'), second = cache.get('same');
  assert.equal(first, second, 'concurrent same-id requests share a promise');
  await Promise.resolve();
  assert.equal(loaded, 1);
  unblock();
  await first;
  await cache.get('same');
  assert.equal(loaded, 1);
  let failures = 0;
  const badCache = api.createWorkflowCache(async () => { failures++; throw new Error('offline'); });
  await assert.rejects(badCache.get('bad'));
  await assert.rejects(badCache.get('bad'));
  assert.equal(failures, 2, 'failed detail must not be cached');
  hooks.renderDynamicInputs({id:'flux2_klein_4b_v1', api_mapping_status:'legacy', input_schema:{
    optional:['width','height','steps'], inputs:[{name:'prompt',type:'text'}]}});
  assert.deepEqual(Array.from(hooks.state.controls, c => c.field.name), ['prompt','width','height','steps']);
  hooks.renderDynamicInputs({id:'custom', api_mapping_status:'ready', input_schema:{
    optional:['width','height'], inputs:[{name:'prompt',type:'text'}]}});
  assert.equal(hooks.state.controls.length, 1, 'custom mappings must not gain unbound inputs');
  hooks.renderDynamicInputs({id:'flux2_klein_4b_v1', input_schema:{inputs:[
    {name:'width',type:'integer'}, {name:'height',type:'integer'}, {name:'image',type:'image'}
  ]}});
  const presets = nodes.get('dynamicInputs').children[1].children[0].children[1].children[0];
  presets.value = '1024x576'; presets.listeners.change();
  assert.equal((await hooks.collectDynamicInputs()).width, 1024);
  assert.equal((await hooks.collectDynamicInputs()).height, 576);
  hooks.state.controls[2].input.files = [{}]; hooks.state.controls[2].input.listeners.change();
  assert.equal(hooks.state.controls[0].input.disabled, true);
  context.setTimeout = (fn,ms) => { if(ms === 3000) {fn();return 0;} return setTimeout(fn,ms); }; context.clearTimeout = clearTimeout;
  let polls = 0;
  context.fetch = async () => ({ok:true, text:async()=>JSON.stringify(++polls < 2
    ? {status:'completed',outputs:[]} : {status:'completed',text:'returned text'})});
  await hooks.pollTask('task', null, 'text');
  assert.equal(polls, 2, 'brief empty completion should retry without resubmission');
  assert.equal(nodes.get('resultStage').children[0].textContent, 'returned text');
  context.fetch = async () => ({ok:true,text:async()=>JSON.stringify({status:'failed',error:'model failed'})});
  await assert.rejects(hooks.pollTask('task', null, 'text'), /model failed/);
  hooks.state.busy = false;
  const before = nodes.get('projectList').children.length;
  nodes.get('newProject').listeners.click();
  assert.equal(nodes.get('projectList').children.length, before + 1);
  nodes.get('projectName').value = '测试项目'; nodes.get('projectName').listeners.change();
  assert.match(nodes.get('projectList').children[0].children[0].textContent, /测试项目/);
  hooks.saveArtwork({name:'test',output_type:'text'}, {prompt:'hello',apiKey:'NEVER-PERSIST'}, {task_id:'saved',text:'hello result'});
  assert.equal(nodes.get('projectHistory').children.length, 1);
  const stored = context.localStorage.getItem('lingjing_studio_projects_v1');
  assert.equal(stored.includes('NEVER-PERSIST'), false);
  assert.equal(nodes.get('projectHistory').children[0].children[2].children[0].textContent, 'hello result');
  nodes.get('projectList').children[1].children[0].listeners.click();
  assert.equal(nodes.get('projectHistory').children.length, 0, 'project histories must be isolated');
  hooks.projects[1].works = Array.from({length:15}, (_,i) => ({taskId:`old-${i}`,model:'mock',date:'',createdAt:i+1,text:`text-${i}`}));
  hooks.renderHistory();
  assert.equal(nodes.get('projectHistory').children.length, 6, 'first page must be bounded');
  assert.equal(nodes.get('projectHistory').children[0].children[2].children[0].textContent, 'text-9');
  hooks.loadOlderHistory();
  assert.equal(nodes.get('projectHistory').children.length, 12);
  assert.equal(nodes.get('projectHistory').children[0].children[2].children[0].textContent, 'text-3');
  hooks.loadOlderHistory();
  assert.equal(nodes.get('projectHistory').children.length, 15);
  assert.equal(nodes.get('loadOlder').hidden, true);
  assert.equal(nodes.get('projectHistory').children[14].children[2].children[0].textContent, 'text-14');
  assert.equal(nodes.has('assetsView'), false);
  nodes.get('baseUrl').value='https://gateway.example';
  nodes.get('modelDoc').showModal=()=>{};
  nodes.get('apiDocs').listeners.click({preventDefault(){}});
  const observed = [];
  context.IntersectionObserver = class { constructor(callback) {this.callback=callback;} observe(target) {observed.push(target);} disconnect() {} unobserve() {} };
  context.URL.createObjectURL = () => 'blob:history-fixture';
  context.URL.revokeObjectURL = () => {};
  hooks.state.connected = true; hooks.state.baseUrl = 'https://gateway.example';
  hooks.projects[1].works = [{taskId:'media',model:'mock',date:'',createdAt:1,baseUrl:'https://gateway.example',path:'/v1/files/media/image.png',category:'image'}];
  let mediaRequests = 0;
  context.fetch = async (url, options) => {mediaRequests++; assert.match(url,/gateway.example/); assert.match(options.headers.Authorization,/Bearer/); return {ok:true,blob:async()=>({})};};
  hooks.renderHistory();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(mediaRequests,1,'latest image starts immediately');
  const preview = nodes.get('projectHistory').children[0].children[2];
  await new Promise(resolve => setImmediate(resolve));
  await preview.loadPreview(); await preview.loadPreview();
  assert.equal(mediaRequests,1,'visible media loads only once');
  assert.equal(preview.children[0].tagName,'IMG');
  assert.equal(preview.children[1].textContent,'下载原文件');
  let fallbackRequests=0;
  context.fetch=async url=>{fallbackRequests++;return url.endsWith('/preview') ? {ok:false,status:404} : {ok:true,blob:async()=>({})};};
  hooks.renderHistory();await new Promise(resolve=>setImmediate(resolve));
  const fallback=nodes.get('projectHistory').children[0].children[2];
  assert.equal(fallbackRequests,1);
  assert.equal(fallback.children[1].textContent,'加载原图');
  await fallback.loadPreview();assert.equal(fallbackRequests,1,'no automatic full-size fallback');
  await fallback.children[1].listeners.click();
  assert.equal(fallbackRequests,2);
  assert.equal(fallback.children[0].tagName,'IMG');

  const switchedRequests=[];
  hooks.state.gatewayId='same-gateway'; hooks.state.baseUrl='https://new-url.example'; hooks.state.apiKey='rotated-key';
  context.fetch=async (url,options)=>{switchedRequests.push({url,options});return {ok:true,blob:async()=>({})};};
  hooks.renderHistory(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(switchedRequests.length,1);
  assert.equal(switchedRequests[0].url,'https://new-url.example/v1/files/media/image.png/preview');
  assert.equal(switchedRequests[0].options.headers.Authorization,'Bearer rotated-key');
  const restoredImage=nodes.get('projectHistory').children[0].children[2].children[0];
  await restoredImage.listeners.click();
  assert.equal(switchedRequests.length,2);
  assert.equal(switchedRequests[1].url,'https://new-url.example/v1/files/media/image.png');
  const viewer = context.document.body.children.at(-1);
  assert.equal(viewer.tagName, 'DIALOG');
  assert.equal(viewer.open, true);
  assert.equal(viewer.children[1].src, 'blob:history-fixture');
  viewer.children[1].listeners.click();
  assert.equal(viewer.children[1].style.maxWidth, 'none');
  viewer.listeners.cancel({preventDefault(){}});
  assert.equal(viewer.removed, true);
  assert.equal(hooks.artworkFilename('一“杯”猫<>:/\\*?\u0000', '/files/output.png'), '灵境-一杯猫.png');
  assert.equal(hooks.artworkFilename('???', '', 'video/mp4'), '灵境-作品.mp4');
  assert.equal(hooks.artworkFilename('a'.repeat(100), '', 'image/webp'), '灵境-' + 'a'.repeat(16) + '.webp');
  const videoWork=hooks.projects[1].works[0]; videoWork.category='video'; videoWork.path='/v1/files/media/movie.mp4';
  switchedRequests.length=0;
  hooks.renderHistory(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(switchedRequests.length,1);
  assert.match(switchedRequests[0].url,/movie.mp4\/preview$/);
  const videoPreview=nodes.get('projectHistory').children[0].children[2];
  assert.equal(videoPreview.children[0].tagName,'IMG','video history loads a still thumbnail');
  assert.equal(videoPreview.children[1].textContent,'▶ 播放视频');
  await videoPreview.children[1].listeners.click();
  assert.equal(switchedRequests.length,2);
  assert.match(switchedRequests[1].url,/movie.mp4$/);
  assert.equal(videoPreview.children[0].tagName,'VIDEO');
  assert.equal(videoPreview.children[0].poster,'blob:history-fixture');
  assert.equal(videoPreview.children[0].played,true);
  videoWork.category='image';videoWork.path='/v1/files/media/image.png';

  hooks.renderDynamicInputs({id:'llm',output_type:'text',api_mapping_status:'legacy',input_schema:{required:['prompt'],inputs:[
    {name:'prompt',type:'text',required:true},{name:'messages',type:'messages'},{name:'response_format',type:'object'}]}});
  const control = name => hooks.state.controls.find(c=>c.field.name===name);
  control('system_prompt').input.value='你是编剧'; control('prompt').input.value='写一个开场';
  const chatBody = await hooks.collectDynamicInputs();
  assert.match(chatBody.prompt,/系统提示词：.*\n你是编剧/);
  assert.match(chatBody.prompt,/用户提示词：.*\n写一个开场/);
  assert.equal(chatBody.messages,undefined);
  assert.equal(chatBody.response_format.type,'text');
  assert.equal(control('prompt').wrapper.children[0].children[0].className,'required-star');
  hooks.state.activeModel={id:'llm',name:'文字模型',available:true};
  nodes.get('apiDocs').listeners.click({preventDefault(){}});
  assert.match(nodes.get('modelDocText').textContent,/POST \/v1\/workflows\/run\/llm/);
  const toastBefore=nodes.get('toast').textContent;
  let attempts=0;
  context.fetch=async()=>{attempts++;throw new TypeError('offline');};
  await hooks.connectClient({silent:true});
  assert.equal(attempts,1);
  assert.equal(nodes.get('toast').textContent,toastBefore,'silent failure must not show a toast');
  const projectToRemove=hooks.projects.find(p=>p.id!=='__unfiled__');
  const artworkSnapshot=JSON.stringify(projectToRemove.works);
  const requestsBeforeDelete=attempts;
  hooks.state.busy=true; hooks.deleteProject(projectToRemove.id);
  assert.equal(hooks.projects.includes(projectToRemove),true,'running generation blocks grouping changes');
  hooks.state.busy=false; hooks.deleteProject(projectToRemove.id);
  assert.equal(hooks.projects.some(p=>p.id===projectToRemove.id),false);
  const unfiled=hooks.projects.find(p=>p.id==='__unfiled__');
  assert.equal(JSON.stringify(unfiled.works),artworkSnapshot,'all artwork records must survive');
  assert.equal(attempts,requestsBeforeDelete,'deleting a project must not request file deletion');
  const persisted=JSON.parse(context.localStorage.getItem('lingjing_studio_projects_v1'));
  assert.equal(persisted.some(p=>p.id===projectToRemove.id),false);
  assert.equal(JSON.stringify(persisted.find(p=>p.id==='__unfiled__').works),artworkSnapshot);
  hooks.renderDynamicInputs({id:'flux2_klein_4b_v1',input_schema:{inputs:[
    {name:'width',type:'integer',default:768},{name:'height',type:'integer',default:768},{name:'image',type:'image'}]}});
  const sizeSelect=nodes.get('dynamicInputs').children[1].children[0].children[1].children[0];
  assert.equal(sizeSelect.value,'768x768');
  const referenceChoice=sizeSelect.children.find(option=>option.value==='reference');
  assert.equal(referenceChoice.disabled,true);
  hooks.state.controls[2].input.files=[{}]; hooks.state.controls[2].input.listeners.change();
  assert.equal(sizeSelect.value,'reference');
  assert.equal(referenceChoice.disabled,false);
  assert.equal(hooks.state.controls[0].input.disabled,true);
  hooks.state.controls[2].input.files=[]; hooks.state.controls[2].input.listeners.change();
  assert.equal(sizeSelect.value,'768x768');
  assert.equal(referenceChoice.disabled,true);
  assert.equal(hooks.state.controls[0].input.disabled,false);
  hooks.renderDynamicInputs({output_type:'video',input_schema:{inputs:[
    {name:'image',type:'image',options:['old-server-image.png'],required:true},
    {name:'duration',type:'number',default:5},{name:'fps',type:'number',default:24}]}});
  assert.equal(control('image').input.tagName,'INPUT');
  assert.equal(control('image').input.type,'file');
  assert.equal(control('duration').input.value,'5');
  hooks.renderDynamicInputs({output_type:'video',input_schema:{video_timing:{fps:24,duration:5,frame_step:8,frame_offset:1,inclusive_end:1},inputs:[
    {name:'frames',type:'integer',default:121,minimum:1},{name:'fps',type:'number',default:24}]}});
  assert.equal(control('frames'),undefined);
  control('__video_duration').input.value='3'; control('fps').input.value='25';
  const timedBody = await hooks.collectDynamicInputs();
  assert.equal(timedBody.frames,73);
  assert.equal(timedBody.__video_duration,undefined);
  hooks.renderDynamicInputs({output_type:'video',api_mapping_status:'legacy',input_schema:{optional:['duration','frames','fps'],inputs:[]}});
  assert.equal(control('frames'),undefined);
  control('duration').input.value='4'; control('fps').input.value='20';
  const legacyTimedBody = await hooks.collectDynamicInputs();
  assert.equal(legacyTimedBody.duration,4); assert.equal(legacyTimedBody.frames,undefined);
  hooks.state.connected=true; hooks.state.busy=false;
  hooks.state.activeModel={id:'offline-model',name:'test',available:true,output_type:'image'};
  hooks.renderDynamicInputs({input_schema:{inputs:[]}});
  nodes.get('resultStage').style.display='none';
  context.fetch=async()=>{throw new TypeError('network gone');};
  await hooks.generate();
  assert.equal(nodes.get('resultStage').scrolls,1,'submission scrolls once to current generation');
  assert.equal(nodes.get('resultStage').scrollOptions.block,'center');
  assert.equal(hooks.state.busy,false);
  assert.equal(hooks.state.connected,false);
  assert.equal(nodes.get('resultStage').style.display,'');
  assert.match(nodes.get('resultStage').children[0].textContent,/服务端离线/);
  context.fetch=async()=>({status:502});
  await assert.rejects(hooks.gatewayFetch('https://test.example'),error=>error.offline===true);
  context.fetch=()=>new Promise(()=>{});
  await assert.rejects(hooks.gatewayFetch('https://test.example',{},5),error=>error.offline===true);
  console.log('PASS: URL boundaries, names-only loading, typed controls, text-only labels, cache reuse, races, connection isolation, deduplication and retry');
})().catch(error => { console.error(error); process.exitCode = 1; });

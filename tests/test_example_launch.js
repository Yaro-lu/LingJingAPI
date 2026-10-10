// Run the actual example page with a desktop launch fragment, without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../examples/LingJingAPI示例页.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function launch(hash) {
  const nodes = new Map();
  class Element {
    constructor(tag = 'div') { this.tagName=tag.toUpperCase(); this.children=[]; this.value=''; this.style={}; this.dataset={}; this.files=[]; this.classList={toggle(){}}; }
    set id(value) { this._id=value; nodes.set(value,this); }
    get id() { return this._id; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children=items; }
    add(item) { this.children.push(item); }
    addEventListener() {}
    setAttribute() {}
    focus() {}
    close() {}
  }
  for(const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) nodes.set(match[2],new Element(match[1]));
  const calls=[],history=[];
  const storage = {getItem:()=>null,setItem(){}};
  const location={protocol:'http:',origin:'http://127.0.0.1:19111',pathname:'/',search:'',hash};
  const context=vm.createContext({
    document:{body:new Element('body'),getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),querySelector:()=>new Element(),querySelectorAll:()=>[]},
    Option:function(label,value){const el=new Element('option');el.textContent=label;el.value=value;return el;},
    localStorage:{getItem:key=>key==='lingjing_connection'?JSON.stringify({url:'https://previous-server.invalid',key:'previous-key'}):null,setItem(){}},
    sessionStorage:storage,window:{location,history:{replaceState:(...args)=>{history.push(args);location.hash='';}}},
    URL,URLSearchParams,console,TypeError,setTimeout:()=>0,clearTimeout(){},performance:{now:()=>0},
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,status:200,text:async()=>JSON.stringify({workflows:[]})};}
  });
  new vm.Script(script+'\nglobalThis.launchHelpers={consumeLaunchKey};').runInContext(context);
  return {nodes,calls,history,location,helpers:context.launchHelpers};
}

(async()=>{
  const key='generation-key &#+';
  const result=launch('#'+new URLSearchParams({lingjing_key:key}));
  assert.equal(result.nodes.get('baseUrl').value,'http://127.0.0.1:19111');
  assert.equal(result.nodes.get('apiKey').value,key);
  assert.equal(result.location.hash,'');
  assert.equal(result.history.length,1);
  assert.equal(result.history[0][2],'/');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(result.calls.length,1,'Launch only discovers workflows; it must not generate');
  assert.ok(result.calls[0].url.startsWith('http://127.0.0.1:19111/'));
  assert.equal(result.calls[0].options.headers.Authorization,'Bearer '+key);
  assert.ok(!result.calls[0].url.includes(key),'Key is not sent in a request URL');
  const normal=launch('');
  assert.equal(normal.nodes.get('baseUrl').value,'https://previous-server.invalid');
  assert.equal(normal.nodes.get('apiKey').value,'previous-key');
  assert.equal(normal.history.length,0,'Ordinary expert/manual connections are unchanged');
  assert.equal(result.helpers.consumeLaunchKey({protocol:'file:',hash:'#lingjing_key=untrusted'},{}),'');
  console.log('PASS: same-origin desktop auto-connect, fragment removal, generation-only authorization, no generation calls, manual connection preserved');
})().catch(error=>{console.error(error);process.exitCode=1;});

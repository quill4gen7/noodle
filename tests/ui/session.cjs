const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../webui/nodes.html'), 'utf8');
const fragment = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const OK = {ok:true, status:200, json:async()=>({version:'v1'})};
const flush = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
function context(extra={}) {
  let now=0, id=0; const timers=new Map();
  const c={JSON,Promise,console,currentName:'demo',sessionEpoch:1,loadingGraph:false,
    lastSavedJSON:JSON.stringify({v:0}),lastObservedJSON:null,docState:'saved',serverSaveTimer:null,
    value:0,nodeIndex:{},saveChain:Promise.resolve(),API:'',thumbWanted:false,
    toGraphJSON:()=>({v:c.value}),setDocState:s=>c.docState=s,
    saveDraft:()=>{c.drafts++;},clearDraft:()=>{c.clears++;},drafts:0,clears:0,
    log(){},maybeThumb(){},
    // live sync (persistToServer names its base version and merges on a 409)
    syncMaySave:()=>true,syncCanSkipEcho:()=>false,syncSaveStarted(){},syncBaseQuery:()=>'',
    syncSaved(){},syncOnStaleSave:async()=>false,
    setTimeout(fn,delay){ const n=++id; timers.set(n,{at:now+delay,fn}); return n; },
    clearTimeout(n){timers.delete(n);},
    advance(ms){const until=now+ms; while(true){
      const due=[...timers].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!due) break; now=due[1].at; timers.delete(due[0]); due[1].fn();
    } now=until;},...extra};
  vm.createContext(c);
  vm.runInContext(fragment('function checkDirty(){','async function guardUnsaved(){'),c);
  return c;
}
test('an unchanged dirty graph autosaves despite the 1s polling loop',()=>{
  let saves=0; const c=context({persistToServer(){saves++;}}); c.value=1;
  for(let i=0;i<10;i++){c.advance(1000);c.checkDirty();}
  assert.equal(saves,1);
});
test('only a real edit resets debounce; failed state is not hidden by polling',()=>{
  let saves=0; const c=context({persistToServer(){saves++;c.docState='failed';}});
  c.value=1;c.checkDirty();c.advance(2000);c.value=2;c.checkDirty();
  c.advance(2000);assert.equal(saves,0);c.advance(500);assert.equal(saves,1);
  for(let i=0;i<10;i++){c.advance(1000);c.checkDirty();}
  assert.equal(c.docState,'failed');assert.equal(saves,1);
});
test('save writer is serial and does not delete a newer local draft',async()=>{
  const requests=[]; const c=context({fetchTimed:(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}))});
  vm.runInContext(fragment('function persistToServer(', 'window.saveGraph ='),c);
  c.value=1;const a=c.persistToServer();c.value=2;const b=c.persistToServer();await flush();
  assert.equal(requests.length,1);
  requests[0].resolve(OK);await flush();
  assert.equal(c.lastSavedJSON,JSON.stringify({v:1}));assert.equal(c.clears,0);
  assert.equal(requests.length,2);requests[1].resolve(OK);
  assert.equal(await a,true);assert.equal(await b,true);
  assert.equal(c.lastSavedJSON,JSON.stringify({v:2}));assert.equal(c.docState,'saved');
});
test('a save completing after project switch cannot mutate the new session',async()=>{
  let resolve;const c=context({fetchTimed:()=>new Promise(r=>resolve=r)});
  vm.runInContext(fragment('function persistToServer(', 'window.saveGraph ='),c);
  c.value=1;const saved=c.persistToServer();await flush();
  c.sessionEpoch++;c.currentName='other';c.lastSavedJSON='other baseline';
  resolve(OK);assert.equal(await saved,false);assert.equal(c.lastSavedJSON,'other baseline');
  assert.equal(c.clears,0);
});
test('failed save preserves draft and can be retried',async()=>{
  let ok=false;const c=context({fetchTimed:async()=>({ok,json:async()=>({detail:'disk full'})})});
  vm.runInContext(fragment('function persistToServer(', 'window.saveGraph ='),c);
  c.value=1;assert.equal(await c.persistToServer(),false);assert.equal(c.docState,'failed');
  assert.equal(c.lastSavedJSON,JSON.stringify({v:0}));assert.equal(c.clears,0);
  ok=true;assert.equal(await c.persistToServer(),true);assert.equal(c.docState,'saved');
});
test('a 409 merges and saves again inside the same queued write (no self-wait)',async()=>{
  const bodies=[];let merges=0;
  const c=context({
    fetchTimed:async(url,opts)=>{bodies.push(opts.body);
      return bodies.length===1 ? {ok:false,status:409,json:async()=>({detail:{}})} : OK;},
    syncOnStaleSave:async()=>{merges++;c.value=7;return true;}});   // the merge changes the canvas
  vm.runInContext(fragment('function persistToServer(', 'window.saveGraph ='),c);
  c.value=1;
  const r=await Promise.race([c.persistToServer(), new Promise(res=>setTimeout(()=>res('hung'),500))]);
  assert.equal(r,true);assert.equal(merges,1);
  assert.deepEqual(bodies,[JSON.stringify({v:1}),JSON.stringify({v:7})]);   // the retry saves the MERGED graph
  assert.equal(c.lastSavedJSON,JSON.stringify({v:7}));assert.equal(c.docState,'saved');
});
test('Live burst keeps one active run and one latest pending run',async()=>{
  const releases=[];let started=0;
  const c=context({runTask:null,runPending:false,runContext:null,window:{},
    document:{getElementById:()=>({})},setStatus(){},
    executeCurrentGraph:()=>{started++;return new Promise(r=>releases.push(r));}});
  vm.runInContext(fragment('window.runGraph = function(){','async function executeCurrentGraph(){'),c);
  const done=c.window.runGraph();for(let i=0;i<20;i++) c.window.runGraph();
  assert.equal(started,1);releases.shift()();await flush();assert.equal(started,2);
  releases.shift()();await done;assert.equal(started,2);assert.equal(c.runTask,null);
});

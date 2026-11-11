// Exercise real load routing: unrelated account queries must never delay page loading.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..');
const menu=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/luci/menu.d/luci-app-routepilot.json')));
const source=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/view',Object.values(menu)[0].action.path+'.js'),'utf8');
let replaced='',calls=[],throwHints=false,featuresReady=false,hangHints=false,hangRPC=false;
let timerSerial=0;const pendingTimers=new Map();
function scheduleTimer(fn,ms){const id=++timerSerial;pendingTimers.set(id,{fn,ms});return id;}
function cancelTimer(id){pendingTimers.delete(id);}
const location={pathname:'',replace(url){replaced=url;}};
const view=new Function('window','view','uci','fs','ui','network','rpc','L','setTimeout','clearTimeout',source)(
 {location},{extend:x=>x},{load:()=>Promise.resolve()},
 {read(){return Promise.resolve('{}');},exec(file,args){calls.push(file.endsWith('routepilot')?args[0]:path.basename(file));if(file.endsWith('routepilot-runtime-status')||file.endsWith('routepilot-exits')||args&&args[0]==='warp-info')return new Promise(()=>{});return Promise.resolve({code:0,stdout:file.endsWith('routepilot-backup-list')?'{}':'[]'});}},
 {menu:{load:()=>Promise.resolve({})}},
 {getHostHints(){calls.push('hints');assert.ok(featuresReady);if(hangHints)return new Promise(()=>{});if(throwHints)throw new TypeError('optional hints failure');return Promise.resolve({host:'hint'});}},
 {declare:spec=>spec.object==='luci-rpc'&&spec.method==='getHostHints'?()=>{calls.push('hints');if(hangRPC)return new Promise(()=>{});if(throwHints)throw new TypeError('optional hints failure');return Promise.resolve({host:'hint'});}:()=>{}}, {url:p=>'/'+p,probeSystemFeatures(){calls.push('features');featuresReady=true;return Promise.resolve();}},scheduleTimer,cancelTimer
);
(async()=>{
 for(const leaf of ['routepilot','overview','account','routing','devices','dns','workers','rules','backup']){
  calls=[];replaced='';featuresReady=false;location.pathname='/admin/services/routepilot'+(leaf==='routepilot'?'':['account','routing','devices','dns','workers'].includes(leaf)?'/cloudflare/'+leaf:'/'+leaf);
  const data=await view.load();assert.equal(data[3].networks.length,0);assert.ok(!calls.includes('routepilot-exits'));assert.ok(!calls.includes('warp-info'));
  assert.deepEqual(calls.slice().sort(),(['routepilot','overview'].includes(leaf)?['routepilot-runtime-status']:leaf==='devices'?['interfaces','hints']:leaf==='routing'?['interfaces']:leaf==='workers'?['routepilot-backup-list']:[]).sort());
  assert.equal(replaced,'');
 }
 for(const [alias,section] of Object.entries({basic:'routing',advanced:'account',devices:'devices',dns:'dns',diagnostics:'dns',workers:'workers'})){
  location.pathname='/admin/services/routepilot/'+alias;replaced='';await view.load();assert.equal(replaced,'/admin/services/routepilot/cloudflare/'+section);
 }
 throwHints=true;location.pathname='/admin/services/routepilot/cloudflare/devices';const data=await view.load();assert.equal(data[1],null);
 // Regression: ISSUE-004 — optional host hints cannot initialize all protocols.
 // Found by /qa on 2026-11-10; report: .local/imok-qa-20261110/report.md
 throwHints=false;hangHints=true;location.pathname='/admin/services/routepilot/cloudflare/devices';
 await Promise.race([view.load(),new Promise((resolve,reject)=>setTimeout(()=>reject(Error('Device page waited for unrelated network protocol initialization')),30))]);
 assert.equal(pendingTimers.size,0,'successful hint reads release their timer');
 hangRPC=true;let settled=false;const pending=view.load().then(data=>{settled=true;return data;});await new Promise(setImmediate);
 assert.equal(settled,false);assert.equal(pendingTimers.size,1);
 const deadline=[...pendingTimers.values()][0];assert.equal(deadline.ms,2000);deadline.fn();
 const timedOut=await pending;assert.equal(timedOut[1],null,'optional host hints cannot prevent editing saved policies');
 console.log('Independent page load: scoped reads; slow directory/protocol/runtime metadata excluded; optional hints isolated; legacy URLs preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});

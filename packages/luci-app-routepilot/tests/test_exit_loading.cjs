// Exercise real load routing: unrelated account queries must never delay page loading.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..');
const menu=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/luci/menu.d/luci-app-routepilot.json')));
const source=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/view',Object.values(menu)[0].action.path+'.js'),'utf8');
let replaced='',calls=[],throwHints=false,featuresReady=false;
const location={pathname:'',replace(url){replaced=url;}};
const view=new Function('window','view','uci','fs','ui','network','rpc','L',source)(
 {location},{extend:x=>x},{load:()=>Promise.resolve()},
 {read(){return Promise.resolve('{}');},exec(file,args){calls.push(file.endsWith('routepilot')?args[0]:path.basename(file));if(file.endsWith('routepilot-runtime-status')||file.endsWith('routepilot-exits')||args&&args[0]==='warp-info')return new Promise(()=>{});return Promise.resolve({code:0,stdout:file.endsWith('routepilot-backup-list')?'{}':'[]'});}},
 {menu:{load:()=>Promise.resolve({})}},
 {getHostHints(){calls.push('hints');assert.ok(featuresReady);if(throwHints)throw new TypeError('optional hints failure');return Promise.resolve({host:'hint'});}},
 {declare:()=>()=>{}}, {url:p=>'/'+p,probeSystemFeatures(){calls.push('features');featuresReady=true;return Promise.resolve();}}
);
(async()=>{
 for(const leaf of ['routepilot','overview','account','routing','devices','dns','workers','rules','backup']){
  calls=[];replaced='';featuresReady=false;location.pathname='/admin/services/routepilot'+(leaf==='routepilot'?'':['account','routing','devices','dns','workers'].includes(leaf)?'/cloudflare/'+leaf:'/'+leaf);
  const data=await view.load();assert.equal(data[3].networks.length,0);assert.ok(!calls.includes('routepilot-exits'));assert.ok(!calls.includes('warp-info'));
  assert.deepEqual(calls.slice().sort(),(['routepilot','overview'].includes(leaf)?['routepilot-runtime-status']:leaf==='devices'?['features','interfaces','hints']:leaf==='routing'?['interfaces']:leaf==='workers'?['routepilot-backup-list']:[]).sort());
  assert.equal(replaced,'');
 }
 for(const [alias,section] of Object.entries({basic:'routing',advanced:'account',devices:'devices',dns:'dns',diagnostics:'dns',workers:'workers'})){
  location.pathname='/admin/services/routepilot/'+alias;replaced='';await view.load();assert.equal(replaced,'/admin/services/routepilot/cloudflare/'+section);
 }
 throwHints=true;location.pathname='/admin/services/routepilot/cloudflare/devices';const data=await view.load();assert.equal(data[1],null);
 console.log('Independent page load: scoped reads; slow directory/protocol/runtime metadata excluded; optional hints isolated; legacy URLs preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});

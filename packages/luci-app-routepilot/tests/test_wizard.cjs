const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const dir=path.join(__dirname,'../htdocs/luci-static/resources/routepilot');
class Base{};Base.extend=props=>{class Helper{};Object.assign(Helper.prototype,props);return Helper;};
const Helper=new Function('baseclass',fs.readFileSync(path.join(dir,fs.readdirSync(dir).find(n=>/^feedback_.*\.js$/.test(n))),'utf8'))(Base);
const h=new Helper(),now=1000000;
const config={organization:'team',exit:'usa',exit_id:'11111111-1111-4111-8111-111111111111',nonce:'n1',scope:'lan',lans:1};
const catalog={available:true,read_at:now,last_success_at:now,networks:[{id:config.exit_id}]};
const status={state:'running',time:1000,exit:'usa',exit_ip:'192.0.2.1',runtime_id:'r1',generation:'g1',applied_nonce:'n1'};
const test={...status,state:'done',finished_at:1000,started_at:999,results:['google','youtube','github','taobao','baidu'].map(id=>({id,state:'ok'}))};
const verify=(t=test,s=status,c=config,n=now)=>h.wizardVerification(t,s,c,n);
assert.equal(verify(),'success');
for(const t of [null,{...test,state:'running',started_at:800},{...test,results:test.results.slice(1)},{...test,results:[...test.results.slice(1),test.results[1]]},{...test,results:[...test.results.slice(1),{id:'google',state:'pending'}]},{...test,finished_at:600},{...test,stale:true},{...test,runtime_id:'r2'},{...test,applied_nonce:'n2'}])assert.notEqual(verify(t),'success');
assert.equal(verify({...test,results:test.results.map((r,i)=>i? r:{...r,state:'restricted'})}),'warning');
assert.equal(verify({...test,results:test.results.map((r,i)=>i? r:{...r,state:'failed'})}),'error');
assert.equal(verify(test,{...status,time:900}),'pending');
assert.equal(verify(test,status,{...config,nonce:'n2'}),'stale');
let steps=h.wizardSteps(config,catalog,{...status,state:'preparing'},null,{},now);
assert.equal(steps.length,3);assert.deepEqual(steps.map(x=>x.state),Array(3).fill('success'));
steps=h.wizardSteps(config,catalog,null,null,{},now+61000);assert.equal(steps[2].state,'pending');assert.equal(steps[2].reason,'access_previous');
steps=h.wizardSteps(config,{...catalog,available:false},null,null,{},now);assert.equal(steps[2].state,'pending');

// First setup follows saved configuration and fresh runtime evidence.
const setupConfig={...config,backend:'masque',enabled:false,devices:0};
const setupStatus={...status,backend:'masque',scope:'lan'};
const setupEvidence=(saved,live,registered,runtime)=>h.firstSetup({config:saved,status:live,registered,runtime,now});
const firstSetup=(saved,live,registered)=>setupEvidence(saved,live,registered).stage;
assert.equal(firstSetup({...setupConfig,exit:'',exit_id:'',nonce:''},null,false),'account');
assert.equal(firstSetup({...setupConfig,exit:'',exit_id:'',nonce:''},null,true),'routing');
assert.deepEqual(setupEvidence(setupConfig,null,true,{installed:false}),{stage:'install',reason:'install'});
assert.equal(setupEvidence(setupConfig,null,true,{installed:false,runtime_exists:true}).reason,'runtime_review');
assert.equal(setupEvidence(setupConfig,null,true,{installed:true,recovery_required:true}).reason,'runtime_review');
assert.deepEqual(h.firstSetup({config:setupConfig,registered:true,runtimePending:true,now}),
  {stage:'install',reason:'runtime_check'},'runtime verification comes before account and saved preferences');
assert.deepEqual(h.firstSetup({config:setupConfig,registered:true,runtimeUnavailable:true,now}),
  {stage:'install',reason:'runtime_unavailable'},'unverified installation cannot advance the power switch');
assert.equal(setupEvidence(setupConfig,null,false,{installed:true,registration_files_present:false}).stage,'account',
  'importing a saved exit and scope does not register a new router');
assert.equal(setupEvidence(setupConfig,null,false,{installed:true,registration_files_present:true}).stage,'power',
  'an existing profile can start even if its directory is unavailable while disabled');
assert.equal(setupEvidence({...setupConfig,exit_id:''},null,false,{installed:true,registration_files_present:true}).reason,'account_check');
assert.deepEqual(h.firstSetup({config:setupConfig,registered:true,networks:[],now}),
  {stage:'account',reason:'catalog_empty'},'a read with no options cannot advance new setup');
assert.equal(h.firstSetup({config:setupConfig,registered:true,networks:[{id:'other'}],now}).reason,'exit_unavailable');
const afterRegistration={config:setupConfig,registered:true,registrationCompleted:true,now};
assert.equal(h.firstSetup(afterRegistration).reason,'catalog_pending');
assert.equal(h.firstSetup({...afterRegistration,directoryChecked:true,directoryPending:true}).reason,'catalog_pending');
assert.equal(h.firstSetup({...afterRegistration,directoryChecked:true}).reason,'catalog_failed');
assert.equal(h.firstSetup({...afterRegistration,config:{...setupConfig,preparing:true},directoryChecked:true,directoryExpired:true}).reason,'catalog_expired');
assert.equal(h.firstSetup({...afterRegistration,networks:catalog.networks}).stage,'power');
assert.equal(h.wizardSteps(config,{available:false},null,null,{},now,true)[2].state,'success',
  'register success remains visible during a directory read failure');
assert.equal(setupEvidence({...setupConfig,enabled:true},setupStatus,true,{installed:false}).stage,'checks',
  'an inconsistent package read must not hide a current connection');
assert.equal(firstSetup({...setupConfig,nonce:''},null,true),'routing',
  'selected values without a saved revision must not advance to power');
assert.equal(firstSetup({...setupConfig,scope:'devices',lans:0,devices:0},null,true),'routing');
assert.equal(firstSetup({...setupConfig,scope:'lan',lans:0},null,true),'routing');
assert.equal(firstSetup(setupConfig,{...setupStatus,state:'preparing'},true),'power');
assert.equal(firstSetup(setupConfig,{...setupStatus,state:'disabled'},false),'power',
  'ending login preparation tears down the directory, but saved setup still allows power on');
assert.equal(firstSetup({...setupConfig,enabled:true},null,true),'connecting');
assert.equal(firstSetup({...setupConfig,enabled:true},setupStatus,true),'checks');
assert.equal(firstSetup({...setupConfig,enabled:true},setupStatus,false),'checks',
  'a fresh applied WARP connection is valid evidence after a page reload');
for(const live of [
  {...setupStatus,time:900},
  {...setupStatus,applied_nonce:'other'},
  {...setupStatus,exit:'seoul'},
  {...setupStatus,backend:'worker'},
  {...setupStatus,exit_ip:''},
  {...setupStatus,runtime_id:''},
  {...setupStatus,tunnel_status:'unknown'},
  {...setupStatus,state:'waiting_devices'},
]) assert.notEqual(firstSetup({...setupConfig,enabled:true},live,true),'checks');
const enabledSetup={...setupConfig,enabled:true};
const failedSetup={...setupStatus,state:'error',attempted_nonce:setupConfig.nonce};
assert.equal(setupEvidence(enabledSetup,failedSetup,true).reason,'failed');
assert.notEqual(setupEvidence(enabledSetup,{...failedSetup,attempted_nonce:'old-attempt'},true).reason,'failed');
assert.equal(setupEvidence(enabledSetup,{...setupStatus,state:'waiting_devices'},true).reason,'waiting_devices');

const directFailure={...test,results:test.results.map(r=>r.id==='baidu'?{...r,state:'failed'}:r)};
assert.equal(h.wizardVerification(directFailure,status,config,now),'error');assert.equal(h.wizardProxyVerification(directFailure,status,config,now),'success');
const dns={schema:1,state:'running',time:1000,runtime_id:'r1',generation:'g1',process_instance:'p1',upstreams:{},policies:[{index:1,cache_entries:0,cache_hits:0,requests:0,errors:0}]};
const dt={...test,process_instance:'p1',results:['domestic','proxy'].flatMap(target=>['udp','tcp'].map(protocol=>({policy:1,target,protocol,state:'ok'})))};
assert.equal(h.dnsState(dns,status,now),'running');assert.equal(h.dnsTestState(dt,status,dns,now),'success');
assert.equal(h.dnsState({...dns,time:900},status,now),'stale');assert.notEqual(h.dnsState({...dns,generation:undefined},{...status,generation:undefined},now),'running');assert.equal(h.dnsState({...dns,policies:[{cache_entries:-1}]},status,now),'unavailable');
assert.equal(h.dnsTestState(dt,status,{...dns,process_instance:'new'},now),'stale');
assert.equal(h.dnsTestState({...dt,results:dt.results.slice(1)},status,dns,now),'stale');
assert.equal(h.dnsTestState({...dt,results:[dt.results[0],dt.results[0],...dt.results.slice(2)]},status,dns,now),'stale');
assert.equal(h.dnsTestState({...dt,results:dt.results.map((r,i)=>i?r:{...r,state:'failed'})},status,dns,now),'error');
assert.equal(h.dnsTestState({...dt,results:dt.results.map((r,i)=>i?r:{...r,state:'warning'})},status,dns,now),'warning');
console.log('Account guide, first setup, independent DNS evidence and runtime freshness passed');

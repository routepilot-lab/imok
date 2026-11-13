// Regression: QA-002/003 — lost or malformed snapshots must not retain a green result.
// Found by /qa on 2026-11-09; report: docs/quality/system-audit-20261109.md
// Value: protects=unknown diagnostics stay unknown; fails_when=old success is retained or malformed rows throw;
// why_new=existing view tests only exercise readable well-formed snapshots; seam=none;
// no_claim=real network reachability (synthetic filesystem and DOM).
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const testdir=__dirname;
const harness=fs.readFileSync(path.join(testdir,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
const cases=String.raw`
(async()=>{
 location.pathname='/admin/services/routepilot/overview';
 const good={...current,state:'done',finished_at:Date.now()/1000,results:[{id:'google',state:'ok',message:'可连接',ms:1}]};
 const faults=[];
 for(const [name,bad] of [['missing',null],['wrong rows',{...good,results:{}}],['null row',{...good,results:[null]}]]){
  test=good;const page=await new View().render([null,null,[],directory]);await tick();
  const card=page.find(e=>e.tag==='div'&&e.children.some(c=>c instanceof Element&&c.tag==='h3'&&c.textContent==='网站连通性检测'));
  assert.ok(card.textContent.includes('可连接'));
  test=bad;
  try{await tick();if(card.textContent.includes('可连接'))faults.push(name+': retained previous success');if(!/未取得|不可用|未检测/.test(card.textContent))faults.push(name+': missing unavailable feedback');}
  catch(error){faults.push(name+': '+error.name);}
  current={...current,time:Date.now()/1000};
 }
 for(const bad of [{results:{}},{state:'done',results:[null]},null]){
  try{feedback.wizardTestToken(bad);if(feedback.wizardVerification(bad,current,{nonce:'n1',exit:'usa'},Date.now())==='success')faults.push('invalid helper success');}
  catch(error){faults.push('helper: '+error.name);}
 }

 location.pathname='/admin/services/routepilot/cloudflare/dns';
 dnsData={...current,schema:1,state:'running',process_instance:'dns-1',control_available:true,upstreams:{dns_domestic:'223.5.5.5',dns_foreign:'1.1.1.1'},policies:[{index:0,cache_entries:0,cache_hits:0,requests:0,errors:0}]};
 const goodDNS={...current,state:'done',finished_at:Date.now()/1000,process_instance:'dns-1',results:['domestic','proxy'].flatMap(target=>['udp','tcp'].map(protocol=>({policy:0,target,protocol,state:'ok',domain:'example.test',upstream:'1.1.1.1',message:'解析通过'})))};
 for(const bad of [null,{...goodDNS,results:[null]},{...goodDNS,results:{}}]){
  test=good;dnsTests=goodDNS;const page=await new View().render([null,null,[],directory]);await tick();
  assert.ok(page.textContent.includes('本机受管 DNS 入口解析通过'));
  dnsTests=bad;try{await tick();if(page.textContent.includes('本机受管 DNS 入口解析通过'))faults.push('DNS retained success');}catch(error){faults.push('DNS: '+error.name);}
 }
 dnsData={...dnsData,policies:[null]};try{if(feedback.dnsState(dnsData,current,Date.now())==='running')faults.push('invalid DNS policy running');}catch(error){faults.push('DNS policy: '+error.name);}

 location.pathname='/admin/services/routepilot/cloudflare/workers';config.backend='masque';test=null;dnsTests=null;
 const live={...current};
 for(const status of [null,{...live,time:Date.now()/1000-120},{...live,state:'disabled',time:Date.now()/1000}]){
  current=status;const page=await new View().render([null,null,[],directory,null,{nodes:[]}]);await tick();
  if(page.textContent.includes('当前使用主线路，备用未接管。'))faults.push('Workers falsely reports active main line');
 }
 assert.deepEqual(faults,[]);
 console.log('Diagnostic snapshots: missing/malformed data clears previous success, stays unknown and does not break polling');
})().catch(e=>{console.error(e);process.exitCode=1;});`;
vm.runInNewContext(harness+cases,{require,__dirname:testdir,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

// Real view regression: independent settings are visible without disclosure toggles.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const harness=fs.readFileSync(path.join(__dirname,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
const cases=String.raw`
(async()=>{
 const beforeConfig=JSON.stringify(config),calls=[],writes=[],requests={};
 const originalExec=mockedFS.exec.bind(mockedFS),originalWrite=mockedFS.write.bind(mockedFS);
 const ruleRevision='a'.repeat(64),ruleInfo={current:{revision:ruleRevision,time:1,counts:{china:1},sources:{china:'https://example.com/china'},hashes:{china:'b'.repeat(64)},changes:{china:{added:1,removed:0}}},application:{state:'applied',revision:ruleRevision},schedule:{hours:168,state:'scheduled',last_attempt_at:1,last_success_at:1,next_due_at:9999999999,retry_count:0,retry_limit:3}};
 let delayedEndpoint=false,releaseEndpoint;
 mockedFS.write=(p,text,mode)=>{writes.push(p);const value=JSON.parse(text);if(p.includes('/requests/rules-input-')){requests[value.request_id]=value.payload;return Promise.resolve();}return originalWrite(p,text,mode);};
 mockedFS.exec=(p,args)=>{
  if(p==='/usr/libexec/routepilot'){
   calls.push(args[0]);
   if(args[0]==='endpoint-info'&&delayedEndpoint)return new Promise(resolve=>{releaseEndpoint=()=>resolve({code:0,stdout:JSON.stringify(endpointResponse)});});
   if(args[0]==='rules-info')return Promise.resolve({code:0,stdout:JSON.stringify(ruleInfo)});
   if(args[0]==='rules-query'){const id=args[1].split(':')[0],q=requests[id];return Promise.resolve({code:0,stdout:JSON.stringify({request_id:id,...q,total:1,pages:1,entries:['example.com']})});}
  }
  if(p==='/usr/libexec/routepilot-exits')return Promise.resolve({code:0,stdout:JSON.stringify(directory)});
  return originalExec(p,args);
 };
 function nodes(root){const result=[];function visit(e){if(!(e instanceof Element))return;result.push(e);e.children.forEach(visit);}visit(root);return result;}
 const groups={account:'advanced',endpoint:'endpoint',routing:'basic',devices:'devices',dns:'dns',workers:'workers'};
 for(const [page,id] of Object.entries(groups)){
  location.pathname='/admin/services/routepilot/cloudflare/'+page;
  const before=calls.filter(x=>x==='endpoint-info').length,beforeWrites=writes.length;
  const node=await new View().render([null,null,[],directory,null,{nodes:[]},warpReply]);await new Promise(setImmediate);
  const ownNav=node.find(e=>e.attrs['aria-label']==='Cloudflare 设置页面'),selected=nodes(ownNav).filter(e=>e.tag==='a'&&e.attrs['aria-current']==='page');assert.equal(selected.length,1);assert.equal(selected[0].attrs.href,'/admin/services/routepilot/cloudflare/'+page);
  const panel=node.find(e=>e.attrs.id==='rp-center-'+id);assert.ok(panel,page+' panel exists');assert.equal(panel.tag,'section',page+' main settings cannot collapse');assert.equal(panel.children.some(e=>e instanceof Element&&e.tag==='summary'),false,'main panel has a heading rather than a disclosure toggle');
  assert.ok(panel.attrs['aria-label'],'independent section retains its accessible label without a duplicate visible page heading');assert.ok(!panel.find(e=>e.tag==='h3'&&e.attrs.class==='rp-center-heading'));
  for(const e of nodes(node).filter(e=>['h2','h3','h4'].includes(e.tag)))assert.ok(!/^\s*(?:[1-5]\.|[①②③④⑤]|第\s*\d\s*步)/.test(e.textContent),'settings heading has no workflow number: '+e.textContent);
  if(page==='account'){
   for(const id of ['org','prepare','auth']){const item=panel.find(e=>e.attrs['data-step']===id);assert.equal(item.tag,'section');assert.equal(item.children.some(e=>e instanceof Element&&e.tag==='summary'),false);const help=item.find(e=>e.attrs.class==='rp-setting-help');assert.ok(help,'long instructions remain available as help');assert.ok(!help.find(e=>String(e.attrs.id||'').startsWith('cbi-routepilot-main-')),'real settings stay outside long help');}
   assert.equal(panel.find(e=>e.attrs['data-step']==='intro').tag,'details','long installation help may remain a disclosure');
   assert.ok(panel.find(e=>e.tag==='button'&&e.textContent==='保存组织名称'));
   assert.ok(panel.find(e=>e.tag==='button'&&e.textContent==='邮箱登录'));
  }
  if(page==='endpoint'){
   const settings=panel.find(e=>e.attrs.class==='rp-guide-step rp-endpoint-card');assert.equal(settings.tag,'section');assert.equal(settings.querySelector('summary'),null);
   assert.ok(settings.find(e=>e.tag==='button'&&e.textContent==='应用并检测（可能重连）'));
   assert.equal(calls.filter(x=>x==='endpoint-info').length-before,1,'entering endpoint reads exactly once without toggling');
   assert.equal(writes.length,beforeWrites,'opening endpoint does not submit configuration or endpoint changes');
   await tick();await tick();await lastMap.renderContents();assert.equal(calls.filter(x=>x==='endpoint-info').length-before,1,'polling/rerender does not repeat initial read');
   const mode=settings.find(e=>e.tag==='select'&&e.children.some(c=>c instanceof Element&&c.attrs.value==='custom'));
   const custom=settings.find(e=>e.tag==='input'&&e.attrs.placeholder==='IPv4:端口');mode.value='custom';mode.listeners.change();custom.value='162.159.197.2:8443';custom.listeners.input();await lastMap.renderContents();
   assert.equal(settings.find(e=>e.tag==='input'&&e.attrs.placeholder==='IPv4:端口'),custom);assert.equal(custom.value,'162.159.197.2:8443');
  }
  if(page==='workers'){
   for(const id of ['deploy','relay','scan','use']){const stage=panel.find(e=>e.attrs['data-stage']===id);assert.equal(stage.tag,'section');assert.ok(stage.find(e=>e.tag==='h3'&&e.attrs.class==='rp-worker-stage-heading'));assert.equal(stage.children.some(e=>e instanceof Element&&e.tag==='summary'),false);}
   const importSettings=nodes(panel).find(e=>e.tag==='section'&&e.children.some(c=>c instanceof Element&&c.tag==='h4'&&c.textContent==='备用节点设置（手动导入 / 调整入口）'));assert.ok(importSettings);
   assert.ok(panel.find(e=>e.attrs['aria-label']==='Cloudflare 账户 ID'),'manual account setting is directly available');
  }
  if(page==='dns')assert.ok(panel.find(e=>e.tag==='h3'&&e.textContent==='WARP 客户端 UDP 接收观测'));
 }
 location.pathname='/admin/services/routepilot/rules';const rules=await new View().render([null,null,[]]);await new Promise(setImmediate);
 for(const name of ['当前下载版本来源与校验摘要','本次订阅更新变化']){const section=nodes(rules).find(e=>e.tag==='section'&&e.children.some(c=>c instanceof Element&&c.tag==='h4'&&c.textContent===name));assert.ok(section,name+' is visible without expanding');}
 location.pathname='/admin/services/routepilot/rules/manage';const managed=await new View().render([null,null,[]]);await new Promise(setImmediate);
 assert.equal(nodes(managed).filter(e=>e.attrs.class==='rp-summary-actions rp-rule-controls').length,3,'responsive rule filters and personal additions have dedicated classes');
 // A slow initial read remains single-flight through native poll and map rerenders.
 delayedEndpoint=true;location.pathname='/admin/services/routepilot/cloudflare/endpoint';const count=calls.filter(x=>x==='endpoint-info').length;
 const pending=await new View().render([null,null,[]]);await new Promise(setImmediate);await tick();await lastMap.renderContents();assert.equal(calls.filter(x=>x==='endpoint-info').length-count,1);releaseEndpoint();await new Promise(setImmediate);await tick();assert.ok(pending.textContent.includes('客户端配置的固定入口'));
 assert.equal(JSON.stringify(config),beforeConfig,'displaying all panels does not save UCI');
 assert.ok(calls.every(action=>['endpoint-info','warp-info','rules-info','rules-query'].includes(action)),'displaying sections never invokes mutation commands');
 assert.ok(writes.every(p=>p.includes('/requests/rules-input-')),'only bounded read-query staging is written');
 console.log('Flat settings: six independent sections, visible controls, no numbered headings, single-flight read-only endpoint load and preserved drafts passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

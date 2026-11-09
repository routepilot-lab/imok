// Behavior of the actual view under isolated runtime fixtures, not a second UI.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const harness=fs.readFileSync(path.join(__dirname,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
const cases=String.raw`
(async()=>{
 location.pathname='/admin/services/routepilot/overview';
 let traffic={schema:1,state:'observing',time:Date.now()/1000,today:'2026-11-08',days:{'2026-11-08':{bytes:{proxy_up:10,proxy_down:20,direct_up:30,direct_down:40}}},total:{proxy_up:100,proxy_down:200,direct_up:300,direct_down:400},history:[],offload:'enabled'};
 const originalRead=mockedFS.read.bind(mockedFS),originalExec=mockedFS.exec.bind(mockedFS);
 mockedFS.read=p=>p.endsWith('/traffic.json')?Promise.resolve(JSON.stringify(traffic)):originalRead(p);
 let powerCalls=[];mockedFS.exec=(p,args)=>{if(p==='/usr/libexec/routepilot'&&['disable','enable'].includes(args[0])){powerCalls.push(args[0]);return Promise.resolve({code:0,stdout:JSON.stringify({enabled:args[0]==='enable',nonce:'power-one'})});}return originalExec(p,args);};
 current={...current,protocol:'MASQUE',scope:'lan',devices:[{}],state:'running',time:Date.now()/1000};config.enabled='1';config.apply_nonce=current.applied_nonce;
 const before=JSON.stringify(config),node=await new View().render([null,null,[],directory,null,{nodes:[]},null,{package:'preview'}]);await new Promise(setImmediate);await tick();
 const facts=node.find(e=>e.attrs.class==='imok-overview-facts');assert.ok(facts);assert.ok(facts.textContent.includes('MASQUE'));assert.ok(facts.textContent.includes('当前配置已确认应用'));
 assert.equal(JSON.stringify(config),before,'render/poll never saves configuration');assert.deepEqual(powerCalls,[]);
 assert.ok(node.textContent.includes('可能远低于实际用量'));assert.ok(node.textContent.includes('30 B'));assert.ok(node.textContent.includes('300 B'));assert.ok(node.textContent.includes('70 B'));assert.ok(node.textContent.includes('700 B'));
 const cumulative=node.find(e=>e.tag==='button'&&e.textContent==='累计');cumulative.attrs.click();assert.equal(cumulative.attrs['aria-pressed'],'true');assert.deepEqual(powerCalls,[],'stat range changes are display only');
 test={...test,state:'done',stale:false,finished_at:Date.now()/1000,results:['google','youtube','github','taobao','baidu'].map(id=>({id,state:id==='google'?'failed':'ok'}))};await tick();
 assert.ok(node.textContent.includes('隧道已连接'));assert.ok(facts.textContent.includes('所测网站存在失败'),'website failure cannot become a tunnel failure or success');
 current={...current,time:Date.now()/1000-120};await tick();assert.ok(node.textContent.includes('状态未知'));assert.ok(facts.textContent.includes('尚无有效检测'));
 traffic={...traffic,days:{},total:{}};await tick();assert.ok(node.textContent.includes('此时段没有采集数据'));
 const off=node.find(e=>e.tag==='button'&&e.textContent==='关闭 imOK');off.attrs.click();await new Promise(setImmediate);assert.deepEqual(powerCalls,['disable'],'one click invokes only existing disable command');current={...current,time:Date.now()/1000,state:'disabled',applied_nonce:'power-one'};await tick();assert.ok(facts.textContent.includes('当前配置已确认应用'),'display binds the acknowledged power request without changing the power handler');
 console.log('Overview v2: independent status, unknown/stale data, all four totals, display-only range and single power command passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

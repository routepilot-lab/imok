// Use the real account view, synthetic runtime status, and an in-memory request receiver.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const harness = fs.readFileSync(path.join(__dirname, 'test_feedback_render.cjs'), 'utf8').split('(async()=>{')[0];
const cases = String.raw`
(async()=>{
  location.pathname='/admin/services/routepilot/cloudflare/account';
  const writes=[],commands=[],originalExec=mockedFS.exec.bind(mockedFS),originalWrite=mockedFS.write.bind(mockedFS);
  mockedFS.write=(target,value)=>{
    if(target.includes('speed-compare-input-')){writes.push(JSON.parse(value).payload);return Promise.resolve();}
    return originalWrite(target,value);
  };
  mockedFS.exec=(target,args)=>{
    if(target==='/usr/libexec/routepilot'&&args[0]==='speed-compare'){
      commands.push(args);
      return Promise.resolve({code:0,stdout:JSON.stringify({state:"submitted",id:"synthetic",request_id:args[1].split(":")[0]})});
    }
    return originalExec(target,args);
  };
  current={...current,time:Date.now()/1000,backend:'masque',protocol:'MASQUE',protocol_managed:true,state:'running'};
  const view=await new View().render([null,null,[],directory,null,{nodes:[]},null,{}]);
  await tick();
  const start=view.find(e=>e.tag==='button'&&e.textContent==='对比 MASQUE 与 WireGuard');
  assert.ok(start.disabled);
  assert.ok(view.textContent.includes('协议由 Cloudflare 组织管理，无法切换测速'));
  current={...current,time:Date.now()/1000,protocol_managed:false};
  await tick();
  assert.ok(!start.disabled);
  start.attrs.click();
  assert.deepEqual(commands,[]);
  assert.ok(lastModal.textContent.includes('受保护设备保持断网'));
  assert.ok(lastModal.textContent.includes('400 MB'));
  const confirm=lastModal.find(e=>e.tag==='button'&&e.textContent==='开始测速');
  await confirm.attrs.click();
  assert.equal(commands.length,1);
  assert.equal(JSON.stringify(writes),JSON.stringify([{kind:'protocol',targets:['MASQUE','WireGuard']}]));
  assert.ok(start.disabled);
  const read=mockedFS.read.bind(mockedFS);
  mockedFS.read=(target)=>target.endsWith('/speed-compare.json')
    ? Promise.resolve(JSON.stringify({
      id:'synthetic',kind:'protocol',state:'done',targets:['MASQUE','WireGuard'],rows:[
        {target:'MASQUE',state:'done',latency_median_ms:40,average_mbps:260,exit_ip:'9.9.9.9',
          cpu_busy_percent:25.5,cpu_busiest_core_percent:98}
      ]
    }))
    : read(target);
  await tick();
  assert.ok(view.textContent.includes('路由器 CPU 平均 25.5%'));
  assert.ok(view.textContent.includes('最忙核心 98%'));
  console.log('Protocol comparison: managed gate, explicit confirmation, traffic estimate and bound request passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

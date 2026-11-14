// Drive the real DNS view with an isolated LuCI form and runtime file.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const harness = fs
  .readFileSync(path.join(__dirname, 'test_feedback_render.cjs'), 'utf8')
  .split('(async()=>{')[0];
const cases = String.raw`
(async()=>{
  location.pathname='/admin/services/routepilot/cloudflare/dns';
  config.warp_dns_mode='auto';
  config.dns_transport='tcp';
  config.dns_domestic='223.5.5.5';
  config.dns_foreign='1.1.1.1';
  current={...current,backend:'masque'};
  const section=Map.prototype.section;
  Map.prototype.section=function(...args){
    const item=section.apply(this,args), original=item.option;
    item.option=function(...fields){
      const option=original.apply(this,fields);
      option.getUIElement=()=>({setValue(value){selections[option.option]=value;}});
      return option;
    };
    return item;
  };
  let persisted=0, submissions=[], lanCommands=[], result, lanResult, proxyResult, validationError=false;
  Map.prototype.save=function(callback){
    for(const name of ['warp_dns_mode','warp_dns_pinned','dns_domestic','dns_foreign','dns_transport']) uci.set('routepilot','main',name,selections[name]);
    callback();
    return Promise.resolve();
  };
  uci.save=()=>{persisted++;return Promise.resolve();};
  const read=mockedFS.read.bind(mockedFS),write=mockedFS.write.bind(mockedFS),exec=mockedFS.exec.bind(mockedFS);
  mockedFS.read=(name)=>name.endsWith('/warp-dns-tests.json')?Promise.resolve(JSON.stringify(result||{})):
    name.endsWith('/lan-dns-domestic.json')?Promise.resolve(JSON.stringify(lanResult||{})):
    name.endsWith('/lan-dns-foreign.json')?Promise.resolve(JSON.stringify(proxyResult||{})):read(name);
  mockedFS.write=(name,value)=>{
    if(name.includes('warp-dns-input-')){
      submissions.push(JSON.parse(value).payload);
      return Promise.resolve();
    }
    return write(name,value);
  };
  mockedFS.exec=(name,args)=>{
    if(name==='/usr/libexec/routepilot'&&args[0].startsWith('lan-dns-')){
      lanCommands.push(args[0]);return Promise.resolve({code:0,stdout:'{"state":"submitted"}'});
    }
    if(name==='/usr/libexec/routepilot'&&args[0]==='warp-dns-test')
      return Promise.resolve({code:0,stdout:JSON.stringify({state:'submitted',request_id:args[1].split(':')[0]})});
    if(name==='/usr/libexec/routepilot'&&args[0]==='warp-dns-validate')
      return Promise.resolve(validationError
        ? {code:1,stderr:'地址不能指向路由器自身'}
        : {code:0,stdout:JSON.stringify({request_id:args[1].split(':')[0]})});
    return exec(name,args);
  };
  const view=new View();
  const page=await view.render([null,null,[],directory,null,{nodes:[]},null,{}]);
  const button=(label)=>page.find(e=>e.tag==='button'&&e.textContent===label);
  const advanced=page.find(e=>e.tag==='details'&&e.children[0].textContent==='高级 · WARP 启动解析');
  assert.ok(advanced&&!advanced.open);
  assert.ok(advanced.textContent.includes('首次安装可保留「自动优选」'));
  const overview=page.find(e=>e.tag==='a'&&e.textContent==='前往总览');
  current={...current,state:'preparing',time:Date.now()/1000};
  await tick();
  assert.ok(button('清理本插件 DNS 缓存').disabled);
  assert.ok(button('检测当前 DNS 解析').disabled);
  assert.ok(page.textContent.includes('请在总览结束登录准备'));
  assert.equal(overview.attrs.href,'/admin/services/routepilot/overview');
  assert.ok(!overview.hidden);
  current={...current,state:'running',time:Date.now()/1000};
  dnsData={schema:1,state:'running',time:current.time,runtime_id:current.runtime_id,generation:current.generation,
    process_instance:'dns-synthetic',control_available:true,upstreams:{dns_domestic:'223.5.5.5',dns_foreign:'1.1.1.1',transport:'tcp'},
    policies:[{index:1,cache_entries:0,cache_hits:0,requests:0,errors:0}]};
  await tick();
  assert.ok(!button('清理本插件 DNS 缓存').disabled);
  assert.ok(!button('检测当前 DNS 解析').disabled);
  assert.ok(overview.hidden);
  await button('测试 WARP 启动解析').attrs.click();
  assert.equal(JSON.stringify(submissions),JSON.stringify([{addresses:[]}]));
  result={state:'done',finished_at:Date.now()/1000,rows:[
    {group:'海外',name:'Cloudflare',address:'1.1.1.1',ms:10},
    {group:'海外',name:'Google',address:'8.8.8.8',ms:null,reason:'timeout'}
  ]};
  await tick();
  button('使用这个').attrs.click();
  assert.equal(selections.warp_dns_mode,'pinned');
  assert.equal(selections.warp_dns_pinned,'1.1.1.1');
  assert.equal(config.warp_dns_mode,'auto');
  assert.equal(persisted,0);
  validationError=true;
  assert.equal(await view.applySettings(),false);
  assert.equal(config.warp_dns_mode,'auto');
  assert.equal(persisted,0);
  validationError=false;
  assert.ok((await view.applySettings()).nonce);
  assert.equal(config.warp_dns_mode,'pinned');
  assert.equal(config.warp_dns_pinned,'1.1.1.1');
  assert.equal(persisted,1);
  assert.equal(JSON.stringify(submissions[2]),JSON.stringify({
    warp_dns_mode:'pinned',warp_dns_pinned:'1.1.1.1',warp_dns_custom:[]
  }));
  result=null;
  proxyResult={state:'done',transport:'tcp',finished_at:Date.now()/1000,rows:[
    {group:'国内',name:'腾讯 DNSPod',address:'119.29.29.29',ms:20,udp:{ms:19},tcp:{ms:20}}
  ]};
  lanResult={state:'done',transport:'tcp',wan_dns:'included',finished_at:Date.now()/1000,rows:[
    {group:'宽带',name:'运营商下发',address:'192.168.0.1',ms:null,reason:'timeout',udp:{ms:6.22},tcp:{ms:null,reason:'timeout'}},
    {group:'国内',name:'阿里',address:'223.5.5.5',ms:8,udp:{ms:4},tcp:{ms:8}},
    {group:'国内',name:'腾讯 DNSPod',address:'119.29.29.29',ms:10,udp:{ms:7},tcp:{ms:10}},
    {group:'国内',name:'不可用',address:'114.114.114.114',ms:null,udp:{ms:null},tcp:{ms:null}}
  ]};
  await tick();
  const resultPanel=(title)=>page.find(e=>e.tag==='details'&&e.children[0].textContent===title);
  const domestic=resultPanel('国内 DNS 候选结果'), proxy=resultPanel('代理 DNS 候选结果');
  assert.ok(domestic && proxy,'each test has its own labeled result panel');
  const row=(panel,address)=>panel.find(e=>e.tag==='tr'&&e.children.some(c=>c.textContent===address));
  const peer=row(domestic,'192.168.0.1');
  assert.ok(peer.textContent.includes('6.22 ms'));
  assert.ok(peer.textContent.includes('超时'));
  assert.equal(row(domestic,'114.114.114.114').find(e=>e.tag==='button'),null);
  const domesticChoice=row(domestic,'119.29.29.29').find(e=>e.tag==='button');
  const proxyChoice=row(proxy,'119.29.29.29').find(e=>e.tag==='button');
  assert.equal(domesticChoice.textContent,'填入国内 DNS');
  assert.equal(proxyChoice.textContent,'填入代理 DNS');
  domesticChoice.attrs.click();
  assert.equal(selections.dns_domestic,'119.29.29.29');
  assert.equal(selections.dns_foreign,'1.1.1.1');
  proxyChoice.attrs.click();
  assert.equal(selections.dns_domestic,'119.29.29.29');
  assert.equal(selections.dns_foreign,'119.29.29.29');
  assert.equal(selections.warp_dns_pinned,'1.1.1.1');
  assert.equal(config.dns_foreign,'1.1.1.1');
  row(domestic,'223.5.5.5').find(e=>e.tag==='button').attrs.click();
  assert.equal(selections.dns_transport,'tcp');
  peer.find(e=>e.tag==='button').attrs.click();
  lastModal.find(e=>e.tag==='button'&&e.textContent==='取消').attrs.click();
  assert.equal(selections.dns_domestic,'223.5.5.5');
  assert.equal(selections.dns_transport,'tcp');
  peer.find(e=>e.tag==='button').attrs.click();
  lastModal.find(e=>e.tag==='button'&&e.textContent==='改为 UDP 并选用').attrs.click();
  assert.equal(selections.dns_domestic,'192.168.0.1');
  assert.equal(selections.dns_transport,'udp');
  assert.equal(config.dns_domestic,'223.5.5.5');
  assert.equal(config.dns_transport,'tcp');
  assert.equal(persisted,1);
  assert.ok((await view.applySettings()).nonce);
  assert.equal(config.dns_domestic,'192.168.0.1');
  assert.equal(config.dns_foreign,'119.29.29.29');
  assert.equal(config.dns_transport,'udp');
  assert.equal(persisted,2);
  await button('测试国内 DNS 候选').attrs.click();
  assert.equal(domestic.open,true);assert.equal(proxy.open,false);
  await button('测试代理 DNS 候选').attrs.click();
  assert.equal(domestic.open,false);assert.equal(proxy.open,true);
  assert.deepEqual(lanCommands,['lan-dns-domestic','lan-dns-foreign']);
  console.log('DNS candidates: UDP/TCP results, cancellation, and saved draft verified.');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(
  harness + cases,
  { require, __dirname, console, Buffer, TextEncoder, setImmediate, process },
  { filename: __filename }
);

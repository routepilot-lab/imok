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
  let persisted=0, submissions=[], result, lanResult, validationError=false;
  Map.prototype.save=function(callback){
    for(const name of ['warp_dns_mode','warp_dns_pinned','dns_domestic','dns_transport']) uci.set('routepilot','main',name,selections[name]);
    callback();
    return Promise.resolve();
  };
  uci.save=()=>{persisted++;return Promise.resolve();};
  const read=mockedFS.read.bind(mockedFS),write=mockedFS.write.bind(mockedFS),exec=mockedFS.exec.bind(mockedFS);
  mockedFS.read=(name)=>name.endsWith('/warp-dns-tests.json')?Promise.resolve(JSON.stringify(result||{})):
    name.endsWith('/lan-dns-domestic.json')?Promise.resolve(JSON.stringify(lanResult||{})):read(name);
  mockedFS.write=(name,value)=>{
    if(name.includes('warp-dns-input-')){
      submissions.push(JSON.parse(value).payload);
      return Promise.resolve();
    }
    return write(name,value);
  };
  mockedFS.exec=(name,args)=>{
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
  lanResult={state:'done',transport:'tcp',wan_dns:'included',finished_at:Date.now()/1000,rows:[
    {group:'宽带',name:'运营商下发',address:'192.168.0.1',ms:null,reason:'timeout',udp:{ms:6.22},tcp:{ms:null,reason:'timeout'}},
    {group:'国内',name:'阿里',address:'223.5.5.5',ms:8,udp:{ms:4},tcp:{ms:8}},
    {group:'国内',name:'不可用',address:'114.114.114.114',ms:null,udp:{ms:null},tcp:{ms:null}}
  ]};
  await tick();
  const row=(address)=>page.find(e=>e.tag==='tr'&&e.children.some(c=>c.textContent===address));
  const peer=row('192.168.0.1');
  assert.ok(peer.textContent.includes('6.22 ms'));
  assert.ok(peer.textContent.includes('超时'));
  assert.equal(row('114.114.114.114').find(e=>e.tag==='button'),null);
  row('223.5.5.5').find(e=>e.tag==='button').attrs.click();
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
  assert.equal(config.dns_transport,'udp');
  assert.equal(persisted,2);
  console.log('DNS candidates: UDP/TCP results, cancellation, and saved draft verified.');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(
  harness + cases,
  { require, __dirname, console, Buffer, TextEncoder, setImmediate, process },
  { filename: __filename }
);

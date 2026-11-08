// Use target LuCI child-array semantics, not a mock that silently drops null.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const harness=fs.readFileSync(path.join(__dirname,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
const cases=String.raw`
(async()=>{
 assert.equal(E('div',{},[null,undefined,0,false]).textContent,'null undefined 0 false');
 assert.equal(E('div',{},null).textContent,'');
 const nativeExec=mockedFS.exec.bind(mockedFS);mockedFS.exec=(p,args)=>p==='/usr/libexec/routepilot'&&args[0]==='rules-info'?Promise.resolve({code:0,stdout:JSON.stringify({current:{},schedule:{}})}):nativeExec(p,args);
 const pages=['overview','cloudflare/account','cloudflare/endpoint','cloudflare/routing','cloudflare/devices','cloudflare/dns','cloudflare/workers','rules/overview','rules/sources','rules/manage','rules/custom','backup'];
 for(const page of pages){
  location.pathname='/admin/services/routepilot/'+page;
  const node=await new View().render([null,null,[],directory,null,{nodes:[]}]);
  await tick();await new Promise(setImmediate);
  const leaks=node.textContent.match(/.{0,40}\b(?:null|undefined)\b.{0,40}/g)||[];
  assert.deepEqual(leaks,[],page+' must not expose missing presentation values');
 }
 console.log('All 12 pages: no null/undefined text with target LuCI child-array semantics');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

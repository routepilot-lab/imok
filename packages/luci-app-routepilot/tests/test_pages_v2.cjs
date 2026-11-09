// Exercise presentation changes in the real view; networking remains a bounded double.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const harness=fs.readFileSync(path.join(__dirname,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
const cases=String.raw`
(async()=>{
 Element.prototype.getAttribute=function(k){return this.attrs[k]??null;};
 location.pathname='/admin/services/routepilot/cloudflare/workers';
 const node=await new View().render([null,null,[],directory,null,{nodes:[]}]);await new Promise(setImmediate);
 const before=JSON.stringify(config),beforeCalls=commandCalls.length;
 const workspace=node.find(e=>e.attrs.class==='imok-worker-workspace');assert.ok(workspace);
 for(const stage of ['deploy','relay','scan','use']){
  const button=node.find(e=>e.attrs['aria-controls']==='imok-worker-'+stage);assert.ok(button);
  button.attrs.click();assert.equal(button.attrs['aria-pressed'],'true');
  assert.equal(node.find(e=>e.attrs.id==='imok-worker-'+stage).attrs['data-current'],'true');
  await tick();assert.equal(node.find(e=>e.attrs.id==='imok-worker-'+stage).attrs['data-current'],'true','poll does not move a manually selected stage');
 }
 const name=node.find(e=>e.attrs['aria-label']==='Worker 名称');name.value='routepilot-draft';
 node.find(e=>e.attrs['aria-controls']==='imok-worker-use').attrs.click();
 node.find(e=>e.attrs['aria-controls']==='imok-worker-deploy').attrs.click();assert.equal(name.value,'routepilot-draft');
 const all=node.find(e=>e.tag==='button'&&e.textContent==='展开全部设置');all.attrs.click();assert.equal(workspace.attrs['data-show-all'],'true');all.attrs.click();assert.equal(workspace.attrs['data-show-all'],'false');
 assert.equal(JSON.stringify(config),before);assert.equal(commandCalls.length,beforeCalls,'UI navigation never submits a command');
 for(const title of ['保存令牌并读取账户 / 域名','检查名称和子域名是否占用','一键创建 Workers 并绑定域名','检查部署与证书状态','清除本机 API 令牌','保存备用节点（不切换）','手动启用备用','切回主线路'])assert.ok(node.find(e=>e.tag==='button'&&e.textContent===title),title+' retained');
 location.pathname='/admin/services/routepilot/cloudflare/dns';const dns=await new View().render([null,null,[],directory]);
 const diagnostics=dns.find(e=>e.attrs.class==='imok-diagnostics');assert.equal(diagnostics.tag,'details');assert.ok(diagnostics.textContent.includes('WARP 客户端 UDP 接收观测'));
 const settings=dns.find(e=>e.attrs.class==='imok-panel');assert.ok(!settings.find(e=>e.tag==='details'),'editable DNS settings stay directly visible');
 const view=new View();await view.render([null,null,[],directory]);let modalClasses;const nativeModal=ui.showModal;ui.showModal=function(title,children,...classes){modalClasses=classes;return nativeModal(title,children);};uci.save=()=>Promise.resolve();view.map.save=()=>Promise.resolve();await view.applySettings();assert.deepEqual(modalClasses,['imok-dialog'],'only imOK modal requests its owner class');assert.ok(lastModal.find(e=>e.tag==='button'&&e.textContent==='关闭'),'original close action retained');
 console.log('Pages v2: stage selection, polling stability, retained drafts/actions and read-only diagnostics passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

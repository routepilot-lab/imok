// Real runtime module and native LuCI modal methods; all RPC data is synthetic.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..'),menu=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/luci/menu.d/luci-app-routepilot.json'))),view=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/view',menu['admin/services/routepilot'].action.path+'.js'),'utf8');
const name=view.match(/L.require\('routepilot\.(runtime_[a-f0-9]{12})'\)/)[1],source=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/routepilot',name+'.js'),'utf8');
class Element{
 constructor(tag,attrs={},children=[]){this.tag=tag;this.attrs=attrs;this.children=Array.isArray(children)?children:[children];this.disabled=attrs.disabled!=null;// Native LuCI DOM retains disabled=false as an HTML boolean attribute.
this.value=attrs.value||'';this.isConnected=true;this.handlers={};this.classList={add:(...v)=>{for(const t of v){assert.ok(t&&!/\s/.test(t));this.attrs.class=((this.attrs.class||'')+' '+t).trim();}},remove:(...v)=>{this.attrs.class=(this.attrs.class||'').split(' ').filter(t=>!v.includes(t)).join(' ');},contains:t=>(this.attrs.class||'').split(' ').includes(t)};}
 appendChild(c){this.children.push(c);}replaceChildren(...c){this.children=c;}setAttribute(k,v){this.attrs[k]=v;}addEventListener(k,v){this.handlers[k]=v;}click(){return this.attrs.click?.({target:this});}
 get textContent(){return this._text??this.children.map(c=>c instanceof Element?c.textContent:String(c??'')).join('');}set textContent(v){this._text=v;}
}
const E=(t,a,c)=>new Element(t,a,c),nodes=root=>[root,...root.children.filter(v=>v instanceof Element).flatMap(nodes)],find=(root,label)=>nodes(root).find(e=>e.tag==='button'&&e.textContent===label);
class Base{}Base.extend=function(p){class Module extends Base{}Object.assign(Module.prototype,p);return Module;};
function fixture(){return {schema:1,supported:true,installed:true,runtime_exists:true,local_version:'2026.8.2115.0',latest_version:'2026.9.1.0',update_available:true,settings:{hours:24},check:{state:'checked',checked_at:1700000000},job:{},backups:[],recovery_required:false,next_check_at:1700086400,automatic_install:false,environment:{can_prepare:true,issues:[],architecture:'aarch64',system:'OpenWrt 24.10',free_bytes:8*1073741824},online:{state:'idle'}};}
function setup(){
 let data=fixture(),writes=[],calls=[],uploads=[],tick,serial=0,statusError;const dlg=E('div'),doc={body:E('body')};
 const dom={create:E,content(el,ch){el.children=Array.isArray(ch)?ch:[ch];},append(el,ch){el.children.push(...(Array.isArray(ch)?ch:[ch]));}};
 const modalDiv={firstElementChild:dlg,focus(){},blur(){},querySelector(){return nodes(dlg).find(e=>e.tag==='button');}};
 const ui=require('./fixtures/luci_modal_contract.cjs')(modalDiv,dom,doc);
 const rpc={exec(file,args=[]){calls.push([file,args]);if(file.endsWith('runtime-status'))return statusError?Promise.reject(Error(statusError)):Promise.resolve({code:0,stdout:JSON.stringify(data)});if(args[0]==='prepare')return Promise.resolve({code:0,stdout:'{"ready":true}'});const req=JSON.parse(writes.at(-1)[1]);if(args[0]==='prepare-online')data={...data,online:{state:'ready',version:'2026.8.2115.0'},job:{state:'success',message:'模拟下载已准备，尚未安装'}};if(args[0]==='first-install')data={...data,job:{state:'running',phase:'download_base',message:'模拟 WARP 后台安装中'}};if(args[0]==='execute')data={...data,job:{state:'running',message:'模拟请求执行中'}};return Promise.resolve({code:0,stdout:JSON.stringify({request_id:req.request_id,...(args[0]==='plan'?{plan:{id:'b'.repeat(32),version:'2026.9.1.0',operation:req.payload.operation}}:{submitted:true})})});},write(...a){writes.push(a);return Promise.resolve();}};
 const Module=new Function('baseclass','fs','ui','poll','L','E','request','rpc','FormData',source)(Base,rpc,ui,{add(fn,interval){assert.equal(interval,2);tick=fn;}},{url:p=>'/'+p,env:{cgi_base:'/cgi-bin/luci'}},E,{post(url,form,options){uploads.push({url,fields:form.fields});options.progress({loaded:1,total:1});return Promise.resolve({json:()=>({size:1024})});}},{getSessionID:()=> 'SYNTHETIC session'},class Form{constructor(){this.fields={};}append(k,v){this.fields[k]=v;}}),module=new Module();
 const bind=payload=>{const id=(++serial).toString(16).padStart(32,'0');return {id,token:id+':synthetic',text:JSON.stringify({request_id:id,payload})};};
 return {module,render:(initial=data)=>module.render(initial,'style.css',bind,new Shared().when),rpc,ui,dlg,doc,calls,writes,uploads,setData:d=>{data=d;},setStatusError:e=>{statusError=e;},poll:()=>tick?.()};
}
const sharedName=view.match(/require routepilot\.(page_shared_[a-f0-9]{12}) as pageShared/)[1];
const Shared=new Function('baseclass','_',fs.readFileSync(path.join(root,'htdocs/luci-static/resources/routepilot',sharedName+'.js'),'utf8'))(Base,x=>x);
const primary=['overview','runtime','cloudflare','clients','rules','backup'];assert.deepEqual(primary.map((key)=>menu['admin/services/routepilot/'+key].order),[0,1,2,3,4,5],'Primary entries need unique predictable order');

async function assertFailedPlanOffersAutomaticRetry(noRuntime) {
 const runtime=setup();
 const ready={...noRuntime,online:{state:'ready',version:'2026.8.2115.0'},
  job:{state:'success',phase:'ready',message:'文件已准备好，尚未安装'}};
 runtime.setData(ready);
 const exec=runtime.rpc.exec;
 runtime.rpc.exec=function(file,args=[]){
  if(args[0]!=='plan')return exec.call(this,file,args);
  const message='准备文件校验失败，请重新准备；未安装';
  runtime.setData({...ready,online:{state:'expired',message}});
  return Promise.resolve({code:1,stdout:JSON.stringify({error:message})});
 };
 const page=runtime.render(await runtime.module.load());
 await find(page,'确认安装到路由器').click();
 assert.ok(page.textContent.includes('准备文件校验失败'));
 assert.ok(find(page,'自动下载并安装 WARP'));
 assert.equal(find(page,'确认安装到路由器'),undefined,'Failed validation must refresh the main action');
 assert.equal(runtime.writes.length,1,'Only the rejected plan request is written');
}

async function assertInstallProgress(noRuntime) {
 const runtime=setup(), statusPath='/usr/libexec/routepilot-runtime-status';
 const progress=page=>nodes(page).find(node=>node.tag==='progress'&&node.attrs['aria-label']==='WARP 安装与下载进度');
 const preparing={...noRuntime,online:{state:'preparing'}}, bytesDone=4*1024**2, bytesTotal=8*1024**2;
 runtime.setData({...preparing,job:{state:'running',phase:'download_base',bytes_done:bytesDone,bytes_total:bytesTotal}});
 const page=runtime.render(await runtime.module.load());
 assert.ok(page.textContent.includes('下载 Debian 基础环境'));
 assert.equal(Number(progress(page)?.attrs.value),bytesDone);
 assert.equal(Number(progress(page)?.attrs.max),bytesTotal);
 assert.equal(runtime.writes.length,0,'Reloading while downloading only reads status');

 runtime.setStatusError('模拟暂时无法读取状态');
 await runtime.poll();
 assert.equal(Number(progress(page)?.attrs.value),bytesDone,'Transient status failure retains progress');
 assert.ok(page.textContent.includes('暂时无法读取最新进度'));
 runtime.setStatusError(null);
 const staged={...preparing,online:{state:'preparing',components:{debian:'cached',warp:'missing',dependencies:'missing'}}};
 runtime.setData({...staged,job:{state:'running',phase:'download_warp',bytes_done:bytesDone,bytes_total:bytesTotal}});
 await runtime.poll();
 assert.ok(page.textContent.includes('下载 Cloudflare WARP 程序'));
 assert.ok(page.textContent.includes('4.0 / 8.0 MiB · 50%'));
 assert.ok(!page.textContent.includes('暂时无法读取最新进度'));
 assert.equal(progress(page).attrs.value,bytesDone);
 const debianRefresh=nodes(page).find(e=>e.attrs['aria-label']==='重新下载 Debian 基础环境');
 assert.ok(debianRefresh?.disabled,'Other download actions stay unavailable during a running job');

 runtime.setData({...staged,job:{state:'running',phase:'download_packages',bytes_done:bytesDone}});
 await runtime.poll();
 assert.ok(page.textContent.includes('下载 Debian 依赖'));
 assert.ok(page.textContent.includes('已下载 4.0 MiB'));
 assert.ok(progress(page),'An active download without a byte total still has a progress indicator');
 assert.equal(progress(page).attrs.value,undefined,'Unknown progress must not be shown as a percentage');
 const activeBar=progress(page);
 await runtime.poll();
 assert.equal(progress(page),activeBar,'An unchanged progress indicator should not be remounted');
 assert.ok(runtime.calls.every(([file])=>file===statusPath),'Polling must only read runtime status');

 const ready={...noRuntime,online:{state:'ready',version:'2026.8.2115.0'},
  job:{state:'success',phase:'ready',message:'文件已准备好，尚未安装'}};
 runtime.setData(ready);
 await runtime.poll();
 assert.ok(page.textContent.includes('已下载并校验，等待确认安装'));
 assert.ok(page.textContent.includes('尚未安装'));
 assert.ok(find(page,'确认安装到路由器'));
 assert.ok(!page.textContent.includes('WARP 已安装'));
 assert.equal(runtime.writes.length,0,'A completed download must never submit installation');
 runtime.setData({...ready,online:{state:'expired',message:'准备文件已过期'}});
 const expiredPage=runtime.render(await runtime.module.load());
 assert.ok(expiredPage.textContent.includes('安装文件需要重新准备'));
 assert.ok(!expiredPage.textContent.includes('等待确认安装'));
 assert.ok(find(expiredPage,'自动下载并准备安装'));

 const installing={...noRuntime,online:{state:'installing'},job:{state:'running',phase:'install_packages'}};
 runtime.setData(installing);
 const installPage=runtime.render(await runtime.module.load());
 assert.ok(installPage.textContent.includes('安装 WARP 和依赖'));
 assert.ok(progress(installPage));
 assert.equal(progress(installPage).attrs.value,undefined);
 assert.ok(!installPage.textContent.includes('WARP 已安装'));
 runtime.setData({...installing,job:{state:'running',phase:'prepare_install',elapsed_seconds:12}});
 await runtime.poll();
 assert.ok(installPage.textContent.includes('准备独立安装副本'));
 assert.ok(installPage.textContent.includes('本阶段已用时 12 秒'));
 assert.equal(progress(installPage).attrs.value,undefined,'Preparing the copy has no observed percentage');
 runtime.setData({...installing,job:{...installing.job,install_step:'unpack',install_percent:42,elapsed_seconds:65}});
 await runtime.poll();
 assert.ok(installPage.textContent.includes('解包 WARP 和依赖'));
 assert.ok(installPage.textContent.includes('安装进度 42% · 本阶段已用时 1 分 5 秒'));
 assert.equal(progress(installPage).attrs.max,100);
 assert.equal(progress(installPage).attrs.value,42);
 runtime.setData({...installing,job:{...installing.job,install_step:'configure',install_percent:100,elapsed_seconds:70}});
 await runtime.poll();
 assert.ok(installPage.textContent.includes('配置 WARP 和依赖'));
 assert.equal(progress(installPage).attrs.value,100);
 assert.ok(!installPage.textContent.includes('WARP 已安装'),'The package percentage does not imply successful verification');
 runtime.setData({...installing,job:{...installing.job,install_step:'unknown',install_percent:101,elapsed_seconds:-1}});
 await runtime.poll();
 assert.ok(installPage.textContent.includes('安装 WARP 和依赖'));
 assert.ok(!installPage.textContent.includes('本阶段已用时'));
 assert.equal(progress(installPage).attrs.value,undefined,'Out-of-range metrics leave the indicator indeterminate');
 runtime.setData({...fixture(),job:{state:'success',phase:'installed'}});
 await runtime.poll();
 assert.ok(installPage.textContent.includes('WARP 已安装'));
 assert.ok(installPage.textContent.includes('下一步：登录 Cloudflare 账户'));
 runtime.setData({...noRuntime,job:{state:'success',phase:'installed'}});
 const absentRuntime=runtime.render(await runtime.module.load());
 assert.ok(!absentRuntime.textContent.includes('WARP 已安装'),'Past success is not proof of an installed runtime');
 assert.ok(!absentRuntime.textContent.includes('下一步：登录 Cloudflare 账户'));
}

async function assertDownloadComponents(noRuntime) {
 const runtime=setup();
 const components={debian:'cached',warp:'cached',dependencies:'missing'};
 const cached={...noRuntime,online:{state:'failed',components}};
 runtime.setData(cached);
 const page=runtime.render(await runtime.module.load());
 const stages=nodes(page).find(e=>e.attrs['aria-label']==='安装文件下载状态');
 assert.equal(stages.children.length,3);
 assert.deepEqual(stages.children.map(e=>e.textContent),[
  'Debian 基础环境已缓存，使用前校验重新下载',
  'Cloudflare WARP 程序已缓存，使用前校验重新下载',
  'Debian 依赖待下载'
 ]);
 assert.equal(runtime.writes.length,0,'Reading staged download status does not prepare or install');
 const refreshWarp=nodes(stages).find(e=>e.attrs['aria-label']==='重新下载 Cloudflare WARP 程序');
 await refreshWarp.click();
 assert.deepEqual(JSON.parse(runtime.writes.at(-1)[1]).payload,{refresh:'warp'});
 assert.equal(runtime.calls.filter(v=>v[1][0]==='prepare-online').length,1);
 assert.equal(runtime.calls.filter(v=>v[1][0]==='execute').length,0);
 assert.ok(find(page,'确认安装到路由器'),'Refreshing a component still requires installation confirmation');
 const readyStages=nodes(page).find(e=>e.attrs['aria-label']==='安装文件下载状态');
 assert.ok(readyStages.children.every(e=>e.textContent.includes('已校验')));
}

async function assertLatestInstallError(noRuntime) {
 const runtime=setup();
 const oldError='旧的 Debian 下载失败';
 const precondition='请先在总览关闭 imOK，并结束登录准备';
 const olderAttempt={...noRuntime,online:{state:'failed',message:oldError}};
 const offlinePanel=page=>nodes(page).find(e=>e.tag==='details'&&e.textContent.startsWith('高级：离线'));
 runtime.setData({...olderAttempt,job:{state:'failed',phase:'queued',message:precondition}});
 const blocked=runtime.render(await runtime.module.load());
 assert.ok(blocked.textContent.includes('维护准备未通过'));
 assert.ok(blocked.textContent.includes(precondition));
 assert.ok(!blocked.textContent.includes('等待维护任务开始'));
 assert.ok(!blocked.textContent.includes(oldError));
 assert.ok(!blocked.textContent.includes('环境检查通过，可以自动下载'));
 assert.ok(offlinePanel(blocked).attrs.open==null);
 assert.ok(!nodes(blocked).some(e=>e.tag==='progress'));

 const downloadError='本次 Debian 下载失败';
 runtime.setData({...olderAttempt,online:{state:'failed',message:downloadError},
  job:{state:'failed',phase:'download_base',message:downloadError}});
 const failed=runtime.render(await runtime.module.load());
 assert.equal(failed.textContent.split(downloadError).length,2,'Show the current error once');
 assert.ok(offlinePanel(failed).attrs.open==null,'Online failures keep offline import collapsed');

 runtime.setData(olderAttempt);
 const legacy=runtime.render(await runtime.module.load());
 assert.ok(legacy.textContent.includes(oldError),'Online-only failure remains available without a job');
 assert.ok(offlinePanel(legacy).attrs.open==null,'Online failures keep offline import collapsed');

 const cached={debian:'cached',warp:'cached',dependencies:'cached'};
 const selfTest={state:'failed',phase:'verify_install',message:'模拟 WARP 自检超时'};
 runtime.setData({...noRuntime,job:selfTest,online:{state:'failed',components:cached}});
 const previous=runtime.render(await runtime.module.load());
 assert.ok(find(previous,'用缓存重新准备'));
 assert.ok(previous.textContent.includes(selfTest.message));
 runtime.setData({...noRuntime,job:selfTest,online:{state:'ready',version:'2026.8.2115.0'}});
 const retry=runtime.render(await runtime.module.load());
 await find(retry,'重试安装到路由器').click();
 assert.equal(JSON.parse(runtime.writes.at(-1)[1]).payload.operation,'online-install');
 assert.equal(runtime.calls.filter(([,args])=>args[0]==='execute').length,0,'Retry still requires confirmation');
}

(async()=>{
 const t=setup();await t.module.load();assert.deepEqual(t.calls,[['/usr/libexec/routepilot-runtime-status',[]]]);
 for(const d of [null,{},[],{...fixture(),settings:{hours:1}},{...fixture(),local_version:5},{...fixture(),job:[]},{...fixture(),backups:[null]},{...fixture(),environment:{can_prepare:true,issues:'invalid'}},{...fixture(),online:{state:'ready',version:null}},{...fixture(),online:{state:'unknown'}}]){t.setData(d);const x=await t.module.load();assert.ok(x.load_error);assert.ok(!/Cannot read|TypeError|undefined/.test(x.load_error));}
 const c=setup(),page=c.render();assert.equal(c.writes.length,0);await c.poll();assert.equal(c.calls.length,0,'Idle page must not poll or check remote metadata');
 assert.equal(find(page,'检查更新').disabled,false);assert.equal(find(page,'刷新本机状态').disabled,false);assert.equal(find(page,'保存检查频率').disabled,false);assert.equal(find(page,'备份并升级').disabled,false);assert.ok(page.textContent.includes('先在路由器安装 WARP'));assert.ok(page.textContent.includes('发现可用新版本'));
 await find(page,'备份并升级').click();assert.equal(c.calls.filter(v=>v[1][0]==='execute').length,0,'Opening confirmation cannot install');
 assert.ok(c.dlg.classList.contains('imok-dialog')&&c.dlg.classList.contains('imok-runtime-dialog'));
 assert.equal(find(c.dlg,'取消').disabled,false);let confirm=find(c.dlg,'备份并升级');assert.ok(confirm.disabled);await confirm.click();assert.equal(c.calls.filter(v=>v[1][0]==='execute').length,0,'Unchecked risk acknowledgment cannot execute');
 c.ui.cancelModal({key:'Escape'});await Promise.resolve();assert.ok(!c.doc.body.classList.contains('modal-overlay-active'),'Native Escape must activate Cancel');
 await find(page,'备份并升级').click();confirm=find(c.dlg,'备份并升级');const ack=nodes(c.dlg).find(e=>e.tag==='input');ack.checked=true;ack.handlers.change();assert.equal(confirm.disabled,false);
 await Promise.all([confirm.click(),confirm.click()]);assert.equal(c.calls.filter(v=>v[1][0]==='execute').length,1,'No duplicate upgrade submission');
 assert.equal(JSON.parse(c.writes.at(-1)[1]).payload.confirmation,'CONFIRM');assert.ok(page.textContent.includes('模拟请求执行中'));
 const check=setup(),checkPage=check.render();await find(checkPage,'检查更新').click();assert.equal(check.calls.filter(v=>v[1][0]==='check').length,1);assert.ok(!checkPage.textContent.includes('正在提交版本检查'),'Accepted check must clear its transient submitting message');
 const unverified=setup();unverified.setData({...fixture(),cli_version_verified:false});
 assert.ok(unverified.render().textContent.includes('此版本的 warp-cli 输出尚未验证'));
 const verified=setup();verified.setData({...fixture(),cli_version_verified:true});
 assert.ok(!verified.render().textContent.includes('此版本的 warp-cli 输出尚未验证'));
 const missing=setup();missing.setData({...fixture(),installed:false,runtime_exists:false,local_version:null,latest_version:null,update_available:false});const m=missing.render();assert.ok(m.textContent.includes('尚未安装'));assert.ok(!/null|undefined/.test(m.textContent));assert.ok(find(m,'备份并升级').disabled);
 const noRuntime={...fixture(),installed:false,runtime_exists:false,local_version:null,latest_version:null,update_available:false};
 await assertFailedPlanOffersAutomaticRetry(noRuntime);
 await assertInstallProgress(noRuntime);
 await assertDownloadComponents(noRuntime);
 await assertLatestInstallError(noRuntime);
 const fresh=setup();fresh.setData(noRuntime);const freshPage=fresh.render();
 await find(freshPage,'自动下载并安装 WARP').click();
 assert.equal(fresh.writes.length,0,'Opening the install confirmation does not submit a write');
 fresh.ui.cancelModal({key:'Escape'});
 assert.equal(fresh.writes.length,0,'Cancel leaves the environment untouched');
 await find(freshPage,'自动下载并安装 WARP').click();
 const startInstall=find(fresh.dlg,'开始下载并安装');
 await Promise.all([startInstall.click(),startInstall.click()]);
 assert.deepEqual(JSON.parse(fresh.writes.at(-1)[1]).payload,{confirmation:'CONFIRM'});
 assert.equal(fresh.calls.filter(([,args])=>args[0]==='first-install').length,1);
 assert.equal(fresh.calls.filter(([,args])=>['prepare-online','plan','execute'].includes(args[0])).length,0);
 assert.ok(freshPage.textContent.includes('模拟 WARP 后台安装中'));

 const auto=setup();auto.setData(noRuntime);const a=auto.render();assert.equal(auto.writes.length,0,'Opening setup must not start downloading');assert.equal(find(a,'自动下载并准备安装').disabled,false);assert.ok(nodes(a).find(e=>e.tag==='details'&&e.textContent.startsWith('高级：离线')).attrs.open==null,'Offline import is advanced and initially collapsed');
 await find(a,'自动下载并准备安装').click();assert.deepEqual(JSON.parse(auto.writes.at(-1)[1]).payload,{},'Default preparation reuses verified files');assert.equal(auto.calls.filter(v=>v[1][0]==='prepare-online').length,1);assert.equal(auto.calls.filter(v=>v[1][0]==='execute').length,0,'Preparation cannot install');assert.ok(a.textContent.includes('文件已自动下载并校验，尚未安装'));
 await find(a,'确认安装到路由器').click();assert.equal(JSON.parse(auto.writes.at(-1)[1]).payload.operation,'online-install');assert.equal(auto.calls.filter(v=>v[1][0]==='execute').length,0);const install=find(auto.dlg,'确认安装');assert.ok(install.disabled);const installAck=nodes(auto.dlg).find(e=>e.tag==='input');installAck.checked=true;installAck.handlers.change();await install.click();assert.equal(auto.calls.filter(v=>v[1][0]==='execute').length,1);
 const blocked=setup();blocked.setData({...noRuntime,environment:{can_prepare:false,issues:['模拟存储空间不足']}});const blockedPage=blocked.render();assert.ok(find(blockedPage,'自动下载并准备安装').disabled);assert.ok(blockedPage.textContent.includes('模拟存储空间不足'));assert.equal(blocked.calls.length,0);
 const u=setup();u.setData({...fixture(),installed:false,runtime_exists:false,local_version:null,update_available:false});const up=u.render();const cancelled=find(up,'上传运行环境包').click();for(let i=0;i<4;i++)await Promise.resolve();u.ui.cancelModal({key:'Escape'});await cancelled;assert.equal(u.uploads.length,0,'Cancel must not upload');
 const uploading=find(up,'上传运行环境包').click();for(let i=0;i<4;i++)await Promise.resolve();const file=nodes(u.dlg).find(e=>e.tag==='input'&&e.attrs.type==='file');file.files=[{name:'SYNTHETIC.tgz',size:1024}];await find(u.dlg,'上传文件').click();await uploading;assert.equal(u.uploads.length,1);assert.equal(u.uploads[0].url,'/cgi-bin/luci/cgi-upload');assert.equal(u.uploads[0].fields.filename,'/opt/routepilot/runtime-uploads/runtime.tgz');assert.equal(u.uploads[0].fields.sessionid,'SYNTHETIC session');assert.ok(up.textContent.includes('尚未安装'));assert.equal(u.calls.filter(v=>v[1][0]==='execute').length,0,'Upload cannot execute install');
 const offline=setup();
 const legacyCheckError='安装或校验命令失败，现用环境未提交；请检查官方源连通性、依赖、隔离权限与存储空间';
 offline.setData({...fixture(),check:{state:'failed',message:legacyCheckError},job:{state:'success',phase:'installed',message:'安装已完成'}});
 const o=offline.render();
 assert.ok(o.textContent.includes('WARP 已安装'));
 assert.ok(o.textContent.includes('2026.8.2115.0'));
 assert.ok(o.textContent.includes('更新检查失败'));
 assert.ok(o.textContent.includes('现有 WARP 安装不受影响，可稍后点「检查更新」重试。'));
 assert.ok(!o.textContent.includes(legacyCheckError));
 assert.ok(!o.textContent.includes('2026.9.1.0'),'A failed check cannot present a stale version as current');
 assert.equal(find(o,'检查更新').disabled,false);
 assert.ok(find(o,'备份并升级').disabled);
 assert.equal(offline.calls.length,0,'Displaying the failure must not submit a retry');
 const detailedCheckError='连接 Cloudflare 版本目录超时。';
 offline.setData({...fixture(),check:{state:'failed',message:detailedCheckError},job:{state:'failed',phase:'check_version',message:detailedCheckError}});
 const detailed=offline.render();
 assert.equal(detailed.textContent.split(detailedCheckError).length,2,'A manual check failure is reported once when the version card is visible');
 assert.ok(!detailed.textContent.includes('未完成 · 检查官方版本'));
 offline.setData({...noRuntime,check:{state:'failed',message:legacyCheckError},job:{state:'failed',phase:'check_version',message:legacyCheckError}});
 const notInstalled=offline.render();
 assert.ok(nodes(notInstalled).some(e=>e.attrs.class==='rp-operation-feedback'&&e.textContent.includes('更新检查失败')),'A failed manual check is visible even if the version card is collapsed');
 assert.ok(!notInstalled.textContent.includes(legacyCheckError));
 const readACL=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/rpcd/acl.d/luci-app-routepilot.json')))['luci-app-routepilot'];assert.ok(readACL.read.file['/usr/libexec/routepilot-runtime-status']);assert.ok(!readACL.read.file['/usr/libexec/routepilot-runtime']);assert.ok(readACL.write.file['/usr/libexec/routepilot-runtime']);assert.ok(!readACL.write.file['/usr/libexec/routepilot-runtime-first-install']);
 const start=view.search(/\bload:\s*function\s*\(/),end=view.search(/\brender:\s*function\s*\(/);assert.ok(start>=0&&end>start);const load=view.slice(start,end);
 const runtimeRoute=load.search(/if\s*\(leaf\s*===\s*'runtime'\)/),mainLoad=load.indexOf("uci.load('routepilot')");assert.ok(runtimeRoute>=0&&runtimeRoute<mainLoad);
 assert.ok(!source.includes("fs.exec('/usr/libexec/routepilot',"));
 console.log('Runtime UI: automatic preparation, separate confirmed installation, advanced offline fallback, read-only loading, malformed data, first-install wording, update states, native modal/Escape, explicit acknowledgment, one submission and idle polling passed');
})().catch(e=>{console.error(e);process.exit(1);});

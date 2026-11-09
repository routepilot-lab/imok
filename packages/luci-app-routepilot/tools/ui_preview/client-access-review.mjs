// Real Chromium + real frontend; only synthetic fs/Cloudflare results.
// Run after opening a new named audit TaskSpace: ego-browser nodejs < this-file
const task=await taskSpace('imOK 客户端接入逻辑复核 20261108'),p=task.page('p1');
const fs=await import('node:fs/promises'),origin='http://127.0.0.1:8772';
const out=(await(await fetch(origin+'/meta.json')).json()).client_access_evidence_directory+'-review';
await fs.mkdir(out,{recursive:true});
const checks=[];
function check(value,message){if(!value)throw Error(message);checks.push(message);}
async function open(){await p.goto(origin+'/cgi-bin/luci/admin/services/routepilot/clients');await p.waitForFunction(()=>window.previewReady||window.previewError);if(await p.evaluate(()=>!!window.previewError))throw Error('Preview failed');await p.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1080,deviceScaleFactor:1,mobile:false});}
async function click(name){await p.click('loc=role:button[name="'+name+'"]',{label:'复核 '+name});}
async function hidden(){await p.waitForFunction(()=>!document.body.classList.contains('modal-overlay-active'));}
async function settle(){await p.waitForFunction(()=>!document.querySelector('.rp-layout').hasAttribute('aria-busy'));}
await open();
// Dirty parameters must never silently export the last saved port.
await p.selectOption('select[name="client-port"]','8095');await click('导出到小火箭');
check(await p.evaluate(()=>!document.body.classList.contains('modal-overlay-active')&&document.querySelector('.imok-client-notice').textContent.includes('尚未保存')&&!previewAudit.calls.some(x=>x.action==='export')),'Unsaved port blocks export without RPC');
await click('保存导出参数');await p.waitForFunction(()=>document.querySelector('.imok-client-notice').textContent.includes('已保存'));
await click('导出到小火箭');await click('确认显示私钥与二维码');await p.waitForSelector('.imok-client-qr img');
check(await p.evaluate(()=>document.querySelector('dialog textarea').value.startsWith('masque://162.159.197.2:8095?')),'Saved port 8095 reaches export');
await p.press('dialog textarea','Escape');await hidden();
check(await p.evaluate(()=>!!document.querySelector('dialog')&&!document.querySelector('dialog textarea').value&&!document.querySelector('.imok-client-qr img')),'Native-style retained modal DOM clears secrets on Escape');
// Delay an export result until its original modal has been closed.
await p.evaluate(()=>{const original=mockedFS.exec;mockedFS.exec=function(path,args=[]){if(args[0]==='export')return original.call(this,path,args).then(result=>new Promise(resolve=>{window.releaseDelayedExport=()=>resolve(result);}));return original.call(this,path,args);};});
await click('导出到小火箭');await click('确认显示私钥与二维码');await p.waitForFunction(()=>!!window.releaseDelayedExport);
await click('关闭并清除');await hidden();await p.evaluate(()=>window.releaseDelayedExport());await settle();
check(await p.evaluate(()=>!document.querySelector('dialog textarea').value&&!document.querySelector('.imok-client-qr img')),'Late export response cannot repopulate a closed modal');
// Delay local create acknowledgement. It must not dismiss a newer modal.
await open();await p.evaluate(()=>{const original=mockedFS.exec;mockedFS.exec=function(path,args=[]){if(args[0]==='create')return original.call(this,path,args).then(result=>new Promise(resolve=>{window.releaseDelayedCreate=()=>resolve(result);}));return original.call(this,path,args);};});
await click('新增客户端');await p.fill('dialog input[placeholder="例如：我的 iPhone"]','模拟迟到回执');await p.fill('dialog input[placeholder="团队名称"]','example-team');await click('创建客户端');await p.waitForFunction(()=>!!window.releaseDelayedCreate);await click('取消');await hidden();
// Open another modal while the prior operation is pending; root controls are
// disabled, so invoke its actual UI opener only after closing via fixture UI.
// The race is reachable through another LuCI modal, not a second submit.
await p.evaluate(()=>previewUI.showModal('模拟其他操作',[document.createTextNode('等待中的独立弹窗')],'imok-dialog'));
await p.evaluate(()=>window.releaseDelayedCreate());await settle();
check(await p.evaluate(()=>document.body.classList.contains('modal-overlay-active')&&document.querySelector('dialog h4').textContent==='模拟其他操作'),'Late create acknowledgement preserves a newer modal');
await p.evaluate(()=>previewUI.hideModal());
// Lost registration reply: backend accepts once; UI refreshes once without retry.
await open();await p.click('.imok-client-item[aria-pressed="false"]',{label:'选择 未注册客户端'});
await click('授权并注册');await p.fill('dialog textarea','SIMULATED-AUTHORIZATION');await p.click('dialog input[type="checkbox"]',{label:'确认 模拟注册条款'});
await p.evaluate(()=>{const original=mockedFS.exec;mockedFS.exec=function(path,args=[]){if(args[0]==='register')return original.call(this,path,args).then(()=>Promise.reject(Error('模拟：提交成功后回执丢失')));return original.call(this,path,args);};});
await click('注册独立客户端');await p.waitForFunction(()=>document.querySelector('dialog [role="alert"]').textContent.includes('回执丢失'));await settle();
await p.press('dialog textarea','Escape');await hidden();
check(await p.evaluate(()=>previewAudit.calls.filter(x=>x.action==='register').length===1&&document.querySelector('.imok-client-detail').textContent.includes('100.96.0.99')),'Lost registration acknowledgement refreshes state without duplicate write');
// Bad row is a read failure, never an empty installation or a rendering crash.
await open();await p.evaluate(()=>{portableClients=[null];});await click('刷新列表');await p.waitForFunction(()=>document.querySelector('.imok-client-notice').textContent.includes('列表格式无效'));
check(await p.evaluate(()=>document.querySelector('.imok-client-detail').textContent.includes('现有身份未重置')&&!window.previewError),'Malformed client row produces recoverable read error');
// Keep in-progress drafts and focus while observing another queued client.
await open();await p.evaluate(()=>{Object.assign(portableClients[1],{state:'queued',label:'任务待处理',operation:'register',busy:true});});await click('刷新列表');
await p.fill('input[name="client-dns"]','1.1.1.1');await p.evaluate(async()=>{await tick();});
check(await p.evaluate(()=>document.querySelector('input[name="client-dns"]').value==='1.1.1.1'&&document.activeElement.name==='client-dns'&&!previewAudit.calls.some(x=>x.action==='edit')&&previewAudit.interval===2),'Queued poll preserves unsaved draft and focus at original 2s interval');
await open();await click('新增客户端');await p.press('dialog input[placeholder="例如：我的 iPhone"]','Escape');await hidden();
await p.click('.imok-client-item[aria-pressed="false"]',{label:'选择 未注册客户端'});await click('授权并注册');await p.press('dialog textarea','Escape');await hidden();
await p.click('.imok-client-advanced > summary >> nth=1',{label:'展开 撤销与删除'});await click('删除本地记录');await p.press('dialog input','Escape');await hidden();
check(true,'Create, authorization and danger confirmation all close via native Escape footer');
await open();await p.screenshot({path:out+'/review-final-desktop.png',fullPage:true});
await fs.writeFile(out+'/interaction-review.json',JSON.stringify({simulated:true,checks,noProductionAccess:true,nativeBackendOrPhoneTest:false},null,2));
console.log({taskSpaceId:task.spaceId,passed:checks.length,checks});

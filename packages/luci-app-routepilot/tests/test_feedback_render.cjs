// Render the real diagnostics view with bounded DOM/UCI/RPC doubles; no router calls.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const menu = JSON.parse(fs.readFileSync(path.join(root, 'root/usr/share/luci/menu.d/luci-app-routepilot.json')));
const viewName = Object.values(menu)[0].action.path;
const source = fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/view', viewName + '.js'), 'utf8');
const helperName = source.match(/require routepilot\.(feedback_[0-9a-f]+) as feedback/)[1];
class Base {}
Base.extend = function(props) { class Module extends this {} Object.assign(Module.prototype, props); return Module; };
const Feedback = new Function('baseclass', fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/routepilot', helperName+'.js'), 'utf8'))(Base);
assert.ok(Feedback.prototype instanceof Base);
const feedback = new Feedback();
class Element {
 constructor(tag, attrs, children) { this.tag=tag; this.attrs=attrs||{}; this.value=this.attrs.value||''; this.disabled=this.attrs.disabled!=null; this.hidden=this.attrs.hidden!=null; this.open=!!this.attrs.open; this.style={}; this.children=[]; this.replaceChildren(...(Array.isArray(children)?children:[children])); }
 set textContent(value) { this.children=[String(value)]; }
 get textContent() { return this.children.map(x=>x instanceof Element ? x.textContent : String(x)).join(' '); }
 appendChild(child) { this.children.push(child); return child; }
 replaceChildren(...children) { this.children=children.filter(x=>x!=null); }
 setAttribute(k,v) { this.attrs[k]=v;if(k==='disabled')this.disabled=true; }
 querySelector(selector) {const cls=selector[0]==='.'?selector.slice(1):null;const id=(selector.match(/id="([^"]+)"/)||[])[1];return this.find(e=>cls?String(e.attrs.class||'').split(' ').includes(cls):id?e.attrs.id===id:e.tag===selector);}
 find(fn){for(const c of this.children){if(c instanceof Element){if(fn(c))return c;const x=c.find(fn);if(x)return x;}}return null;}
 addEventListener(name,fn){this.listeners=this.listeners||{};this.listeners[name]=fn;}
 contains(el){return this===el||!!this.find(e=>e===el);}
 remove(){}
 focus(){activeElement=this;}

}
// Target LuCI dom.append stringifies null/undefined inside arrays; direct null adds nothing.
function E(tag,attrs,children) { return new Element(tag,attrs,Array.isArray(children)?children.map(c=>c instanceof Element?c:String(c)):children); }
let lastMap,lastMapNode;const selections={};
class Map {
 constructor(){this.children=[];lastMap=this;}
 render(){return this.renderContents();}
 renderContents(){return Promise.all(this.children.map(s=>s.render())).then(nodes=>{this.root=this.root||E('div',{id:'cbi-routepilot'});this.root.replaceChildren(...nodes);lastMapNode=this.root;return this.root;});}
 section(){const s={children:[],render(){return Promise.all(s.children.map(o=>Promise.resolve(o.renderWidget('main',0,o.cfgvalue?o.cfgvalue():config[o.option])).then(widget=>E('div',{id:'cbi-routepilot-main-'+o.option},E('div',{class:'cbi-value-field'},widget))))).then(nodes=>E('div',{},nodes));},option(type,name,title){const o={option:name,title,value(k,v){this.keylist=this.keylist||[];this.vallist=this.vallist||[];this.keylist.push(k);this.vallist.push(v);},depends(){},formvalue(){return selections[this.option];},renderWidget(id,index,value){if(this.onclick)return E('button',{click:this.onclick},this.inputtitle);selections[this.option]=value;return E('select',{},value||'');}};s.children.push(o);return o;}};this.children.push(s);return s;}
}
let current = {state:'running',time:Date.now()/1000,exit:'usa',exit_ip:'192.0.2.1',generation:'rules1',runtime_id:'before',applied_nonce:'n1'};
let test = {...current,state:'done',stale:false,finished_at:Date.now()/1000,results:[{id:'google',state:'ok',message:'可连接',ms:1}]};
let tick;
let endpointRecoveryInput=null,endpointScheduleInput=null,endpointInput=null,endpointResponse={settings:{mode:"preserve",address:"",hours:24,revision:"",staged:false},application:{},candidates:{entries:[]},collection:{},capability:{supported:true},pending:false};
let receiver={schema:1,state:'observing',time:Date.now()/1000,runtime_id:'before',generation:'rules1',coverage:'complete',observed_drops:1289,queue_now:1048576,peak_queue:2097152,socket_count:1,known_ingress_count:1,other_public_udp_count:0,started_at:Date.now()/1000};
let legacyReplacement='';const location={pathname:'/admin/services/routepilot/cloudflare/dns',replace(url){legacyReplacement=url;}};

let activeElement=null,warpReply=null,observationReply=null,backupNodeReply=null,relayReply=null,dnsData=null,dnsTests=null,cloudReply=null,cloudCommandReply=null,cloudDirectoryReply=null,scanReply=null,cloudInputs=[],organizationInputs=[],notices=[];let directoryReply,directoryCalls=0,lastModal,syncDirectoryFailure=false,failStatusRead=false,registerWrites=0,commandCalls=[],readCalls=[];
const mockedFS={write(p,text){const slot=p.match(/\/requests\/([a-z-]+)-[a-f0-9]{32}\.json$/);if(slot)p='/'+slot[1];if(p.endsWith('/auth-input'))registerWrites++;if(p.endsWith('/endpoint-recover-input'))endpointRecoveryInput=JSON.parse(text).payload;if(p.endsWith('/endpoint-schedule-input'))endpointScheduleInput=JSON.parse(text).payload;if(p.endsWith('/endpoint-input'))endpointInput=JSON.parse(text).payload;if(p.endsWith('/organization-input'))organizationInputs.push(JSON.parse(text).payload);if(p.endsWith('/cloud-input'))cloudInputs.push(JSON.parse(text).payload);return Promise.resolve();},exec(p,args){if(p==='/usr/libexec/routepilot-backup-list')return Promise.resolve({code:0,stdout:'{}'});if(p==='/usr/libexec/routepilot'&&args[0]==='interfaces')return Promise.resolve({code:0,stdout:'[]'});if(p==='/usr/libexec/routepilot'){commandCalls.push(args[0]);assert.ok(['login','register','prepare','warp-info','dns-test','dns-clear','test','cloud-save','cloud-verify','cloud-target','cloud-check','warp-org-save','endpoint-info','endpoint-save','endpoint-refresh','endpoint-schedule','endpoint-recover','settings-export','settings-import'].includes(args[0]));if(args[0]==='endpoint-recover')return Promise.resolve({code:0,stdout:JSON.stringify({saved:true,request_id:args[1].split(':')[0]})});if(args[0]==='endpoint-schedule'){endpointResponse={...endpointResponse,settings:{...endpointResponse.settings,hours:endpointScheduleInput.hours}};return Promise.resolve({code:0,stdout:JSON.stringify({saved:true,settings:endpointResponse.settings,request_id:args[1].split(':')[0]})});}if(args[0]==='endpoint-save'){endpointResponse={...endpointResponse,settings:{...endpointInput,revision:'endpoint-new',staged:false},pending:true,application:{}};return Promise.resolve({code:0,stdout:JSON.stringify({saved:true,settings:endpointResponse.settings,request_id:args[1].split(':')[0]})});}if(args[0]==='endpoint-info'||args[0]==='endpoint-refresh')return Promise.resolve({code:0,stdout:JSON.stringify(endpointResponse)});return Promise.resolve({code:0,stdout:args[0]==='warp-org-save'?JSON.stringify({organization:organizationInputs.at(-1).organization,nonce:'org-only',request_id:args[1].split(':')[0]}):args[0].startsWith('cloud-')?JSON.stringify(args[0]==='cloud-save'?{...(cloudCommandReply||{}),saved:true,request_id:args[1].split(':')[0]}:(cloudCommandReply||{})):args[0]==='register'?JSON.stringify({registered:true,message:'registered',request_id:args[1].split(':')[0]}):args[0]==='login'?'https://example.invalid/warp':args[0]==='dns-test'?JSON.stringify({state:'submitted',request_id:'job-new',message:'submitted'}):args[0]==='dns-clear'?JSON.stringify({ok:true,...dnsData,removed:2}):'registered'});}if(p==='/usr/libexec/routepilot-device-usage')return Promise.resolve({code:0,stdout:'{"state":"unsupported","devices":[]}'});assert.equal(p,'/usr/libexec/routepilot-exits');directoryCalls++;if(syncDirectoryFailure)throw Error('synchronous transport failure');return new Promise(resolve=>{directoryReply=resolve;});},read(p){readCalls.push(p);if(p.endsWith('status.json')&&failStatusRead)return Promise.reject(Error('status read failure'));return Promise.resolve(JSON.stringify(p.endsWith('warp-state.json')?warpReply:p.endsWith('exit-observation.json')?observationReply:p.endsWith('backup-node.json')?backupNodeReply:p.endsWith('relay-tests.json')?relayReply:p.endsWith('cloud-directory-state.json')?cloudDirectoryReply:p.endsWith('backup-scan.json')?scanReply:p.endsWith('cloud-state.json')?cloudReply:p.endsWith('dns-state.json')?dnsData:p.endsWith('dns-tests.json')?dnsTests:p.endsWith('receive.json')?receiver:p.endsWith('status.json')?current:p.endsWith('connectivity.json')?test:{state:'success'}));}};
const config={enabled:'1',scope:'lan',exit:'usa',vnet_usa:'11111111-1111-4111-8111-111111111111',lan_interfaces:['br-lan'],apply_nonce:'n1'};
const uci={load(){return Promise.resolve();},unload(){return Promise.resolve();},sections(){return [];},set(c,s,k,v){config[k]=v;},get(c,s,k){return config[k];}};
const ui={menu:{load(){return Promise.resolve({});},flushCache(){}},addNotification(a,b){notices.push(b.textContent);},hideModal(){},showModal(title,children){lastModal=E('modal',{},children);}};
const pageModules = require('./fixtures/page_modules.cjs')(root, source, Base, E, x=>x);
let commitError;
const rpc = {
 declare(options) {
  if(options.object!=='uci') return ()=>Promise.resolve({});
  assert.equal(options.method,'commit');assert.equal(options.reject,true);
  assert.deepEqual(options.params,['config']);
  return (name)=>{
   assert.equal(name,'routepilot');
   return commitError ? Promise.reject(Error(commitError)) : Promise.resolve(0);
  };
 }
};
const View = new Function('window','document','view','form','uci','fs','ui','poll','network','rpc','dom','L','E','_','feedback',...Object.keys(pageModules),source)(
 {location,crypto:require('node:crypto').webcrypto}, {get activeElement(){return activeElement;},getElementById(id){return lastMapNode&&lastMapNode.find(e=>e.attrs.id===id);},createElementNS(ns,tag){return E(tag);}}, Base, {Map},uci,mockedFS,ui, {add(fn){tick=fn;}}, {}, rpc, {}, {url(p){return '/'+p;},resource(){return '';}},E,s=>s,feedback,...Object.values(pageModules));
assert.ok(View.prototype instanceof Base);
const exitView = new View(), netA='11111111-1111-4111-8111-111111111111', netB='abcdefab-abcd-4abc-8abc-abcdefabcdef';
const directory={available:true,read_at:Date.now(),last_success_at:Date.now(),networks:[{id:netA,name:'任意名称'},{id:netB,name:'任意名称',is_default:true}]};
const options=exitView.exitOptions('usa',{usa:netA},directory);
assert.deepEqual(options.map(x=>x.value),['usa',netB]);
assert.ok(options[0].label.includes(netA.slice(0,8)));
assert.ok(options[1].label.includes('账户默认网络'));
assert.equal(exitView.exitOptions('usa',{usa:netA},{available:false,networks:[]})[0].value,'usa');
assert.equal(exitView.exitOptions('default',{},directory)[0].value,'default');
assert.equal(exitView.exitOptions('',{},directory)[0].value,'');
assert.equal(exitView.exitOptions(netB.toUpperCase(),{},directory)[1].value,netB.toUpperCase());

(async()=>{
 location.pathname='/admin/services/routepilot/overview';
 const page=await new View().render([null,null,[]]);await tick();assert.ok(!page.textContent.includes('历史结果已过期'));assert.ok(page.textContent.includes('本次观测新增丢弃：1289'));
 assert.ok(page.textContent.includes('网站连通性检测'));
 current={...current,runtime_id:'after',time:Date.now()/1000};await tick();assert.ok(page.textContent.includes('历史结果已过期'));assert.ok(page.textContent.includes('接收观测已过期'));
 test={...test,runtime_id:'after'};await tick();assert.ok(!page.textContent.includes('历史结果已过期'));
 location.pathname='/admin/services/routepilot/cloudflare/account';const advanced=await new View().render([null,null,[],directory]);
 const removed=['manual_networks','vnet_default','vnet_seoul','vnet_usa','expected_seoul','expected_usa'];
 assert.ok(!lastMap.children[0].children.some(o=>removed.includes(o.option)));
 assert.ok(!lastMap.children[0].children.some(o=>o.option==='_discover'),'account excludes routing fields');
 assert.ok(!advanced.find(e=>e.attrs['data-step']==='dir'));assert.ok(!advanced.find(e=>e.attrs['data-step']==='select'));
 assert.equal(advanced.find(e=>e.attrs['data-setup-stage']).attrs['data-setup-stage'],'connecting');
 assert.equal(advanced.find(e=>e.attrs.class==='imok-panel rp-account-setup').open,false);
 assert.ok(advanced.textContent.includes('正在检查账户'));assert.ok(!advanced.textContent.includes(netA));
 const account=lastMap.children[0].children.find(o=>o.option==='_account_check');
 const refresh=account.onclick();await new Promise(setImmediate);directoryReply({code:0,stdout:JSON.stringify(directory)});await refresh;
 assert.equal(advanced.find(e=>e.attrs['data-step']==='auth').attrs['data-state'],'success');
 assert.ok(advanced.textContent.includes('已读取 2 个账户网络，连通性待验证。'));
 const emptyRefresh=account.onclick();await new Promise(setImmediate);
 directoryReply({code:0,stdout:JSON.stringify({...directory,networks:[]})});await emptyRefresh;
 assert.ok(advanced.textContent.includes('没有返回可选网络'));
 const restoreDirectory=account.onclick();await new Promise(setImmediate);
 directoryReply({code:0,stdout:JSON.stringify(directory)});await restoreDirectory;
 if(process.argv[2]) {
  const formSource=fs.readFileSync(process.argv[2],'utf8');
  function method(start,end){const a=formSource.indexOf(start),b=formSource.indexOf(end,a);assert.ok(a>=0&&b>a);const text=formSource.slice(a,b+1);return new Function('isEqual','return function'+text.slice(text.indexOf('(')))( (a,b)=>JSON.stringify(a)===JSON.stringify(b));}
  const sectionParse=method('parse(){const section_ids=this.cfgsections();','},tab(');
  const valueParse=method('parse(section_id){const active=this.isActive(section_id);','},write(section_id,formvalue)');
  const saved={exit:'usa',vnet_usa:netA,vnet_default:netB,vnet_seoul:netB,manual_networks:'1',expected_usa:'192.0.2.1',expected_seoul:'192.0.2.2',organization:'before'};
  const original={...saved};const section=lastMap.children[0];section.cfgsections=()=>['main'];
  for(const o of section.children){o.parse=()=>Promise.resolve();}
  const org=section.children.find(o=>o.option==='organization');Object.assign(org,{isActive:()=>true,isValid:()=>true,cfgvalue:()=>saved.organization,formvalue:()=> 'after',write:(sid,v)=>{saved.organization=v;}});org.parse=valueParse;
  await sectionParse.call(section);assert.equal(saved.organization,'after');
  for(const k of removed.concat('exit'))if(k in original)assert.equal(saved[k],original[k]);
 }

 // Exit directory now lives with the selector; refreshing must preserve current unsaved choice.
 location.pathname='/admin/services/routepilot/cloudflare/routing';const basic=await new View().render([null,null,[],directory]);assert.ok(!basic.textContent.includes('5. Workers 备用与 Cloudflare API 授权'));assert.ok(!basic.textContent.includes('配置备份与迁移'));assert.ok(basic.textContent.includes('账户出口'));
 const discover=lastMap.children[0].children.find(o=>o.option==='_discover');selections.exit=netB;selections.lan_mode='global';
 basic.find(e=>e.attrs.id==='cbi-routepilot-main-exit').querySelector('.cbi-value-field').appendChild(E('div',{class:'cbi-value-description'},'出口说明保留'));
 const beforeConfig=JSON.stringify(config),pending=discover.onclick();assert.equal(discover.onclick(),pending);
 selections.exit='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
 await new Promise(setImmediate);directoryReply({code:0,stdout:JSON.stringify({available:true,networks:[{name:'Fresh directory',id:netA}]})});await pending;
 assert.equal(selections.exit,'cccccccc-cccc-4ccc-8ccc-cccccccccccc');assert.equal(JSON.stringify(config),beforeConfig);
 assert.ok(basic.textContent.includes('Fresh directory'));assert.ok(basic.textContent.includes('出口说明保留'));assert.equal(selections.lan_mode,'global');
 syncDirectoryFailure=true;await discover.onclick();syncDirectoryFailure=false;assert.ok(basic.textContent.includes('读取失败，请重试'));
 location.pathname='/admin/services/routepilot/cloudflare/workers';const workersPage=await new View().render([null,null,[],directory,null,{nodes:[],node:{configured:true,fingerprint:'saved',relay_fingerprint:'core'}}]);assert.ok(workersPage.textContent.includes('备用节点与独立检测'));assert.ok(workersPage.textContent.includes('手动启用备用'));assert.ok(workersPage.textContent.includes('无需自备 VPS'));assert.ok(workersPage.textContent.includes('一键创建 Workers 并绑定域名'));assert.ok(workersPage.textContent.includes('开始自动优选'));assert.ok(workersPage.textContent.includes('API 令牌'));assert.ok(workersPage.textContent.includes('创建自定义令牌'));assert.ok(workersPage.textContent.includes('不会覆盖其他项目'));assert.ok(!workersPage.textContent.includes('配置备份与迁移'));
 const findLabel=name=>workersPage.find(e=>e.attrs['aria-label']===name),findButton=name=>workersPage.find(e=>e.tag==='button'&&e.textContent===name);
 backupNodeReply={configured:true,fingerprint:'saved'};relayReply={state:'done',results:[{address:'proxyip.cmliussss.net:443',profile:'cf-relay-v1',fingerprint:'core',finished_at:Date.now()/1000,forced:true,state:'ok'}]};await tick();assert.equal(findButton('应用已通过的主备顺序（不启用）').disabled,false);
 const cfBefore=JSON.stringify(config);findLabel('Cloudflare API 令牌').value='t'.repeat(40);
 await findButton('保存令牌并读取账户 / 域名').attrs.click();assert.equal(cloudInputs.at(-1).account_id,'');assert.equal(cloudInputs.at(-1).hostname,'');assert.equal(findLabel('Cloudflare API 令牌').value,'');
 const accountA='a'.repeat(32),accountB='c'.repeat(32),zoneA='b'.repeat(32);
 cloudReply={configured:true,account_id:'',hostname:'',zone_id:'',script:'routepilot-backup',state:'verified',accounts:[{id:accountA,name:'A'},{id:accountB,name:'B'}],zones:[]};await tick();
 const accountSelect=findLabel('选择已授权的 Cloudflare 账户');assert.equal(accountSelect.children.length,3);accountSelect.value=accountA;accountSelect.listeners.change();
 await findButton('保存令牌并读取账户 / 域名').attrs.click();assert.equal(cloudInputs.at(-1).account_id,accountA);
 cloudReply={...cloudReply,account_id:accountA,discovered_account_id:accountA,zones:[{id:zoneA,name:'example.com'}]};await tick();
 assert.equal(findLabel('绑定的子域名').value,'backup.example.com');assert.equal(findButton('检查名称和子域名是否占用').disabled,false);
 const rootOption=findLabel('选择账户下可用的根域名').children[1],accountOption=accountSelect.children[1];await tick();assert.equal(findLabel('选择账户下可用的根域名').children[1],rootOption);assert.equal(accountSelect.children[1],accountOption);
 const rowBefore=workersPage.find(e=>e.attrs.class==='rp-relay-row');activeElement=findLabel('PROXYIP 地址');await tick();assert.equal(workersPage.find(e=>e.attrs.class==='rp-relay-row'),rowBefore);activeElement=null;
 const prefix=findLabel('子域名前缀');prefix.value='new-backup';prefix.listeners.input();assert.equal(findLabel('绑定的子域名').value,'new-backup.example.com');
 await findButton('检查名称和子域名是否占用').attrs.click();assert.equal(cloudInputs.at(-1).hostname,'new-backup.example.com');assert.equal(cloudInputs.at(-1).zone_id,zoneA);
 prefix.value='https://bad';prefix.listeners.input();assert.equal(findLabel('绑定的子域名').value,'');assert.equal(findButton('检查名称和子域名是否占用').disabled,true);
 assert.equal(JSON.stringify(config),cfBefore);cloudReply=null;registerWrites=0;
 location.pathname='/admin/services/routepilot/cloudflare/account';
 warpReply={state:'available',protocol:'MASQUE',managed:true,source:'organization',message:'组织策略'};
 const centerDirectory={available:true,networks:[{id:netA,name:'美国出口'},{id:netB,name:'韩国出口'}]};
 let centerPage=await new View().render([null,null,[],centerDirectory,null,{nodes:[],node:{configured:true,fingerprint:'saved',relay_fingerprint:'core'}},warpReply]);
 assert.equal(centerPage.find(e=>e.attrs.id==='rp-center-advanced').attrs['aria-label'],'主线路账户与连接协议');assert.ok(!centerPage.textContent.includes('1. 主线路账户与连接协议'));
 for(const id of ['basic','devices','dns','workers'])assert.ok(!centerPage.find(e=>e.attrs.id==='rp-center-'+id));
 for(const section of ['account','endpoint','routing','devices','dns','workers'])assert.ok(centerPage.find(e=>e.tag==='a'&&e.attrs.href==='/admin/services/routepilot/cloudflare/'+section));
 assert.ok(centerPage.find(e=>e.attrs.id==='cbi-routepilot-main-warp_protocol'));assert.ok(!centerPage.find(e=>e.attrs.id==='cbi-routepilot-main-dns_foreign'));
 assert.ok(!centerPage.find(e=>e.attrs['aria-label']==='出口地区筛选（账户名称提示）'));
 await tick();assert.ok(centerPage.textContent.includes('实际协议'));assert.ok(!centerPage.textContent.includes('本次观测出口地区：美国'));
 const centerRoot=centerPage.find(e=>e.attrs.id==='cbi-routepilot');assert.ok(centerRoot);assert.equal(centerRoot.find(e=>e.attrs.id==='rp-center-advanced').tag,'section');
 await lastMap.renderContents();assert.equal(centerPage.find(e=>e.attrs.id==='cbi-routepilot'),centerRoot);assert.equal(centerRoot.find(e=>e.attrs.id==='rp-center-advanced').tag,'section');assert.ok(!centerRoot.find(e=>e.attrs.id==='cbi-routepilot-main-dns_foreign'));
 assert.ok(!centerPage.find(e=>e.attrs.class==='rp-guide-step rp-endpoint-card'));
 endpointResponse.candidates={entries:['162.159.197.2:443','[2606:4700:102::2]:443','162.159.197.2:8443']};
 location.pathname='/admin/services/routepilot/cloudflare/endpoint';
 centerPage=await new View().render([null,null,[],centerDirectory,null,{nodes:[]},warpReply]);
 assert.ok(centerPage.find(e=>e.attrs.id==='rp-center-endpoint'));
 assert.ok(!centerPage.find(e=>e.attrs.id==='rp-center-advanced'));
 assert.ok(!centerPage.find(e=>e.attrs.id==='cbi-routepilot-main-warp_protocol'));
 assert.ok(centerPage.find(e=>e.tag==='a'&&e.attrs['aria-current']==='page'&&e.textContent==='主线路入口'));
 // Track the real endpoint form's asynchronous revision without committing other form edits.
 const endpointPanel=centerPage.find(e=>e.tag==='section'&&e.children.some(c=>c instanceof Element&&c.tag==='h3'&&c.textContent==='入口选择与候选同步'));
 await new Promise(setImmediate);assert.ok(endpointPanel);
 // LuCI sets every non-null attribute with setAttribute: disabled=false still disables HTML options.
 const candidates=endpointPanel.find(e=>e.tag==='select'&&e.children.some(c=>c instanceof Element&&c.attrs.value==='162.159.197.2:443'));
 const entryOptions=candidates.children.filter(c=>c instanceof Element&&c.tag==='option');
 for(const opt of entryOptions.filter(o=>o.attrs.value&&o.attrs.value[0]!=='['))assert.ok(opt.attrs.disabled==null,'IPv4 must omit the disabled attribute, including false');
 assert.equal(entryOptions.find(o=>o.attrs.value[0]==='[').attrs.disabled,true);
 const modeControl=endpointPanel.find(e=>e.tag==='select'&&e.children.some(c=>c instanceof Element&&c.attrs.value==='custom'));
 const candidateField=endpointPanel.find(e=>e.tag==='label'&&e.textContent.includes('固定候选入口'));
 const customField=endpointPanel.find(e=>e.tag==='label'&&e.textContent.includes('手写 IPv4:端口'));
 const refreshCandidates=endpointPanel.find(e=>e.tag==='button'&&e.textContent==='读取客户端最新本地候选（不切换）');
 const syncInterval=endpointPanel.find(e=>e.tag==='select'&&e.children.some(c=>c instanceof Element&&c.attrs.value==='24'));
 assert.deepEqual(modeControl.children.map(c=>c.attrs.value),['auto','candidate','custom']);
 assert.equal(candidateField.hidden,true);assert.equal(customField.hidden,true);assert.equal(syncInterval.disabled,false);
 assert.equal(E('option',{disabled:false}).disabled,true,'LuCI forwards false as a present HTML boolean attribute');
 const callsBeforeMode=commandCalls.length;
 modeControl.value='candidate';modeControl.listeners.change();assert.equal(candidateField.hidden,false);assert.equal(customField.hidden,true);assert.equal(candidates.disabled,false);
 const entrySave=centerPage.find(e=>e.tag==='button'&&e.textContent==='应用并检测（可能重连）');
 const noCandidateCalls=commandCalls.filter(c=>c==='endpoint-save').length;await entrySave.attrs.click();assert.equal(commandCalls.filter(c=>c==='endpoint-save').length,noCandidateCalls);assert.ok(endpointPanel.textContent.includes('请先选择候选入口'));
 candidates.value='162.159.197.2:443';candidates.listeners.change();
 await refreshCandidates.attrs.click();assert.equal(candidates.value,'162.159.197.2:443');assert.equal(modeControl.value,'candidate');
 modeControl.value='custom';modeControl.listeners.change();assert.equal(candidateField.hidden,true);assert.equal(customField.hidden,false);
 const entryInput=centerPage.find(e=>e.tag==='input'&&e.attrs.placeholder==='IPv4:端口');
 entryInput.value='162.159.197.2:443';entryInput.listeners.input();
 modeControl.value='auto';modeControl.listeners.change();assert.equal(candidateField.hidden,true);assert.equal(customField.hidden,true);assert.ok(endpointPanel.textContent.includes('无需选择或填写 IP'));
 modeControl.value='custom';modeControl.listeners.change();assert.equal(customField.hidden,false);assert.equal(entryInput.value,'162.159.197.2:443');
 const beforeIntervalCommands=commandCalls.length;const beforeIntervalRevision=endpointResponse.settings.revision;syncInterval.value='12';syncInterval.listeners.change();await endpointPanel.find(e=>e.tag==='button'&&e.textContent==='只保存同步间隔（不重连）').attrs.click();assert.deepEqual(commandCalls.slice(beforeIntervalCommands),['endpoint-schedule','endpoint-info']);assert.equal(endpointResponse.settings.revision,beforeIntervalRevision);assert.equal(modeControl.value,'custom');
 const configBeforeEndpoint=JSON.stringify(config);await entrySave.attrs.click();assert.ok(centerPage.textContent.includes('等待后台处理本次请求'));
 entryInput.value='162.159.197.2:500';entryInput.listeners.input();
 endpointResponse={...endpointResponse,pending:false,application:{revision:'endpoint-new',state:'verifying'}};await tick();assert.ok(centerPage.textContent.includes('正在连接并验证原出口'));assert.equal(entryInput.value,'162.159.197.2:500');
 endpointResponse.application={revision:'endpoint-new',state:'applied',connected:true,exit_verified:true};await tick();assert.ok(centerPage.textContent.includes('原预期出口验证通过'));assert.equal(JSON.stringify(config),configBeforeEndpoint);assert.equal(entryInput.value,'162.159.197.2:500');
 // A second operation must terminate on a failure instead of leaving an old success visible.
 await entrySave.attrs.click();endpointResponse={...endpointResponse,pending:false,application:{revision:'endpoint-new',state:'failed',rollback_confirmed:true}};await tick();assert.ok(centerPage.textContent.includes('已恢复原入口设置'));
 // A recovery block overrides an already-saved revision and historical success.
 const entryRead=centerPage.find(e=>e.tag==='button'&&e.textContent==='读取当前设置 / 应用结果');
 entryInput.value='162.159.197.2:8095';entryInput.listeners.input();
 endpointResponse={...endpointResponse,pending:true,application:{revision:'old',state:'applied',connected:true,exit_verified:true},recovery_block:{new_request_block:true,startup_pause:true,terminal:true,code:'RECOVERY_LATCHED',reason:'需人工核对'}};
 await entryRead.attrs.click();assert.ok(centerPage.textContent.includes('入口请求受阻'));assert.ok(centerPage.textContent.includes('RECOVERY_LATCHED'));assert.ok(!endpointPanel.textContent.includes('等待后台处理本次请求'));assert.equal(entrySave.disabled,true);assert.equal(entryInput.value,'162.159.197.2:8095');
 const fullRecoveryBlock=endpointResponse.recovery_block;
 for(const terminal of [true,false]){
  endpointResponse={...endpointResponse,recovery_block:{new_request_block:true,startup_pause:true,terminal}};
  await entryRead.attrs.click();assert.ok(endpointPanel.textContent.includes('入口请求受阻'));assert.ok(!endpointPanel.textContent.includes('undefined'));assert.equal(entrySave.disabled,true);
 }
 endpointResponse.recovery_block=fullRecoveryBlock;
 endpointResponse={...endpointResponse,recovery_offer:{available:true,revision:'blocked',context:'c'.repeat(64)}};await entryRead.attrs.click();
 const recoverButton=endpointPanel.find(e=>e.tag==='button'&&e.textContent==='受控恢复原入口并验证');assert.equal(recoverButton.disabled,false);
 recoverButton.attrs.click();const confirmation=lastModal.find(e=>e.tag==='input'&&e.attrs.placeholder==='RESTORE');const confirmButton=lastModal.find(e=>e.tag==='button'&&e.textContent==='确认恢复并验证');const beforeConfirm=commandCalls.length;await confirmButton.attrs.click();assert.equal(commandCalls.length,beforeConfirm);
 confirmation.value='RESTORE';await confirmButton.attrs.click();assert.equal(endpointRecoveryInput.revision,'blocked');assert.equal(endpointRecoveryInput.context,'c'.repeat(64));assert.equal(JSON.stringify(config),configBeforeEndpoint);assert.ok(endpointPanel.textContent.includes('等待后台验证结果'));
 endpointResponse.recovery_result={request_id:commandCalls.length?'unrelated':'',state:'done',message:'historical',time:Date.now()/1000};await tick();assert.ok(!endpointPanel.textContent.includes('上次受控恢复：'));
 endpointResponse.recovery_result=null;
 const blockedCommands=commandCalls.length;await entrySave.attrs.click();assert.equal(commandCalls.length,blockedCommands);
 // A valid existing transaction still tracks verification to completion while new saves are disabled.
 endpointResponse={...endpointResponse,pending:false,recovery_block:{new_request_block:false}};await entryRead.attrs.click();assert.equal(entrySave.disabled,false);await entrySave.attrs.click();
 endpointResponse={...endpointResponse,pending:false,application:{revision:'endpoint-new',state:'applying'},recovery_block:{new_request_block:true,startup_pause:false,terminal:false,code:'TRANSACTION_ACTIVE',reason:'原事务处理中'}};await tick();assert.ok(!endpointPanel.textContent.includes('需要时重新保存'));assert.ok(endpointPanel.textContent.includes('必要时人工核对'));
 endpointResponse={...endpointResponse,pending:false,application:{revision:'endpoint-new',state:'verifying'},recovery_block:{new_request_block:true,startup_pause:false,terminal:false,code:'TRANSACTION_ACTIVE',reason:'原事务处理中'}};await tick();assert.ok(endpointPanel.textContent.includes('正在连接并验证原出口'));assert.equal(entrySave.disabled,true);
 endpointResponse={...endpointResponse,application:{revision:'endpoint-new',state:'applied',connected:true,exit_verified:true},recovery_block:{new_request_block:false}};await tick();assert.ok(endpointPanel.textContent.includes('原预期出口验证通过'));assert.equal(entrySave.disabled,false);
 modeControl.value='candidate';modeControl.listeners.change();candidates.value='162.159.197.2:8443';candidates.listeners.change();await entrySave.attrs.click();assert.equal(endpointInput.mode,'manual');assert.equal(endpointInput.address,'162.159.197.2:8443');
 modeControl.value='auto';modeControl.listeners.change();await entrySave.attrs.click();assert.equal(endpointInput.mode,'auto');assert.equal(endpointInput.address,'');assert.equal(candidateField.hidden,true);assert.equal(customField.hidden,true);assert.equal(JSON.stringify(config),configBeforeEndpoint);
 location.pathname='/admin/services/routepilot/cloudflare/account';
 centerPage=await new View().render([null,null,[],centerDirectory,null,{nodes:[]},warpReply]);
 const snapshot=JSON.parse(JSON.stringify(config));selections.organization='new-team';selections.dns_foreign='9.9.9.9';const pendingExit=selections.exit;
 await centerPage.find(e=>e.tag==='button'&&e.textContent==='保存组织名称').attrs.click();assert.equal(config.organization,'new-team');assert.equal(config.apply_nonce,'org-only');assert.equal(config.exit,snapshot.exit);assert.equal(config.dns_foreign,snapshot.dns_foreign);assert.equal(selections.exit,pendingExit);assert.equal(selections.dns_foreign,'9.9.9.9');
 for(const key of Object.keys(config))delete config[key];Object.assign(config,snapshot);registerWrites=0;
 // A saved token restores the independent domain cache even when the last job is deployment readiness.
 const domainCache={state:'cached',read_at:Date.now()/1000,stale:false,accounts:[{id:accountA,name:'Account A'},{id:accountB,name:'Account B'}],all_zones:[{id:zoneA,name:'example.com',account_id:accountA},{id:'d'.repeat(32),name:'second.example',account_id:accountA},{id:'e'.repeat(32),name:'third.example',account_id:accountA},{id:'f'.repeat(32),name:'fourth.example',account_id:accountB}]};
 const cloudMeta={configured:true,token_saved:true,account_id:accountA,zone_id:zoneA,script:'routepilot-backup',hostname:'cloud.example.com',directory:domainCache,projects:[{script:'routepilot-backup',hostname:'cloud.example.com',uploaded:true,bound:true,saved_as_backup:true}]};
 cloudReply={...cloudMeta,state:'ready',action:'check',started_at:Date.now()/1000-10,finished_at:Date.now()/1000-9,directory:{...domainCache,read_at:domainCache.read_at-10,all_zones:[]}};
 backupNodeReply={configured:true,fingerprint:'saved',sni:'cloud.example.com',address:'104.21.39.117',relay_fingerprint:'core'};
 location.pathname='/admin/services/routepilot/cloudflare/workers';const domainPage=await new View().render([null,null,[],centerDirectory,null,{nodes:[],cloud:cloudMeta,node:backupNodeReply},warpReply]);await tick();
 const domainControl=name=>domainPage.find(e=>e.attrs['aria-label']===name),domainButton=name=>domainPage.find(e=>e.tag==='button'&&e.textContent===name);
 assert.equal(domainControl('选择账户下可用的根域名').children.length,4);assert.equal(domainControl('绑定的子域名').value,'cloud.example.com');
 assert.equal(domainButton('将新部署设为备用节点（不启用）').disabled,true);
 assert.equal(domainPage.find(e=>e.attrs['data-stage']==='relay').tag,'section');
 assert.ok(domainPage.textContent.includes('当前保存的备用节点'));assert.ok(domainPage.textContent.includes('本路由器创建的云端节点'));assert.ok(domainPage.textContent.includes('✓ 已保存为当前备用'));
 const draftBefore=JSON.stringify(config),inputsBefore=cloudInputs.length,writesBefore=registerWrites;
 await domainButton('刷新账户与根域名（只读）').attrs.click();assert.equal(registerWrites,writesBefore);assert.equal(cloudInputs.length,inputsBefore);assert.equal(commandCalls.at(-1),'cloud-verify');
 cloudDirectoryReply={...domainCache,state:'verified',read_at:domainCache.read_at+1};await tick();assert.equal(domainControl('选择账户下可用的根域名').children.length,4);assert.equal(domainButton('将新部署设为备用节点（不启用）').disabled,true);
 // Submit a new read-only check while old success is still on disk: only the new job may finish it.
 cloudCommandReply={state:'submitted'};await domainButton('检查部署与证书状态').attrs.click();await tick();
 const progressPanel=domainPage.find(e=>e.attrs.class==='rp-operation-feedback');assert.ok(progressPanel.textContent.includes('正在等待后台接收确认'));assert.ok(progressPanel.find(e=>e.tag==='ol').children.every(e=>!e.textContent.startsWith('✓')));assert.equal(domainButton('将新部署设为备用节点（不启用）').disabled,true);
 const checkStart=Date.now()/1000+0.01;cloudReply={...cloudReply,action:'check',state:'running',phase:'connecting',started_at:checkStart,finished_at:null,message:'正在只读检查项目'};await tick();assert.ok(progressPanel.textContent.includes('已等待'));
 cloudReply={...cloudReply,state:'waiting_dns',finished_at:Date.now()/1000,message:'域名已绑定，等待证书'};await tick();assert.equal(progressPanel.attrs['data-state'],'waiting');assert.ok(progressPanel.textContent.includes('域名已绑定，等待证书'));
 await tick();await domainButton('检查部署与证书状态').attrs.click();cloudReply={...cloudReply,state:'ready',started_at:checkStart+0.02,finished_at:Date.now()/1000,message:'HTTPS ready'};await tick();assert.equal(progressPanel.attrs['data-state'],'success');assert.ok(progressPanel.textContent.includes('Worker 与域名 HTTPS 已确认就绪'));assert.ok(domainPage.find(e=>e.attrs.class==='rp-worker-activity').textContent.includes('Worker 与域名 HTTPS 已确认就绪'));assert.ok(!progressPanel.textContent.includes('正在等待后台接收确认'));assert.equal(JSON.stringify(config),draftBefore);assert.equal(registerWrites,writesBefore);cloudCommandReply=null;
 const callsAfterManualCheck=commandCalls.length;cloudDirectoryReply={...cloudDirectoryReply,stale:true};await tick();assert.equal(commandCalls.length,callsAfterManualCheck,'background directory refresh must not replace the result of an explicit user operation');
 const chooseAccount=domainControl('选择已授权的 Cloudflare 账户');chooseAccount.value=accountB;chooseAccount.listeners.change();assert.equal(domainControl('选择账户下可用的根域名').children[1].textContent,'fourth.example');assert.equal(registerWrites,writesBefore);assert.equal(JSON.stringify(config),draftBefore);
 chooseAccount.value=accountA;chooseAccount.listeners.change();domainControl('选择账户下可用的根域名').value=zoneA;domainControl('选择账户下可用的根域名').listeners.change();
 // All settings remain visible and mounted while polling refreshes evidence.
 const useStage=domainPage.find(e=>e.attrs['data-stage']==='use');assert.equal(useStage.tag,'section');const help=useStage.find(e=>e.tag==='details'&&e.textContent.includes('无需自备 VPS'));assert.ok(help);assert.equal(help.open,false);assert.equal(help.find(e=>['button','input','select','textarea'].includes(e.tag)),null);await tick();assert.equal(domainPage.find(e=>e.attrs['data-stage']==='use'),useStage);
 cloudReply=null;cloudDirectoryReply=null;backupNodeReply=null;registerWrites=0;
 location.pathname='/admin/services/routepilot/cloudflare/dns';
 // Private cache and DNS actions never call save/apply or change configuration.
 location.pathname='/admin/services/routepilot/cloudflare/dns';current={...current,state:'running',time:Date.now()/1000};
 dnsData={schema:1,state:'running',time:Date.now()/1000,runtime_id:current.runtime_id,generation:current.generation,process_instance:'p1',control_available:true,upstreams:{dns_domestic:'223.5.5.5',dns_foreign:'1.1.1.1',transport:'tcp'},policies:[{index:1,cache_entries:2,cache_hits:1,requests:3,errors:0}]};
 const dnsPage=await new View().render([null,null,[]]);await tick();const baselineConfig=JSON.stringify(config);
 await dnsPage.find(e=>e.tag==='button'&&e.textContent==='清理本插件 DNS 缓存').attrs.click();await new Promise(setImmediate);
 assert.ok(notices.some(n=>n.includes('移除 2 条')));
 dnsPage.find(e=>e.tag==='button'&&e.textContent==='检测当前 DNS 解析').attrs.click();await new Promise(setImmediate);await tick();assert.ok(dnsPage.textContent.includes('等待本次 DNS 检测作业'));
 dnsTests={...current,request_id:'job-new',state:'done',process_instance:'p1',finished_at:Date.now()/1000,results:['domestic','proxy'].flatMap(target=>['udp','tcp'].map(protocol=>({policy:1,target,domain:'example.com',protocol,upstream:'1.1.1.1',configured_route:'proxy',state:'ok',message:'解析成功',addresses:['203.0.113.7'],ms:1})))};
 await tick();assert.ok(dnsPage.textContent.includes('本机受管 DNS 入口解析通过'));assert.equal(JSON.stringify(config),baselineConfig);
 dnsData={...dnsData,process_instance:'p2'};await tick();assert.ok(dnsPage.textContent.includes('历史 DNS 结果已过期'));
 // Registration single-flight and post-registration directory invalidation are retained.
 location.pathname='/admin/services/routepilot/cloudflare/account';config.enabled='0';current={...current,state:'preparing'};
 let runtimeResolve;const freshAccount=[null,null,[],{available:false,networks:[]}];
 freshAccount.runtimeRead=new Promise(resolve=>{runtimeResolve=resolve;});
 const initialReads=directoryCalls,initialProtocolCalls=commandCalls.filter(c=>c==='warp-info').length;
 const uninstalled=await new View().render(freshAccount);await tick();
 const guideStage=page=>page.find(e=>e.attrs['data-setup-stage']).attrs['data-setup-stage'];
 const guideButton=(page,label)=>page.find(e=>e.tag==='button'&&e.textContent===label);
 assert.equal(guideStage(uninstalled),'install');
 assert.ok(guideButton(uninstalled,'准备登录环境').disabled);
 assert.ok(guideButton(uninstalled,'检查账户').disabled);
 runtimeResolve({installed:false,registration_files_present:false});await Promise.resolve();
 assert.equal(guideStage(uninstalled),'install');
 assert.ok(guideButton(uninstalled,'邮箱登录').disabled);
 assert.equal(directoryCalls,initialReads,'the installation check must precede automatic account access');
 assert.equal(commandCalls.filter(c=>c==='warp-info').length,initialProtocolCalls);
 const installedAccount=[null,null,[],{available:false,networks:[]}];
 installedAccount.runtimeRead=Promise.resolve({installed:true,registration_files_present:false});
 const unregistered=await new View().render(installedAccount);await tick();
 assert.equal(guideStage(unregistered),'account');
 assert.equal(guideButton(unregistered,'准备登录环境').disabled,false);
 assert.equal(guideButton(unregistered,'邮箱登录').disabled,false);
 assert.ok(guideButton(unregistered,'检查账户').disabled);
 assert.equal(directoryCalls,initialReads,'an unregistered installation must wait for account login');
 const registrationView=new View(),registrationPage=await registrationView.render([null,null,[],{available:false,networks:[]}]);const options=lastMap.children[0].children;
 registrationView.applySettings=()=>{throw Error('merged center must not save unrelated form edits');};const beforeCommands=commandCalls.length;await options.find(o=>o.option==='_prepare').onclick();assert.deepEqual(commandCalls.slice(beforeCommands),['warp-org-save','prepare']);
 registrationView.applySettings=()=>Promise.resolve(true);failStatusRead=true;await options.find(o=>o.option==='_login').onclick();failStatusRead=false;assert.notEqual(registrationPage.find(e=>e.attrs['data-step']==='auth').attrs['data-state'],'error');
 const first=options.find(o=>o.option==='_account_check').onclick();await new Promise(setImmediate);const beforeCalls=directoryCalls;
 await options.find(o=>o.option==='_login').onclick();
 const authorization=lastModal.find(e=>e.tag==='input');
 assert.equal(authorization.attrs.type,'password');
 assert.ok(lastModal.textContent.includes('复制含授权链接的整行即可'));
 const reg=lastModal.find(e=>e.tag==='button'&&e.textContent==='注册此路由器');
 reg.attrs.click();assert.equal(registerWrites,0);assert.equal(activeElement,authorization);
 authorization.value="onclick=\"location.href='com.cloudflare.warp\\://example.cloudflareaccess.com/auth?token=test-only';\"";
 const registering=reg.attrs.click();reg.attrs.click();assert.equal(registerWrites,1);
 assert.equal(authorization.value,'');
 await new Promise(setImmediate);directoryReply({code:0,stdout:JSON.stringify({available:true,networks:[{name:'Old account response',id:netA}]})});await first;await new Promise(setImmediate);assert.equal(directoryCalls,beforeCalls+1);
 assert.equal(guideStage(registrationPage),'account','saved routing cannot skip the post-registration catalog');
 assert.ok(registrationPage.textContent.includes('路由器已注册，正在读取账户出口'));
 directoryReply({code:0,stdout:JSON.stringify({...directory,networks:[]})});await registering;
 assert.equal(guideStage(registrationPage),'account','an empty account catalog cannot guide a fresh setup to device configuration');
 assert.ok(registrationPage.textContent.includes('没有返回可选网络'));
 const retryCatalog=options.find(o=>o.option==='_account_check').onclick();await new Promise(setImmediate);
 directoryReply({code:0,stdout:JSON.stringify(directory)});await retryCatalog;
 assert.equal(guideStage(registrationPage),'power','a usable directory enables the next step for saved configuration');
 assert.ok(registrationPage.textContent.includes('已读取 2 个账户网络，连通性待验证'));
 assert.ok(!registrationPage.textContent.includes('Old account response'));
 // The top status must follow an existing account even when the supervisor still emits first-use text.
 current={...current,state:'preparing',time:Date.now()/1000,message:'登录环境已就绪；请完成邮箱登录，再自动读取出口'};
 const existingAccount=[null,null,[],{available:false,networks:[]}];
 existingAccount.runtimeRead=Promise.resolve({installed:true,registration_files_present:true});
 const registeredPage=await new View().render(existingAccount);await tick();await new Promise(setImmediate);
 const summary=registeredPage.querySelector('.rp-summary-connection');
 assert.match(summary.textContent,/正在检查账户/);
 assert.doesNotMatch(summary.textContent,/请完成邮箱登录/);
 directoryReply({code:0,stdout:JSON.stringify(directory)});await new Promise(setImmediate);
 assert.match(summary.textContent,/分流尚未开启；已读到 2 个账户网络（未验证连通性）/);
 assert.match(summary.textContent,/WARP 已注册/);
 // Real load -> render migration: legacy URLs retain behavior through the canonical center.
 for(const [alias,group] of Object.entries({basic:'basic',devices:'devices',dns:'dns',diagnostics:'dns',advanced:'advanced',workers:'workers'})){
  location.pathname='/admin/services/routepilot/'+alias;legacyReplacement='';const instance=new View();const loaded=await instance.load();const node=await instance.render(loaded);
  const section={basic:'routing',advanced:'account',devices:'devices',dns:'dns',workers:'workers'}[group];assert.equal(legacyReplacement,'/admin/services/routepilot/cloudflare/'+section);assert.ok(!node.textContent.includes('[object Object]'),'nested children must not stringify into the page');assert.equal(node.attrs['data-page'],section);assert.ok(node.find(e=>e.attrs.id==='rp-center-'+group));
 }
 // Each canonical URL renders one section and polls only its own runtime data.
 const expectedReads={account:['status.json','warp-state.json','exit-observation.json','speed-compare.json'],endpoint:['status.json','exit-observation.json','endpoint-quality.json'],routing:['status.json','connectivity.json','exit-observation.json','speed-compare.json'],devices:['status.json'],dns:['status.json','dns-state.json','dns-tests.json','warp-dns-tests.json','lan-dns-domestic.json','lan-dns-foreign.json'],workers:['status.json','backup-node.json','backup-test.json','cloud-state.json','backup-scan.json','relay-tests.json','relay-observed.json','exit-observation.json','connection-events.json','cloud-directory-state.json']};
 for(const [section,group] of Object.entries({account:'advanced',endpoint:'endpoint',routing:'basic',devices:'devices',dns:'dns',workers:'workers'})){
  location.pathname='/admin/services/routepilot/cloudflare/'+section;
  const before=JSON.stringify(config),node=await new View().render([null,null,[],directory]);
  assert.ok(!node.textContent.includes('[object Object]'),'nested children must not stringify into the page');assert.equal(node.attrs['data-page'],section);assert.ok(node.find(e=>e.attrs.id==='rp-center-'+group));
  for(const other of ['advanced','endpoint','basic','devices','dns','workers'].filter(x=>x!==group))assert.ok(!node.find(e=>e.attrs.id==='rp-center-'+other));
  if(section==='endpoint')current={...current,state:'running',time:Date.now()/1000,protocol:'MASQUE'};
  readCalls=[];await tick();if(section==='endpoint'){assert.ok(node.textContent.includes('当前使用 WARP 主线路'));assert.ok(!node.textContent.includes('状态过期，当前连接未确认'));}
  assert.ok(!node.textContent.includes('[object Object]'),'poll updates must preserve DOM children');assert.deepEqual(readCalls.map(p=>path.basename(p)).sort(),expectedReads[section].sort());
  const previousTick=tick;location.pathname='/admin/services/routepilot/backup';readCalls=[];await previousTick();assert.deepEqual(readCalls,[],'departed page must stop polling');assert.equal(JSON.stringify(config),before);
 }
 // The two read-only buttons have independent in-flight state.
 location.pathname='/admin/services/routepilot/cloudflare/account';
 current={...current,state:'running',backend:'masque',time:Date.now()/1000};
 const oldExec=mockedFS.exec.bind(mockedFS),savedExit=config.exit;
 const refreshProtocol={},inspectExit={};
 const exitResult={...current,state:'done',country:'US',ip:'192.0.2.1'};
 let protocolCalls=0,exitCalls=0;
 mockedFS.exec=(path,args)=>{
  if(path==='/usr/libexec/routepilot'&&args[0]==='warp-info'){
   protocolCalls++;return new Promise(resolve=>{refreshProtocol.resolve=resolve;});
  }
  if(path==='/usr/libexec/routepilot'&&args[0]==='exit-observe'){
   exitCalls++;return new Promise(resolve=>{inspectExit.resolve=resolve;});
  }
  return oldExec(path,args);
 };
 config.exit='';
 const accountPage=await new View().render([null,null,[],directory,null,{nodes:[]}]);await tick();
 const selectExit=accountPage.find(e=>e.tag==='a'&&e.textContent==='前往出口与分流');
 assert.equal(selectExit.attrs.href,'/admin/services/routepilot/cloudflare/routing');
 assert.equal(accountPage.find(e=>e.attrs['data-setup-stage']).attrs['data-setup-stage'],'routing');
 const protocolButton=accountPage.find(e=>e.tag==='button'&&e.textContent==='正在读取协议…');
 const regionButton=accountPage.find(e=>e.tag==='button'&&e.textContent==='检测出口地区');
 assert.equal(protocolButton.disabled,true);
 assert.equal(regionButton.disabled,false);
 const result=regionButton.attrs.click();regionButton.attrs.click();
 assert.equal(regionButton.textContent,'正在检测地区…');
 assert.equal(exitCalls,1);
 inspectExit.resolve({code:0,stdout:JSON.stringify(exitResult)});await result;
 assert.equal(regionButton.disabled,false);
 assert.equal(protocolButton.disabled,true);
 const protocolRequest=protocolButton.attrs.click();
 assert.equal(protocolCalls,1);
 refreshProtocol.resolve({code:0,stdout:JSON.stringify({state:'available',protocol:'MASQUE'})});await protocolRequest;
 assert.equal(protocolButton.textContent,'读取当前协议');
 assert.equal(protocolButton.disabled,false);
 current={...current,state:'disabled',time:Date.now()/1000};await tick();
 assert.equal(regionButton.disabled,true);
 assert.ok(accountPage.textContent.includes('线路运行后可检测地区'));
 config.exit=savedExit;
 console.log('Real view: five independent pages, scoped polling, preserved drafts/configuration, isolated DNS actions and registration single-flight passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

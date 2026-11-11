'use strict';
'require view';
'require form';
'require uci';
'require fs';
'require ui';
'require poll';
'require rpc';
'require dom';
'require routepilot.feedback_8f30a36f4eaa as feedback';
var callHostHints = rpc.declare({object:'luci-rpc',method:'getHostHints',expect:{'':{}}});
var commitConfig = rpc.declare({object:'uci',method:'commit',params:['config'],expect:{'':{}}});

function refreshMenuIndex(){return ui.menu.load().then(function(tree){var node=tree;['admin','services','routepilot'].forEach(function(k){node=node&&node.children&&node.children[k];});if(node&&node.children&&(!node.children.cloudflare||!node.children.runtime||['basic','devices','dns','advanced','workers'].some(function(k){return node.children[k]&&node.children[k].title;})))ui.menu.flushCache();}).catch(function(){});}
function readExitDirectory() { return fs.exec('/usr/libexec/routepilot-exits',[]).then(function(r){if(r.code)throw Error('directory');var v=JSON.parse(r.stdout);if(!v.available||!Array.isArray(v.networks))throw Error('directory');v.read_at=Date.now();v.last_success_at=v.read_at;return v;}).catch(function(){return {available:false,networks:[],read_at:Date.now()};}); }
function readHostHints(){
 return new Promise(function(resolve){
  var timer=setTimeout(function(){resolve(null);},2000);
  Promise.resolve().then(function(){return callHostHints();}).then(function(rows){clearTimeout(timer);resolve(rows&&typeof rows==='object'&&!Array.isArray(rows)?rows:null);},function(){clearTimeout(timer);resolve(null);});
 });
}
function readOverviewSnapshot(){
 var files=[];
 return Promise.all([['status.json',0],['connectivity.json',2],['traffic.json',14],['connection-events.json',15]].map(function(item){return fs.read('/var/run/routepilot/'+item[0]).then(JSON.parse).catch(function(){return null;}).then(function(value){files[item[1]]=value;});})).then(function(){return files;});
}
function normalizedID(value){return String(value||'').toLowerCase();}
function configuredExit(){var value=uci.get('routepilot','main','exit');return value==null?'seoul':value;}
function savedExitID(key){return ['default','seoul','usa'].indexOf(key)>=0?normalizedID(uci.get('routepilot','main','vnet_'+key)):normalizedID(key);}
function directoryLabel(key,catalog){if(key==null)key=configuredExit();var id=savedExitID(key),item=(catalog.networks||[]).filter(function(n){return n.id===id;})[0];return item?item.name+(item.is_default?' · 账户默认网络':''):(['default','seoul','usa'].indexOf(key)>=0?({default:'旧默认',seoul:'旧首尔',usa:'旧美国'}[key])+'（兼容配置）':String(key||'未选择'));}
var sectionGroups={account:'advanced',endpoint:'endpoint',routing:'basic',devices:'devices',dns:'dns',workers:'workers'};
var legacyPages={basic:'routing',devices:'devices',dns:'dns',diagnostics:'dns',advanced:'account',workers:'workers'};
var groupSections={advanced:'account',endpoint:'endpoint',basic:'routing',devices:'devices',dns:'dns',workers:'workers'};
var ruleSections={'rules-overview':'overview','rules-sources':'sources','rules-manage':'manage','rules-custom':'custom'},ruleSectionNames={overview:'更新概况',sources:'来源与频率',manage:'规则管理',custom:'自定义分流'};
var activeRender=0;
var imokStylesheet='routepilot/style_abf8f564a12f.css';
function routeLeaf(){var parts=window.location.pathname.split('/').filter(Boolean),leaf=parts.pop();if(parts[parts.length-1]==='rules'&&Object.keys(ruleSectionNames).indexOf(leaf)>=0)return 'rules-'+leaf;if(leaf==='rules')return 'rules-overview';if(leaf==='routepilot')return 'overview';if(leaf==='cloudflare')return groupSections[(window.location.hash||'').replace('#rp-center-','')]||'account';return parts[parts.length-1]==='cloudflare'&&sectionGroups[leaf]?leaf:legacyPages[leaf]||leaf;}
return view.extend({
 exitOptions: function(current,mapping,catalog) {
  var id=['default','seoul','usa'].indexOf(current)>=0?normalizedID(mapping[current]):normalizedID(current),found=false;
  var networks=catalog.available?catalog.networks:[],names={};networks.forEach(function(n){names[n.name]=(names[n.name]||0)+1;});
  var options=networks.map(function(n){var same=n.id===id;found=found||same;return {value:same&&current?current:n.id,label:n.name+(n.is_default?' · 账户默认网络':'')+(names[n.name]>1?' · '+n.id.slice(0,8):'')};});
  if(!found)options.unshift({value:current||'',label:current?'保留当前选择：'+String(current)+'（'+(catalog.available?'未在本次目录中':!catalog.read_at?'目录尚未读取，保留原值':'目录读取失败，保留原值')+'）':'请选择账户出口（不会自动选择）'});
  return options;
 },
 load: function() {
  var parts=window.location.pathname.split('/').filter(Boolean),raw=parts[parts.length-1],leaf=routeLeaf();
  if((legacyPages[raw]&&parts[parts.length-2]!=='cloudflare'||raw==='cloudflare'&&window.location.hash)&&typeof window.location.replace==='function')window.location.replace(L.url('admin/services/routepilot/cloudflare/'+leaf));
  if(leaf==='runtime')return L.require('routepilot.runtime_9c815026873f').then(function(runtimeView){return runtimeView.load().then(function(runtimeData){return {runtimeView:runtimeView,runtimeData:runtimeData};});});
  if(leaf==='clients')return L.require('routepilot.clients_dc0db1c2b643').then(function(clientView){return clientView.load().then(function(clientData){return {clientsView:clientView,clientsData:clientData};});});
  var devices=leaf==='devices',interfaces=devices||leaf==='routing';
  var initialOverviewRead=leaf==='overview'?readOverviewSnapshot():null;
  var runtimeRead=leaf==='overview'?Promise.resolve().then(function(){return fs.exec('/usr/libexec/routepilot-runtime-status',[]);}).then(function(r){var d=r.code?null:JSON.parse(r.stdout);return d&&d.schema===1&&typeof d.installed==='boolean'?d:null;}).catch(function(){return null;}):Promise.resolve(null);
  return Promise.all([uci.load('routepilot'),devices?readHostHints():Promise.resolve(null),interfaces?fs.exec('/usr/libexec/routepilot',['interfaces']).then(function(r){return r.code?[]:JSON.parse(r.stdout);}).catch(function(){return [];}):Promise.resolve([]),Promise.resolve({available:false,networks:[],skipped:!['overview','account','routing'].includes(leaf)}),refreshMenuIndex(),leaf==='workers'?fs.exec('/usr/libexec/routepilot-backup-list',[]).then(function(r){return r.code?{nodes:[]}:JSON.parse(r.stdout);}).catch(function(){return {nodes:[]};}):Promise.resolve({nodes:[]}),Promise.resolve(null),fs.read('/usr/share/routepilot/version.json').then(JSON.parse).catch(function(){return null;})]).then(function(data){data.runtimeRead=runtimeRead;data.initialOverviewRead=initialOverviewRead;return data;});
 },
 render: function(data) {
  if(data&&data.runtimeView){this.handleSaveApply=null;this.handleSave=null;return data.runtimeView.render(data.runtimeData,L.resource(imokStylesheet),boundRequest);}
  if(data&&data.clientsView){this.handleSaveApply=null;this.handleSave=null;return data.clientsView.render(data.clientsData,L.resource(imokStylesheet),boundRequest);}
  var pages=[['account','账户与协议','登录身份与主线路连接协议'],['endpoint','主线路入口','入口选择、候选读取与定期同步'],['routing','出口与分流','直接选择账户出口与接管范围'],['devices','设备','独立设备策略'],['dns','DNS','解析、缓存与联网检测'],['workers','Workers','手动备用的部署、转发与检测'],['overview','总览','开关与当前运行状态'],['rules-overview','更新概况','更新状态、运行版本与订阅变化'],['rules-sources','来源与频率','选择或编辑订阅地址，设置自动更新间隔'],['rules-manage','规则管理','浏览规则，维护长期保留的个人新增和排除'],['rules-custom','自定义分流','独立维护始终直连和始终代理的规则'],['backup','备份迁移','导入或导出本插件配置']];
  var leaf=routeLeaf(),page=pages.some(function(p){return p[0]===leaf;})?leaf:'overview';
  var ruleSection=ruleSections[page],ruleDataPage=!!ruleSection&&ruleSection!=='custom';
  var pageInfo=pages.filter(function(p){return p[0]===page;})[0],base=L.url('admin/services/routepilot'),renderToken=++activeRender;
  function isCurrentView(){return renderToken===activeRender&&routeLeaf()===page;}
  var pageGroup=sectionGroups[page],center=!!pageGroup,centerDeviceNode=null,centerFields={basic:[],dns:[]},warpState=data[6]||null,exitObservation=null,saveInFlight=null,organizationOption=null,centerComposed=null,workerStages={},workerBadges={},workerStageLinks={},workerStageStates={},workerWorkspace=null,workerPinned=false,workerFlow={},buildInfo=data[7]||{};
  var workerActions={},workerDisplays={},workerPanels={},workerMessages={},workerActivity=E('div',{class:'rp-worker-activity',role:'status','aria-live':'polite'}),workerProjectRows=(data[5]&&data[5].cloud&&data[5].cloud.projects)||[],workerProjectsAt=Number(data[5]&&data[5].cloud&&data[5].cloud.projects_read_at||0),cloudDirectoryJob=null,workerInventory=null;
  var formPage=['account','routing','devices','dns','rules-sources','rules-custom'].includes(page);
  if(!formPage){this.handleSaveApply=null;this.handleSave=null;}
  var m = this.map = new form.Map('routepilot', '', '');
  var sourcePickers={};
  var s, o, self=this, hints=data[1],catalog=data[3]||{available:false,networks:[]};
  function panel(title,description,children,extra){return E('section',{class:'imok-panel'+(extra?' '+extra:'')},[E('div',{class:'imok-panel-heading'},[E('h3',{},title),description?E('p',{class:'rp-help'},description):null].filter(function(item){return item!=null;})),E('div',{class:'imok-panel-body'},children)]);}
  function finishPage(root){
   if(!formPage||!root||root.nodeType!==1||typeof requestAnimationFrame!=='function'||typeof MutationObserver!=='function')return root;
   requestAnimationFrame(function(){if(!root.isConnected)return;var host=root.parentElement,owned=null;function mark(){if(!root.isConnected||!isCurrentView()){if(owned&&owned._imokRoot===root){owned.classList.remove('imok-page-actions');delete owned._imokRoot;}observer.disconnect();return;}var actions=host.querySelector('.cbi-page-actions');if(actions){actions.classList.add('imok-page-actions');actions._imokRoot=root;owned=actions;}}var observer=new MutationObserver(mark);observer.observe(host,{childList:true});mark();});return root;
  }
  function showImokModal(title,children){return ui.showModal(title,children,'imok-dialog');}
  function notify(msg,err) { ui.addNotification(null,E('p',{class:'imok-notice','data-tone':err?'error':'info'},msg),err?'error':'info'); }
  function command(action,requestId) { return fs.exec('/usr/libexec/routepilot',requestId?[action,requestId]:[action]).then(function(r){ if(r.code)throw new Error(r.stderr || _('操作未完成')); return r.stdout; }); }
  function submitInput(name,action,payload){var request=boundRequest(payload);return fs.write('/var/run/routepilot/requests/'+name+'-'+request.id+'.json',request.text,384).then(function(){return command(action,request.token);}).then(function(raw){var result=JSON.parse(raw);if(result.request_id!==request.id)throw Error(_('未收到本次请求确认，请读取当前状态后重试'));return result;});}
  function save(){return (center?saveOrganizationOnly():self.applySettings()).then(function(ok){if(ok===false)throw Error(_('配置未保存，后续操作未执行'));return ok;});}
  var wizardActions={},wizardCards={},wizardBadges={},wizardActive=null,wizardPinned=false,wizardInitialized=false,wizardAdvance=null;
  var wizardStatus=null,wizardTest=null,exitOption=null,liveDNS=null,updateWizard=function(){},wizardSummary=E('p',{role:'status','aria-live':'polite'});
  var committedOrganization=uci.get('routepilot','main','organization')||'';
  function wizardConfig(){return {organization:committedOrganization,exit:configuredExit(),exit_id:savedExitID(configuredExit()),nonce:uci.get('routepilot','main','apply_nonce'),scope:uci.get('routepilot','main','scope'),lans:(uci.get('routepilot','main','lan_interfaces')||uci.get('routepilot','main','lan_interface')||'').length,devices:uci.sections('routepilot','policy').filter(function(p){return p.enabled==='1'&&p.mode!=='direct';}).length};}
  function openWizardStep(id){if(!wizardCards[id])return;Object.keys(wizardCards).forEach(function(key){wizardCards[key].setAttribute('data-current',key===id?'true':'false');});wizardActive=id;}
  function beginWizardStep(id){wizardPinned=false;wizardActive=id;wizardAdvance=id;wizardActions[id]={state:'busy',started:Date.now(),baseline:id==='verify'?feedback.wizardTestToken(wizardTest):null};updateWizard();}
  function failWizardStep(id){var previous=wizardActions[id];wizardActions[id]={state:'error',started:Date.now(),baseline:previous&&previous.baseline};updateWizard();}

  var directoryStatus=E('p',{role:'status','aria-live':'polite'}),directoryRows=E('tbody'),directoryPending=null,directoryGeneration=0,registeredHere=false,lastDirectoryRows=null;
  function renderDirectory(){
   var age=Date.now()-Number(catalog.read_at),recent=catalog.available&&age>=-5000&&age<=60000;
   directoryStatus.textContent=directoryPending?_('正在读取账户出口，请稍候（客户端查询最长约 8 秒）…'):catalog.available?(!recent?_('上次读取时间已失效，请刷新确认；'): '')+_('已读取 ')+catalog.networks.length+_(' 个账户网络；未切换出口，也未验证连通性。'):!catalog.read_at?_('尚未读取账户目录；可点击下方刷新按钮。'):(registeredHere?_('已注册，目录暂未就绪，请稍后刷新；当前出口与配置未改动。'):_('读取失败，请重试；当前出口与配置未改动。若持续失败，请检查登录环境。'));
   var renderKey=[catalog.read_at,catalog.available,recent,wizardStatus&&wizardStatus.state,wizardStatus&&wizardStatus.runtime_id,feedback.wizardTestToken(wizardTest)].join('|');
   if(renderKey===lastDirectoryRows){updateWizard();return;}lastDirectoryRows=renderKey;
   var rows=(catalog.available?catalog.networks:[]).map(function(n){return E('tr',{},[E('td',{},n.name+(n.is_default?' · 账户默认网络':'')),E('td',{},exitHealth(n.id))]);});
   if(!rows.length)rows.push(E('tr',{},E('td',{colspan:2},catalog.available?_('账户目录中没有可选虚拟网络。'):_('本次未取得出口目录；不代表账户没有其他出口。'))));
   directoryRows.replaceChildren.apply(directoryRows,rows);
   updateWizard();
  }
  function refreshDirectory(force){
   if(force){directoryGeneration++;catalog={available:false,networks:[],last_success_at:catalog.last_success_at};}
   if(directoryPending)return force?directoryPending.then(function(){return refreshDirectory();}):directoryPending;
   directoryStatus.textContent=_('正在读取账户出口，请稍候（客户端查询最长约 8 秒）…');
   var generation=directoryGeneration;
   directoryPending=Promise.resolve().then(readExitDirectory).catch(function(){return {available:false,networks:[],read_at:Date.now()};}).then(function(next){if(!isCurrentView()||generation!==directoryGeneration)return next;next.last_success_at=next.available?next.read_at:catalog.last_success_at;catalog=next;return updateExitChoices().then(function(){return next;});}).finally(function(){directoryPending=null;renderDirectory();});
   return directoryPending;
  }
  function updateExitChoices(){
   if(!isCurrentView()||page!=='routing'||!exitOption)return Promise.resolve();
   var frame=document.getElementById('cbi-routepilot-main-exit'),field=frame&&frame.querySelector('.cbi-value-field');
   if(!field)return Promise.resolve();
   var current=exitOption.formvalue('main');if(current==null)current=configuredExit();
   exitOption.keylist=[];exitOption.vallist=[];
   self.exitOptions(current,{default:uci.get('routepilot','main','vnet_default'),seoul:uci.get('routepilot','main','vnet_seoul'),usa:uci.get('routepilot','main','vnet_usa')},catalog).forEach(function(item){exitOption.value(item.value,item.label);});
   var description=field.querySelector('.cbi-value-description');
   return Promise.resolve(exitOption.renderWidget('main',0,current)).then(function(widget){if(description)field.replaceChildren(widget,description);else field.replaceChildren(widget);if(typeof exitOption.triggerValidation==='function')exitOption.triggerValidation('main');});
  }
  function exitHealth(id){
   var action=wizardActions.verify;if(action&&action.state==='error')return _('本次检测未完成，请重试');if(action&&action.state==='busy'&&action.baseline===feedback.wizardTestToken(wizardTest))return _('正在请求检测，等待新结果');
   var result=feedback.wizardProxyVerification(wizardTest,wizardStatus,wizardConfig(),Date.now());
   if(!wizardStatus||wizardStatus.exit!==configuredExit()||id!==savedExitID(wizardStatus.exit))return _('未检测');
   return ({success:_('✓ 当前连接检测通过'),error:_('✕ 当前连接检测失败'),warning:_('! 当前连接访问受限'),busy:_('… 当前连接检测中')})[result]||_('未检测 / 旧结果已过期');
  }
  if(page==='account'||page==='routing')renderDirectory();
  s=m.section(form.NamedSection,'main','main',pageInfo[1]);
  var mainSection=s;
  function pageOption(tab){var option=s.option.apply(s,Array.prototype.slice.call(arguments,1));option.routepilotPage=tab;if(center)option.retain=true;if(centerFields[tab])centerFields[tab].push(option);return option;}
  function sourcePresetField(option,kind){
   var render=option.renderWidget.bind(option);
   option.renderWidget=function(section,index,value){return Promise.resolve(render(section,index,value)).then(function(widget){
    var select=E('select',{class:'cbi-input-select','aria-label':({'china':'中国 IP','china_domains':'中国域名','gfw':'代理域名','whitelist':'直连域名'})[kind]+_('订阅预设')},[E('option',{value:'custom'},_('自定义地址 / 保留当前地址'))]),downloaded=E('p',{class:'rp-help'},_('正在读取已下载版本的来源…'));
    var saved=E('p',{class:'rp-help'},_('当前已保存地址：')+String(value||_('未设置'))),picker=sourcePickers[kind]={option:option,widget:widget,select:select,downloaded:downloaded,saved:saved,initial:value,presets:[]};select.value='custom';
    select.addEventListener('change',function(){picker.touched=true;picker.manual=select.value==='custom';select.setAttribute('data-selected',picker.manual?'custom':'preset');var preset=picker.presets.filter(function(p){return p.id===select.value;})[0];if(!preset)return;var element=typeof option.getUIElement==='function'?option.getUIElement(section):null;if(element&&typeof element.setValue==='function')element.setValue(preset.url);else widget.value=preset.url;if(typeof option.triggerValidation==='function')option.triggerValidation(section);});
    widget.addEventListener('input',function(){picker.touched=true;picker.manual=true;select.value='custom';select.setAttribute('data-selected','custom');});
    if(rulesCatalog)updateSourcePickers(rulesCatalog);
    return E('div',{class:'rp-rule-source-field'},[E('label',{class:'rp-cloud-field'},[E('span',{},_('可选来源')),select]),widget,saved,downloaded]);
   });};
  }
  // The top-level switch commits only its own power state, never unsaved form edits.
  o=pageOption('advanced',form.ListValue,'warp_protocol',_('主线路连接协议'),_('默认跟随现有客户端设置。选择 MASQUE / WireGuard 后仅管理本路由器客户端，应用时重连；不自动换出口、不改其他设备或缓冲。WireGuard 尚未在本机切换验收。'));
  o.value('auto',_('保持现状 / 跟随当前设置'));o.value('MASQUE','MASQUE');o.value('WireGuard','WireGuard');o.cfgvalue=function(){return uci.get('routepilot','main','warp_protocol')||'auto';};o.write=function(id,value){if(value==='auto'&&uci.get('routepilot',id,'warp_protocol')==null)return;uci.set('routepilot',id,'warp_protocol',value);};o.rmempty=false;
  o=pageOption('basic',form.ListValue,'scope',_('接管范围'));o.value('devices',_('按设备选择'));o.value('lan',_('所选局域网（无需绑定设备）'));o.default='devices';
  o=pageOption('basic',form.MultiValue,'lan_interfaces',_('接管的 LAN 接口（可多选）'),_('勾选设备所在的主网络、访客网络或 VLAN，可同时选择。Wi-Fi 与网线共用网桥时只勾选该网桥。每个接口需要一个独立 IPv4 私网网段；不会创建网络，也不会开放主网络与访客网络之间的互访。标记未启用的网络需先在路由器网络设置中开启，再启用本插件。'));o.rmempty=false;
  var interfaces=data[2]||[],currentLAN=uci.get('routepilot','main','lan_interfaces')||[uci.get('routepilot','main','lan_interface')||'br-lan'];if(typeof currentLAN==='string')currentLAN=currentLAN.split(/\s+/);o.cfgvalue=function(){return uci.get('routepilot','main','lan_interfaces')||currentLAN;};interfaces.forEach(function(i){o.value(i.name,i.name+' · '+i.network+(i.up?'':' · 尚未启用，请先在路由器启用'));});currentLAN.forEach(function(name){if(!interfaces.some(function(i){return i.name===name;}))o.value(name,name+' · 未检测到有效网段，请核对');});o.validate=function(id,v){return v&&v.length&&v.length<=8?true:_('请选择 1–8 个 LAN 接口');};
  o=pageOption('basic',form.ListValue,'lan_mode',_('局域网分流模式'));o.depends('scope','lan');[['bypass_cn',_('国内 IP 直连，其余代理')],['global',_('全部代理')],['gfw',_('仅代理名单内域名')],['whitelist',_('仅直连名单内域名')]].forEach(function(v){o.value(v[0],v[1]);});o.default='bypass_cn';
  o=pageOption('basic',form.Flag,'bypass_clients',_('Cloudflare 客户端连接直连'),_('避免电脑尚未关闭 Cloudflare One 时叠加隧道；关闭电脑客户端后，普通网页仍按本插件分流。'));o.depends('scope','lan');o.default='1';o.rmempty=false;
  o=pageOption('basic',form.ListValue,'ipv6_mode',_('IPv6 处理方式（所选网络 / 设备默认）'),_('阻断：禁止受管设备 IPv6 转发并过滤本插件 DNS 的 AAAA。允许直连：IPv6 不经过代理隧道，也不遵循本插件代理或域名规则，可能使用宽带真实出口；不创建 IPv6 网络，不覆盖其他防火墙。LAN 模式统一作用于所有勾选接口，忽略设备表覆盖；按设备模式可单独选择。未接管设备不受此选项影响。'));o.value('block',_('阻断 IPv6（默认）'));o.value('direct',_('允许 IPv6 直连（绕过代理规则）'));o.default='block';o.rmempty=false;
  exitOption=o=pageOption('basic',form.ListValue,'exit',_('主线路代理出口'),_('这是 WARP 主线路的出口；启用下方备用线路时暂不使用此出口。按设备分流不会改变主线路出口。列表来自当前账户，已列出不代表可用，读取不会切换。'));
  self.exitOptions(configuredExit(),{default:uci.get('routepilot','main','vnet_default'),seoul:uci.get('routepilot','main','vnet_seoul'),usa:uci.get('routepilot','main','vnet_usa')},catalog).forEach(function(item){o.value(item.value,item.label);});o.cfgvalue=function(){return configuredExit();};o.rmempty=false;o.validate=function(id,value){return value?true:_('请明确选择一个账户出口');};
  o=pageOption('basic',form.Button,'_discover',_('账户出口'));o.inputtitle=_('刷新账户出口（只读）');o.onclick=function(){return refreshDirectory();};
  o=pageOption('basic',form.DummyValue,'_exit_directory',_('目录读取与检测状态'));
  o.renderWidget=function(){return E('div',{},[directoryStatus,E('section',{},[E('h4',{},_('账户出口列表')),E('table',{class:'table'},[E('thead',{},E('tr',{},[E('th',{},_('账户可选网络')),E('th',{},_('最近连通检测（仅所测网站）'))])),directoryRows])])]);};
  o=pageOption('basic',form.DummyValue,'_device_mode',_('按设备的分流模式'));o.depends('scope','devices');o.cfgvalue=function(){return _('到“设备”页面，')+_('为每台设备分别选择分流模式：国内 IP 直连、全部代理、代理名单、直连名单或保持原样。不接管未启用的设备；局域网模式不覆盖设备表。');};
  o=pageOption('basic',form.DummyValue,'_info',_('使用说明')); o.rawhtml=false; o.cfgvalue=function(){return _('国内直连流量使用本地宽带 IP；只有代理流量使用所选出口。关闭后撤回本插件的接管规则；其他软件按其自身配置运行。');};
  o=pageOption('dns',form.Value,'dns_domestic',_('国内 DNS'),_('中国域名与自定义直连域名使用本地宽带解析。')); o.datatype='ip4addr'; o.default='223.5.5.5';
  o=pageOption('dns',form.Value,'dns_foreign',_('代理 DNS'),_('主线路的其他普通 DNS 经 WARP 隧道发送。应用自带的 DoH/DoT 不能按普通 DNS 域名规则分类。')); o.datatype='ip4addr'; o.default='1.1.1.1';
  o=pageOption('dns',form.ListValue,'dns_transport',_('上游 DNS 传输')); o.value('tcp','TCP');o.value('udp','UDP');o.default='tcp';
  o=pageOption('dns',form.Flag,'dns_cache',_('启用独立 DNS 缓存'));o.default='1';o.rmempty=false;
  o=pageOption('dns',form.DummyValue,'_ipv6_info',_('IPv6 与 AAAA'));o.cfgvalue=function(){return center?_('AAAA 过滤由“出口与分流”或“设备”页面中的 IPv6 处理方式决定。本插件只代理 IPv4；允许 IPv6 直连会绕过代理规则。'):_('AAAA 是否过滤由“分流设置 / 设备管理”的 IPv6 处理方式决定；本插件仅代理 IPv4，允许 IPv6 直连会绕过所有代理规则。');};
  [['custom_direct_domains',_('始终直连域名'),'hostname'],['custom_proxy_domains',_('始终代理域名'),'hostname'],['custom_direct_ips',_('始终直连 IPv4/CIDR'),'or(ip4addr,cidr4)'],['custom_proxy_ips',_('始终代理 IPv4/CIDR'),'or(ip4addr,cidr4)']].forEach(function(v){o=pageOption('rules-custom',form.DynamicList,v[0],v[1]);o.datatype=v[2];});
  [['china_url',_('中国 IP 规则')],['china_domains_url',_('中国域名规则')],['gfw_url',_('代理域名名单')],['whitelist_url',_('直连域名名单')]].forEach(function(v){o=pageOption('rules-sources',form.Value,v[0],v[1],_('可信 HTTPS 名单；支持普通域名、domain:、full:。regexp 条目跳过并记录，不执行脚本或编码订阅。'));o.validate=function(id,v){return !v || /^https:\/\/[^\s]+$/.test(v) ? true:_('必须使用 HTTPS');};sourcePresetField(o,v[0].replace('_url',''));});
  o=pageOption('rules-sources',form.Value,'update_hours',_('自动更新间隔（小时）'));o.datatype='range(1,720)';o.default='168';
  o=pageOption('advanced',form.Value,'organization',_('组织名称（只在首次登录时填写）'),_('仅首次登录时填写 Cloudflare Zero Trust 团队名称，不填邮箱或个人网站域名；查找方法见操作说明。'));o.datatype='hostname';organizationOption=o;
  o=pageOption('advanced',form.Button,'_prepare',_('启动独立登录环境'));o.inputtitle=_('准备登录环境（不接管设备）');o.onclick=function(){if(uci.get('routepilot','main','enabled')==='1'){notify(_('请先到“总览”关闭总开关；已有连接无需重新登录。'),true);return;}beginWizardStep('prepare');return save().then(function(result){if(!result||!result.nonce)throw Error(_('缺少本次保存确认，未启动登录环境'));wizardActions.prepare.nonce=result.nonce;return command('prepare');}).then(function(msg){uci.set('routepilot','main','preparing','1');notify(msg);}).catch(function(e){failWizardStep('prepare');notify(e.message,true);});};
  o=pageOption('advanced',form.Button,'_account_check',_('已有账户'));o.inputtitle=_('重新检查账户（只读）');o.onclick=function(){beginWizardStep('auth');return refreshDirectory().finally(function(){delete wizardActions.auth;updateWizard();});};
  o=pageOption('advanced',form.Button,'_login',_('验证账户'));o.inputtitle=_('邮箱登录');o.onclick=function(){
   if(uci.get('routepilot','main','enabled')==='1'){notify(_('请先到“总览”关闭总开关；已有连接无需重新登录。'),true);return;}
   function alreadyAccessible(){return catalog.available&&Date.now()-catalog.read_at<=60000;}
   function skipLogin(){openWizardStep('auth');notify(_('账户目录已可访问，无需重复邮箱注册。'));}
   if(alreadyAccessible()){skipLogin();return;}
   return save().then(function(){return fs.read('/var/run/routepilot/status.json').then(JSON.parse);}).then(function(st){
    if(!feedback.fresh(st,Date.now())||st.state!=='preparing')throw Error(_('登录环境尚未确认就绪，请先准备路由器登录环境并等待状态更新。'));
    if(alreadyAccessible()){skipLogin();return;}
    directoryGeneration++;beginWizardStep('auth');
    return command('login').then(function(url){
     var token=E('input',{type:'password',class:'cbi-input-text',autocomplete:'off',placeholder:'com.cloudflare.warp://…','aria-label':_('登录成功页的完整登录链接')}),registrationPending=false;
     var closeButton=E('button',{class:'btn',click:function(){if(registrationPending)return;delete wizardActions.auth;ui.hideModal();updateWizard();}},_('关闭'));
     var registerButton=E('button',{class:'btn',click:function(){
      if(registrationPending)return;
      beginWizardStep('auth');registrationPending=true;registerButton.disabled=true;closeButton.disabled=true;
      var loginPayload={token:token.value};token.value='';return submitInput('auth-input','register',loginPayload).then(function(msg){
       registeredHere=true;delete wizardActions.auth;ui.hideModal();notify(msg.message);return refreshDirectory(true);
      }).catch(function(e){failWizardStep('auth');notify(e.message,true);}).finally(function(){registrationPending=false;registerButton.disabled=false;closeButton.disabled=false;});
     }},_('注册此路由器'));
     showImokModal(_('登录独立路由器客户端'),[E('p',{},_('打开下方地址，用组织允许的邮箱完成验证。浏览器提示打开本机 Cloudflare 应用时取消；复制成功页的 com.cloudflare.warp:// 登录链接，粘贴到此处。')),E('a',{href:url.trim(),target:'_blank',rel:'noopener'},url.trim()),token,registerButton,closeButton]);
    }).catch(function(e){failWizardStep('auth');notify(e.message,true);});
   }).catch(function(e){notify(_('准备或状态读取未完成：')+e.message,true);});
  };
  s=m.section(form.GridSection,'policy','','');
  var policySection=s;
  s.addremove=true;s.anonymous=true;s.nodescriptions=true;
  s.addbtntitle=_('添加设备');
  s.modaltitle=function(id){return this.map.addedSection===id?_('添加设备策略'):_('编辑设备策略');};
  s.renderSectionPlaceholder=function(){return E('span',{class:'imok-device-empty'},_('还没有设备策略。点击「添加设备」开始设置。'));};
  o=s.option(form.Flag,'enabled',_('启用'));o.default='0';o.rmempty=false;
  o.textvalue=function(id){
   var enabled=this.cfgvalue(id)==='1';
   return E('span',{class:'imok-device-state','data-enabled':enabled?'true':'false'},enabled?_('已启用'):_('未启用'));
  };
  o=s.option(form.Value,'name',_('设备名称'));
  var devices={};
  o=s.option(form.ListValue,'_device',_('在线设备'),_('选择设备后自动填入 MAC 地址和当前 IPv4。列表中找不到设备时，可按路由器的 DHCP 租约手动填写。'));
  o.modalonly=true;o.optional=true;o.value('',_('请选择设备，或手动填写'));
  if(hints&&typeof hints==='object'&&!Array.isArray(hints))Object.keys(hints).sort().forEach(function(mac){
   var row=hints[mac];if(!/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac)||!row||typeof row!=='object')return;
   var values=row.ipaddrs||row.ipv4,ips=Array.isArray(values)?values:typeof values==='string'?[values]:[],ip=ips.filter(function(value){return typeof value==='string'&&/^\d{1,3}(?:\.\d{1,3}){3}$/.test(value)&&value.split('.').every(function(part){return Number(part)<=255;});})[0];
   if(ip){devices[mac]=ip;o.value(mac,(typeof row.name==='string'&&row.name?row.name:mac)+' · '+ip);}
  });
  o.parse=function(){return Promise.resolve();};
  o.cfgvalue=function(){return '';};
  o.onchange=function(ev,id,value){
   var address=devices[value];
   if(!address)return;
   // GridSection clones its options into a separate map for the edit dialog.
   var mac=this.section.getUIElement(id,'mac'),ip=this.section.getUIElement(id,'ip');
   if(!mac||!ip)return;
   mac.setValue(value);
   ip.setValue(address);
   mac.triggerValidation();
   ip.triggerValidation();
  };
  o=s.option(form.Value,'mac',_('MAC 地址'));o.datatype='macaddr';o.rmempty=false;
  o=s.option(form.Value,'ip',_('固定 IPv4'),_('填写设备在此局域网的 IPv4。可在路由器的 DHCP 设置中为该 MAC 保留地址，避免设备换 IP 后策略失效。'));o.datatype='ip4addr';o.rmempty=false;
  function deviceChoiceText(id){
   var value=this.cfgvalue(id)||this.default,index=this.keylist.indexOf(value);
   return index>=0?this.vallist[index]:_('未知选项');
  }
  o=s.option(form.ListValue,'mode',_('分流模式'));o.textvalue=deviceChoiceText;
  [['bypass_cn',_('国内 IP 直连，其余代理')],['global',_('全部代理')],['gfw',_('仅代理名单内域名')],['whitelist',_('仅直连名单内域名')],['direct',_('保持原样，不接管')]].forEach(function(v){o.value(v[0],v[1]);});o.default='bypass_cn';
  o=s.option(form.ListValue,'ipv6_mode',_('IPv6 处理方式'),_('仅按设备模式生效。允许直连会绕过代理隧道 和全部代理规则，可能使用宽带真实出口；不创建 IPv6。分流模式为“保持原样”时 IPv6 本就不受管理，此项不生效。'));o.value('inherit',_('跟随默认'));o.value('block',_('阻断 IPv6'));o.value('direct',_('允许 IPv6 直连'));o.default='inherit';o.rmempty=false;o.textvalue=deviceChoiceText;['global','bypass_cn','gfw','whitelist'].forEach(function(mode){o.depends('mode',mode);});o.retain=true;
  if(page==='account'){
   var originalSectionRender=mainSection.render.bind(mainSection);
   mainSection.render=function(){return originalSectionRender.apply(null,arguments).then(function(node){
    var steps=[];
    function para(text){return E('p',{},text);}
    function link(text,url){return E('a',{href:url,target:'_blank',rel:'noopener'},text);}
    function step(id,title,items,fields){
     var help=id==='intro',content=items;if(!help){var guidance=E('details',{class:'rp-setting-help'},[E('summary',{},_('操作说明')),E('div',{},items)]);content=[guidance];if(id==='prepare')content.unshift(para(_('只启动登录环境，不接管设备。')));if(id==='auth')content.unshift(para(_('仅注册路由器；提示打开本机 Cloudflare 应用时取消。登录链接是临时凭据，请勿分享。')));}
     var box=E('div',{class:'rp-guide-body'},content),badge=E('span',{class:'rp-step-badge'},help?_('说明'):_('○ 未执行'));
     (fields||[]).forEach(function(key){var field=node.querySelector('[id="cbi-routepilot-main-'+key+'"]');if(field)box.appendChild(field);});
     var heading=E(help?'summary':'h3',help?{}:{class:'rp-guide-heading'},[badge,E('span',{},title)]),card=E(help?'details':'section',{class:'rp-guide-step','data-step':id},[heading,box]);
     wizardCards[id]=card;wizardBadges[id]=badge;steps.push(card);
     return card;
    }
    step('intro',_('开始前：新路由器需要准备什么？'),[
     para(_('下载 ZIP 不会自动安装。完整安装包包含 IPK 插件、WARP 运行环境压缩包、安装脚本和校验文件。将 ZIP 解压后的整个文件夹复制到兼容路由器，在该文件夹内以管理员运行 sh install-router.sh；脚本会校验文件、解压运行环境并安装插件。仅在网页上传 IPK，只安装管理插件，不会自动安装配套环境。')),
     para(_('支持条件：目前只验证 GL-MT6000 / ARM64 OpenWrt 24.10，至少 500 MB 可用存储。不是在 Mac 或 Windows 上运行脚本。运行环境已存在时，安装脚本会停止以保护注册信息；更新已有插件只需安装新版 IPK，不要删除运行环境。安装结束默认关闭，之后再完成本向导登录。')),
     para(_('换路由器但继续用同一 Cloudflare 账户：组织名称、现有虚拟网络及 VPS 出口可以沿用，不用重新创建；新路由器必须独立登录。先保持总览里的总开关关闭。')),
     para(_('快捷迁移：在旧路由器“备份迁移”导出配置，到新路由器导入。导入后保持关闭；已导入组织名称时无需重复填写，仍需准备登录环境并注册新路由器。备份不包含登录凭据。')),
     E('a',{href:base+'/backup'},_('打开备份迁移 →'))
    ]);
    step('org',_('组织名称'),[
     para(_('到 Cloudflare 控制台进入 Zero Trust → Settings（设置），查看 Team name / Team domain（团队名称 / 团队域名）。也可查看已登录的 Cloudflare One 客户端账户页面。')),
     para(_('例如显示 myteam.cloudflareaccess.com，这里只填 myteam；不填 https://，不填 .cloudflareaccess.com 后缀，也不填邮箱、个人域名或 Cloudflare 账户 ID。')),
     link(_('查看官方团队名称说明'), 'https://developers.cloudflare.com/cloudflare-one/faq/getting-started-faq/'),
     para(_('如果是全新 Cloudflare 账户，需要先创建 Zero Trust 团队并允许你的邮箱注册设备。沿用现有账户则不要再创建。'))
    ],['organization']);
    wizardCards.org.querySelector('.rp-guide-body').appendChild(E('button',{class:'btn',click:function(){beginWizardStep('org');return save().catch(function(e){failWizardStep('org');notify(e.message,true);});}},_('保存组织名称')));
    step('prepare',_('准备路由器登录环境'),[
     para(_('点击下方按钮。它只启动独立登录环境，不接管局域网，不需要先打开总开关。')),
     para(_('成功标志：本页上方实际状态显示“登录环境已就绪”。首次启动可能稍慢；未就绪先不要点击邮箱登录。若提示缺少运行环境，请先按安装包说明安装配套运行环境。'))
    ],['_prepare']);
    step('auth',_('邮箱验证与路由器注册'),[
     para(_('点击“邮箱登录”，在弹窗打开团队登录地址。使用组织允许的邮箱收验证码，或按组织要求完成登录。')),
     para(_('若浏览器询问是否打开电脑上的 Cloudflare 应用，选择取消。你要注册的是新路由器，不是这台电脑。')),
     para(_('复制登录成功页面里的完整 com.cloudflare.warp://… 登录链接，粘贴回插件弹窗，点击“注册此路由器”。这是临时登录凭据，不要分享或发到 GitHub。')),
     para(_('成功标志：提示路由器已注册。若邮箱无权限，在 Cloudflare 的设备注册权限中允许该邮箱；链接失效则重新登录获取，不要反复粘贴旧链接。'))
    ],['_login','_account_check']);
    var wizardNext=E('div',{class:'cbi-section'},[E('h3',{},_('连接准备已完成')),para(_('账户目录已可访问。请到“出口与分流”页面明确选择并保存；DNS 与网页检查在“DNS”页面。')),E('a',{href:base+'/cloudflare/routing',class:'btn cbi-button-action'},_('继续设置分流 →'))]);
    wizardNext.hidden=true;
    var guide=E('div',{class:'rp-guide'},[
     para(_('✓ 已确认，✕ 失败，… 处理中，○ 待确认；所有操作均需手动执行。')),
     wizardSummary
    ].concat(steps,[wizardNext]));
    updateWizard=function(){
     var now=Date.now(),config=wizardConfig();
     
     ['org','prepare'].forEach(function(id){var a=wizardActions[id];if(a&&a.state==='busy'&&now-a.started>45000)wizardActions[id]={state:'error',started:now,baseline:a.baseline};});
     var prep=wizardActions.prepare;if(prep&&prep.state==='busy'&&prep.nonce&&feedback.fresh(wizardStatus,now)&&wizardStatus.state==='error'&&wizardStatus.attempted_nonce===prep.nonce){prep.state='error';}
     if(prep&&prep.state==='busy'&&prep.nonce&&feedback.fresh(wizardStatus,now)&&['preparing','running'].indexOf(wizardStatus.state)>=0&&wizardStatus.applied_nonce===prep.nonce){delete wizardActions.prepare;prep=null;}
     var states=feedback.wizardSteps(config,catalog,wizardStatus,wizardTest,wizardActions,now);
     if(prep&&prep.state==='busy'&&(!prep.nonce||!wizardStatus||wizardStatus.applied_nonce!==prep.nonce)){var ps=states.filter(function(x){return x.id==='prepare';})[0];ps.state='busy';}
     var descriptions={org:committedOrganization?_('组织名称已保存'):_('可沿用已可访问的账户，无需重填'),prepare:_('登录环境已就绪'),auth:_('账户目录可访问；无需重复注册'),dir:_('目录已读取（未验证联网）'),select:_('选择已保存（未验证运行）'),verify:_('当前连接所测网站通过')};
     var names={pending:'○ '+_('待确认'),busy:'… '+_('处理中'),success:'✓ '+_('已完成'),error:'✕ '+_('失败'),warning:'! '+_('访问受限'),stale:'○ '+_('未检测 / 已过期')};
     states.forEach(function(st){var a=wizardActions[st.id];if(st.state==='success'&&a&&a.state==='busy')delete wizardActions[st.id];var pendingText=st.reason==='access_previous'?_('○ 账户检查结果较旧，可重新检查'):st.reason==='directory_old'?_('○ 上次已读取，待刷新'):st.id==='select'&&st.reason==='saved'?_('○ 已保存，目录待确认'):names[st.state];wizardBadges[st.id].textContent=st.state==='success'?'✓ '+descriptions[st.id]:st.state==='pending'?pendingText:names[st.state];wizardCards[st.id].setAttribute('data-state',st.state);});
     if(!wizardInitialized){var first=states.filter(function(st){return st.state!=='success';})[0];if(first)openWizardStep(first.id);wizardNext.hidden=!states.every(function(st){return st.state==='success';});wizardInitialized=true;}
     var current=states.filter(function(st){return st.id===wizardActive;})[0];
     if(!wizardPinned&&current&&wizardAdvance===current.id&&current.state==='success'&&current.id!=='verify'){var index=states.indexOf(current),next=states.slice(index+1).filter(function(st){return st.state!=='success';})[0];if(next)openWizardStep(next.id);else{wizardActive=null;}wizardAdvance=null;}
     wizardNext.hidden=!states.every(function(st){return st.state==='success';});
     wizardSummary.textContent=_('已完成 ')+states.filter(function(st){return st.state==='success';}).length+'/3 · '+(registeredHere&&!catalog.available?_('已注册，账户目录暂未就绪，请稍后重新检查。'):_('已有账户可沿用；出口与范围在“分流设置”统一选择。'))+(catalog.last_success_at?_(' · 上次账户检查：')+new Date(catalog.last_success_at).toLocaleString():'');
    };
    updateWizard();
    node.appendChild(guide);return node;
   });};
  }
  mainSection.children=mainSection.children.filter(function(o){return center?o.routepilotPage===pageGroup:o.routepilotPage===page;});
  if(page==='devices'){var renderDevices=policySection.render.bind(policySection);policySection.render=function(){return renderDevices.apply(null,arguments).then(function(node){centerDeviceNode=node;return node;});};}
  m.children=page==='devices'?[policySection]:formPage?[mainSection]:[];
  var status=E('p',{role:'status','aria-live':'polite'},_('正在读取实际运行状态…'));
  var rulesProgress=E('p',{},_('正在读取规则状态…')),rulesTime=E('p'),rulesCounts=E('div'),lastRule=null;
  var testsInfo=E('p',{},_('路由器侧 HTTPS 检测：代理网站在隧道环境测试，国内网站在主机环境测试；不代表受管设备 DNS 或 LAN 端到端验收。'));
  var testCells={},testNames={google:'Google',youtube:'YouTube',github:'GitHub',taobao:'淘宝',baidu:'百度'};
  var testRows=Object.keys(testNames).map(function(k){var result=E('td',{},_('未检测')),delay=E('td',{},'—');testCells[k]={result:result,delay:delay};return E('tr',{},[E('td',{},testNames[k]),E('td',{},k==='taobao'||k==='baidu'?_('国内直连'):_('当前代理出口')),result,delay]);});
  var testButton=E('button',{class:'btn cbi-button-action',click:function(){if((center)&&(!feedback.fresh(wizardStatus,Date.now())||wizardStatus.state!=='running')){notify(_('请先在总览手动开启，并等待主线路已连接后再检测。'),true);return;}testButton.disabled=true;testsInfo.textContent=_('正在检测，最长约 10 秒…');Object.keys(testCells).forEach(function(k){testCells[k].result.textContent=_('检测中…');testCells[k].delay.textContent='—';});command('test').catch(function(e){testsInfo.textContent=e.message;testButton.disabled=false;});}},_('一键检测全部'));
  var switchEnabled=uci.get('routepilot','main','enabled')==='1',switchPending=null;
  var switchText=E('p',{},_('新安装默认关闭。开关立即生效，无需点击页面底部保存；不会提交尚未保存的其他设置。'));
  var switchFeedback=E('div',{hidden:true,class:'imok-switch-feedback',role:'status','aria-live':'polite'},switchText);
  var powerButton=E('button',{class:'btn cbi-button-action',click:function(){switchFeedback.hidden=false;var action=switchEnabled?'disable':'enable';powerButton.disabled=true;switchText.textContent=_('正在切换，等待后台确认…');command(action).then(function(msg){var result=JSON.parse(msg);switchEnabled=result.enabled;uci.set('routepilot','main','enabled',switchEnabled?'1':'0');uci.set('routepilot','main','preparing','0');switchPending={nonce:result.nonce,started:Date.now()};powerButton.textContent=switchEnabled?_('关闭 imOK'):_('开启 imOK');}).catch(function(e){switchText.textContent=e.message;powerButton.disabled=false;});}},switchEnabled?_('关闭 imOK'):_('开启 imOK'));
  var backupNode=null,backupTest=null,backupBusy=false,backupRequested=0,backupSwitch=null;
  var backupInfo=E('p',{role:'status'},_('正在读取备用状态…')),backupResult=E('div'),backupProgress=E('p',{class:'rp-help'});
  var backupSource=E('select',{'aria-label':_('备用节点来源')},[E('option',{value:'current'},_('保留已配置备用，只调整入口')),E('option',{value:'saved'},_('从原有节点读取（不修改原配置）')),E('option',{value:'uri'},_('粘贴 VLESS＋WS＋TLS 链接'))]);
  var backupSaved=E('select',{'aria-label':_('已有备用节点')},[E('option',{value:''},_('请选择节点，不会自动选择'))].concat(((data[5]&&data[5].nodes)||[]).map(function(n){return E('option',{value:n.id},n.name+' · '+n.address+':'+n.port);}))); 
  var backupURI=E('input',{type:'password',autocomplete:'off',placeholder:'vless://…','aria-label':_('私人备用节点链接')});
  var backupEntry=E('select',{'aria-label':_('备用入口方式')},[E('option',{value:'saved'},_('保留原有入口地址与端口')),E('option',{value:'domain'},_('直接连接节点域名 · 443')),E('option',{value:'ip'},_('指定优选 IPv4（保持域名证书验证）'))]);
  var backupIP=E('input',{type:'text',placeholder:_('公网 IPv4 地址'),'aria-label':_('优选入口 IPv4')});
  var backupURIBox=E('div',{hidden:true},[E('p',{class:'rp-help'},_('链接只保存在路由器私有文件，不进入普通配置导出。')),backupURI]);
  var backupSavedBox=E('div',{hidden:true},backupSaved),backupIPBox=E('div',{hidden:true},backupIP);
  backupSource.addEventListener('change',function(){backupURIBox.hidden=backupSource.value!=='uri';backupSavedBox.hidden=backupSource.value!=='saved';});
  backupEntry.addEventListener('change',function(){backupIPBox.hidden=backupEntry.value!=='ip';});
  var backupImport=E('button',{class:'btn',click:function(){
   workerActionStage('use');startWorkerOperation('use',_('保存备用节点'),'backup',null,backupTest);backupBusy=true;updateBackup(backupNode,backupTest,wizardStatus);var input={source:backupSource.value,entry:backupEntry.value};if(input.source==='uri')input.uri=backupURI.value;else if(input.source==='saved')input.id=backupSaved.value;if(input.entry==='ip')input.ip=backupIP.value;
   backupURI.value='';return submitInput('backup-input','backup-import',input).then(function(){backupInfo.textContent=_('备用节点已保存；尚未切换，下一步请独立检测。');backupTest=null;acceptWorkerOperation('use',{message:_('✓ 节点已保存，尚未启用。可在本组顶部“当前保存的备用节点”查看。')},false);return refreshWorkerMetadata();}).catch(function(e){failWorkerOperation('use',e.message);notify(e.message,true);}).finally(function(){backupBusy=false;updateWorkerProgress();});
  }},_('保存备用节点（不切换）'));
  var backupProbe=E('button',{class:'btn',click:function(){workerActionStage('use');startWorkerOperation('use',_('独立检测与测速'),'backup',null,backupTest);backupBusy=true;backupRequested=Date.now();updateBackup(backupNode,backupTest,wizardStatus);return command('backup-test').then(function(raw){acceptWorkerOperation('use',JSON.parse(raw),true);}).catch(function(e){failWorkerOperation('use',e.message);notify(e.message,true);backupRequested=0;}).finally(function(){backupBusy=false;updateWorkerProgress();});}},_('独立检测与测速（约 1 MiB）'));
  function switchBackup(action){startWorkerOperation('use',action==='backup-enable'?_('手动启用备用'):_('切回主线路'),'switch',null,wizardStatus);backupBusy=true;updateBackup(backupNode,backupTest,wizardStatus);return command(action).then(function(raw){var r=JSON.parse(raw);uci.set('routepilot','main','backend',r.backend);uci.set('routepilot','main','apply_nonce',r.nonce);backupSwitch={nonce:r.nonce,backend:r.backend,started:Date.now()};backupProgress.textContent=_('已提交手动切换，等待实际状态确认…');acceptWorkerOperation('use',{message:backupProgress.textContent},false);workerActions.use.waitingSwitch=true;}).catch(function(e){failWorkerOperation('use',e.message);notify(e.message,true);}).finally(function(){backupBusy=false;updateWorkerProgress();});}
  var backupEnable=E('button',{class:'btn cbi-button-action',click:function(){return switchBackup('backup-enable');}},_('手动启用备用'));
  var backupDisable=E('button',{class:'btn',click:function(){return switchBackup('backup-disable');}},_('切回主线路'));
  var workerBackupCard=E('section',{class:'cbi-section rp-backup-card'},[E('h3',{},_('备用节点与独立检测 / 手动使用')),E('p',{},_('保留原主线路，备用不会自动接管。不需要自有 VPS；优选 IP 只改变连接入口，最终出口可能变化。TLS 证书始终验证。')),E('div',{class:'rp-route-explainer'},[E('p',{},_('原主线路：路由器 → Cloudflare WARP → 账户所选出口 → 网站')),E('p',{},_('新备用：路由器 → Cloudflare 入口 → 自己的 Workers → 网站（部分目标使用公共转发）'))]),backupInfo,workerPanel('use'),
   E('section',{class:'rp-guide-step'},[E('h4',{class:'rp-guide-heading'},_('备用节点设置（手动导入 / 调整入口）')),E('div',{class:'rp-guide-body'},[backupSource,backupSavedBox,backupURIBox,E('p',{class:'rp-help'},_('优选只改变连接入口，不决定最终出口或国家。')),backupEntry,backupIPBox,E('p',{},backupImport)])]),
   E('p',{class:'rp-help'},_('备用仅代理 TCP；需代理的 UDP 流量会阻断，不会偷偷走宽带。国内直连及已有 IPv6 选择继续按原规则。备用的代理 DNS 经该线路使用 dns.google 加密解析，主线路 DNS 配置不改。')),
   E('div',{class:'rp-backup-actions'},[backupProbe,backupEnable,backupDisable]),backupProgress,backupResult,
   E('p',{class:'rp-help'},_('独立检测不接管设备，约使用 1 MiB 流量，结果在本次开机 5 分钟内有效。重启后需重新检测再启用备用。下载测速仅供参考，站点限流或下载失败不会单独判定整条线路不可用。备用节点凭据独立保存，不随普通配置备份导出；换路由器后需重新导入。'))]);
  function updateBackup(node,test,st){
   var initialNode=(data[5]&&data[5].node)||null;node=node||initialNode;if(node&&initialNode&&node.fingerprint===initialNode.fingerprint)node=Object.assign({},initialNode,node);backupNode=node;backupTest=test;var active=st&&st.backend==='worker'&&feedback.fresh(st,Date.now())&&st.state==='running',chosen=uci.get('routepilot','main','backend')==='worker';
   var proof=test&&test.usable===true&&test.state==='done'&&node&&test.fingerprint===node.fingerprint&&Date.now()/1000-test.finished_at>=0&&Date.now()/1000-test.finished_at<=300&&Array.isArray(test.results)&&test.results.length===5;
   var newJob=test&&Number(test.started_at)*1000>=backupRequested-1000,remoteTest=test&&['queued','running'].indexOf(test.state)>=0&&Date.now()/1000-Number(test.started_at)<100,busyTest=remoteTest||backupRequested&&(Date.now()-backupRequested<100000)&&(!newJob||(test.state==='running'||test.state==='queued'));
   if(backupRequested&&newJob&&test.state!=='running'&&test.state!=='queued')backupRequested=0;
   if(backupSwitch){if(st&&feedback.fresh(st,Date.now())&&st.applied_nonce===backupSwitch.nonce&&st.state==='running'&&st.backend===backupSwitch.backend){backupProgress.textContent=_('手动切换已生效。');acceptWorkerOperation('use',{message:backupProgress.textContent},false);workerActions.use.waitingSwitch=false;backupSwitch=null;}else if(st&&st.state==='error'&&st.attempted_nonce===backupSwitch.nonce){backupProgress.textContent=_('切换失败：')+st.message;failWorkerOperation('use',backupProgress.textContent);backupSwitch=null;}else if(Date.now()-backupSwitch.started>90000){backupProgress.textContent=_('尚未收到切换确认，请查看总览；不会把已提交当作已成功。');failWorkerOperation('use',backupProgress.textContent,'unknown');backupSwitch=null;}}
   backupInfo.textContent=(active?_('当前使用备用线路。'):st&&feedback.fresh(st,Date.now())&&st.state==='running'?_('当前使用主线路，备用未接管。'):st&&feedback.fresh(st,Date.now())&&st.state==='disabled'?_('本插件已关闭，未接管网络。'):chosen?_('已选择备用，实际运行状态尚未确认。'):_('当前线路状态尚未确认；保存的选择为主线路。'))+(node&&node.configured?_(' 已保存：')+node.name+' · '+node.address+':'+node.port+' · '+_('验证域名：')+node.sni:_(' 尚未配置备用节点。'));
   backupImport.disabled=!!(backupBusy||workerOperationBusy('use')||backupSwitch||chosen);backupProbe.disabled=!!(backupBusy||workerOperationBusy('use')||backupSwitch||busyTest||!node||!node.configured);backupEnable.disabled=!!(backupBusy||workerOperationBusy('use')||backupSwitch||busyTest||active||!proof||!st||!feedback.fresh(st,Date.now())||(st.state!=='running'&&!(st.state==='error'&&st.fallback==='direct')));backupDisable.disabled=!!(backupBusy||backupSwitch||!chosen);
   workerFlow.tested=!!proof;workerFlow.testing=!!busyTest;backupProbe.setAttribute('class',!proof?'btn cbi-button-action':'btn');backupEnable.setAttribute('class',proof?'btn cbi-button-action':'btn');
   backupResult.replaceChildren();if(busyTest){backupResult.appendChild(E('p',{},_('正在独立检测，最长约 90 秒；主线路未切换…')));}
   if(!test)return;
   var expired=!proof;backupResult.appendChild(E('p',{class:'rp-help'},_('备用检测时间：')+when(test.finished_at)+(busyTest?_(' · 检测进行中，已完成 ')+(test.results||[]).length+'/5':expired?_(' · 未通过、已过期或节点已改变，请重新检测'):(test.results||[]).some(function(r){return r.state!=='ok';})?_(' · 基础连通可用，但部分项目未通过，请查看具体结果'):_(' · 本次检测全部通过，可手动启用'))+(test.entry_ip?_(' · 本次入口 IP：')+test.entry_ip:'')+(test.exit_ip?_(' · 本次观测出口：')+test.exit_ip+_('（可能随连接变化）'):'')));
   var names={trace:_('出口观测'),google:'Google',github:'GitHub',dns:_('备用加密 DNS'),download:_('1 MiB 下载')};(test.results||[]).forEach(function(r){backupResult.appendChild(E('p',{class:'rp-help'},names[r.id]+'：'+(r.state==='ok'?'✓ '+_('通过'):r.state==='restricted'?'! '+_('站点返回 HTTP ')+r.http+' · '+_('访问受限 / 限流'):'✕ '+_('未通过'))+(r.message?' · '+r.message:'')+(r.attempt_count>1?' · '+_('尝试次数：')+r.attempt_count:'')+(r.ms!=null?' · '+r.ms+' ms':'')+(r.id==='download'&&r.state==='ok'&&r.ms>0?' · '+(r.bytes*8/r.ms/1000).toFixed(2)+' Mbps（'+_('短测，非带宽上限')+'）':'')));});
  }
  var relayMeta=(data[5]&&data[5].relays)||{},savedRelayRows=relayMeta.candidates||[{address:'proxyip.cmliussss.net:443',source:'cmliu 上游文档示例；未作维护方背书',source_url:'https://github.com/cmliu/edgetunnel',enabled:true}],relaySavedHash=relayMeta.order_hash||null;
  var relayModels=[],relayDirty=false,relayBusy=false,relayTest=null,relayObserved=null;
  var relayInfo=E('p',{role:'status','aria-live':'polite'},_('候选配置与检测结果正在读取…')),relayRows=E('div'),relayRuntime=E('div');
  function markRelayDirty(){relayDirty=true;relayInfo.textContent=_('有尚未保存的候选修改；保存后才能逐项检测或应用。');refreshRelayButtons();}
  function relayModel(row){
   var m={address:E('input',{type:'text',value:row.address||'',placeholder:'proxy.example.com:443','aria-label':_('PROXYIP 地址')}),source:E('input',{type:'text',value:row.source||'用户提供','aria-label':_('PROXYIP 来源说明')}),link:E('input',{type:'url',value:row.source_url||'',placeholder:'https://…','aria-label':_('PROXYIP 公开来源链接')}),enabled:E('input',{type:'checkbox','aria-label':_('启用此候选参与主备')})};
   m.enabled.checked=row.enabled!==false;
   [m.address,m.source,m.link].forEach(function(input){input.addEventListener('input',markRelayDirty);});m.enabled.addEventListener('change',function(){markRelayDirty();renderRelayRows();});
   return m;
  }
  function relayValues(){return relayModels.map(function(m){return {address:String(m.address.value||'').trim(),source:String(m.source.value||'').trim(),source_url:String(m.link.value||'').trim(),enabled:!!m.enabled.checked};});}
  function relayRowProof(address){return relayTest&&Array.isArray(relayTest.results)?relayTest.results.filter(function(r){return r.address===address.toLowerCase();})[0]:null;}
  function relayCurrent(row){var node=backupNode||(data[5]&&data[5].node);return row&&row.profile==='cf-relay-v1'&&node&&row.fingerprint===node.relay_fingerprint&&Date.now()/1000-Number(row.finished_at)>=-5&&Date.now()/1000-Number(row.finished_at)<=300;}
  function relayTaskRunning(){return relayTest&&['queued','running'].indexOf(relayTest.state)>=0&&Date.now()/1000-Number(relayTest.started_at)<260;}
  function refreshRelayButtons(){
   var running=relayBusy||workerOperationBusy('relay')||relayTaskRunning(),chosen=uci.get('routepilot','main','backend')==='worker';
   relaySave.disabled=!!(running||chosen);relayAdd.disabled=!!(running||relayModels.length>=6);relayProbe.disabled=!!(running||relayDirty||!backupNode);relayStatus.disabled=!!running;
   var enabled=relayValues().filter(function(r){return r.enabled;});
   relayApply.disabled=!!(running||relayDirty||chosen||!relayTest||relayTest.state!=='done'||!enabled.length||enabled.some(function(r){var proof=relayRowProof(r.address);return !relayCurrent(proof)||proof.state!=='ok'||proof.forced!==true;}));
   var primary=relayDirty?relaySave:relayApply.disabled?relayProbe:relayApply;[relaySave,relayProbe,relayApply].forEach(function(button){button.setAttribute('class',button===primary?'btn cbi-button-action':'btn');});
  }
  function renderRelayRows(){
   relayRows.replaceChildren();var enabledRank=0;
   relayModels.forEach(function(m,index){
    var rank=m.enabled.checked?++enabledRank:0,proof=relayRowProof(String(m.address.value||'').trim()),fresh=relayCurrent(proof),info=E('p',{class:'rp-help'});
    info.textContent=!proof?_('尚未强制检测此候选'):!fresh?_('历史检测已过期或节点改变，请重新检测'):proof.state==='ok'?_('✓ 强制转发两个 Cloudflare 目标通过'):_('✕ 未通过，不会用直连或其他候选掩盖');
    if(proof){info.textContent+=' · '+_('检测时间：')+when(proof.finished_at);(proof.checks||[]).forEach(function(check,n){info.textContent+=' · '+(n===0?'Cloudflare 官网':'Cloudflare 测速服务')+': HTTP '+check.http+' / '+check.ms+' ms'+(check.curl_code?' / curl '+check.curl_code:'');});if(proof.download)info.textContent+=' · '+(proof.download.ok?_('64 KiB 下载完整'):proof.download.http===429?_('测速站点限流'):_('下载未通过'));}
    function move(delta){var next=index+delta;if(next<0||next>=relayModels.length)return;relayModels.splice(index,1);relayModels.splice(next,0,m);markRelayDirty();renderRelayRows();}
    var up=E('button',{class:'btn',disabled:index===0,click:function(){move(-1);}},_('上移')),down=E('button',{class:'btn',disabled:index===relayModels.length-1,click:function(){move(1);}},_('下移'));
    var remove=E('button',{class:'btn',disabled:relayModels.length===1,click:function(){relayModels.splice(index,1);markRelayDirty();renderRelayRows();}},_('移除'));
    up.disabled=index===0;down.disabled=index===relayModels.length-1;remove.disabled=relayModels.length===1;
    var sourceLink=String(m.link.value||'').trim(),sourceNote=E('p',{class:'rp-help'},_('来源为填写者声明，检测通过不代表维护方背书或长期可靠。'));
    if(/^https:\/\//i.test(sourceLink))sourceNote.appendChild(E('a',{href:sourceLink,target:'_blank',rel:'noopener noreferrer'},_(' 查看公开来源 ↗')));
    relayRows.appendChild(E('section',{class:'rp-relay-row'},[E('h4',{},rank===1?_('主用'):rank>1?_('备用 ')+(rank-1):_('未启用')),
     E('label',{class:'rp-inline-check'},[m.enabled,_('启用，按此顺序参与主备')]),
     E('div',{class:'rp-cloud-grid'},[cloudField(_('转发主机:端口'),m.address),cloudField(_('来源说明'),m.source),cloudField(_('公开来源链接（可选）'),m.link,_('只填写公开资料链接，不填私人订阅、账户或口令。'))]),sourceNote,info,E('div',{class:'rp-backup-actions'},[up,down,remove]) ]));
   });refreshRelayButtons();
  }
  function saveRelayRows(){var rows=relayValues();return submitInput('relay-input','relay-save',{candidates:rows}).then(function(result){savedRelayRows=result.candidates;relaySavedHash=result.order_hash;relayDirty=false;cloudRelay.value=savedRelayRows.filter(function(r){return r.enabled;}).map(function(r){return r.address;}).join(',');relayInfo.textContent=result.message;return result;});}
  function relayAction(action){workerActionStage('relay');startWorkerOperation('relay',({save:_('保存候选与顺序'),test:_('逐项强制检测 PROXYIP'),apply:_('应用已通过的主备顺序'),status:_('读取实例切换记录')})[action],'relay',null,relayTest);relayBusy=true;refreshRelayButtons();return (action==='save'?saveRelayRows():command('relay-'+action)).then(function(raw){var r=typeof raw==='string'?JSON.parse(raw):raw;acceptWorkerOperation('relay',r,action==='test');if(action==='apply')return refreshWorkerMetadata();}).catch(function(e){failWorkerOperation('relay',e.message);notify(e.message,true);}).finally(function(){relayBusy=false;refreshRelayButtons();updateWorkerProgress();});}
  var relayAdd=E('button',{class:'btn',click:function(){relayModels.push(relayModel({enabled:true}));markRelayDirty();renderRelayRows();}},_('添加候选'));
  var relaySave=E('button',{class:'btn',click:function(){return relayAction('save');}},_('保存候选与顺序（不切换）'));
  var relayProbe=E('button',{class:'btn',click:function(){return relayAction('test');}},_('逐项强制检测 PROXYIP'));
  var relayApply=E('button',{class:'btn',click:function(){return relayAction('apply');}},_('应用已通过的主备顺序（不启用）'));
  var relayStatus=E('button',{class:'btn',click:function(){return relayAction('status');}},_('查询 Worker 实例切换记录'));
  var relayCard=E('section',{class:'cbi-section rp-backup-card'},[E('h3',{},_('公共转发 PROXYIP · 主用与备用')),
   E('p',{},_('最多 6 项。启用项从上到下依次为主用、备用 1、备用 2。每项强制转发两个 Cloudflare 目标并验证 HTTPS，不允许直连或其他候选代替通过。Google / GitHub 由下方整条备用线路独立检测。保存后再检测，所有启用项通过后才能应用顺序。')),relayInfo,workerPanel('relay'),relayRows,
   E('div',{class:'rp-backup-actions'},[relayAdd,relaySave,relayProbe,relayApply,relayStatus]),relayRuntime,
   E('p',{class:'rp-help'},_('应用只修改 Workers 备用节点的连接参数，不启用备用、不改原主线路与分流。连接失败时按顺序尝试后续候选，失败项在当前 Worker 实例暂避 30 秒；所有候选都暂避时仍会尝试。已建立会话中断需重新连接，不能保证无缝续传。检测 5 分钟有效，域名背后的实际服务器也可能变化。公共 PROXYIP 不是通用 VPS 出口；普通网站通常由 Worker 直接连接，此候选主要负责需要公共转发的目标。'))]);
  relayModels=savedRelayRows.map(relayModel);
  function updateRelays(test,observed){
   relayTest=test;relayObserved=observed;
   if(!relayDirty&&!relayBusy){var node=backupNode||(data[5]&&data[5].node),applied=node&&relaySavedHash&&node.relay_order_hash===relaySavedHash;
    relayInfo.textContent=relayTaskRunning()?_('正在强制逐项检测，已完成 ')+(test.results||[]).length+'/'+(test.total||savedRelayRows.length):test&&test.state==='failed'?test.message:applied?_('已在当前备用节点固定此主备顺序；主线路未改变。'):_('候选已列出；尚未在当前备用节点应用此顺序。');}
   if(!relayDirty&&!(relayRows.contains&&relayRows.contains(document.activeElement)))renderRelayRows();else refreshRelayButtons();relayRuntime.replaceChildren();
   if(observed){relayRuntime.appendChild(E('p',{class:'rp-help'},(Date.now()/1000-observed.time>60?_('历史：'):'')+(observed.message||_('未取得实例状态说明'))+' · '+when(observed.time)));Object.keys(observed.observed||{}).forEach(function(key){var h=observed.observed[key];relayRuntime.appendChild(E('p',{class:'rp-help'},key+' · '+_('连接成功 ')+(h.successes||0)+' / '+_('失败 ')+(h.failures||0)+' · '+({connected:_('最近连接成功'),connect_failed:_('最近连接失败'),stream_failed:_('传输中断')}[h.last]||_('未知'))));});}
  }

  var cloudMeta=(data[5]&&data[5].cloud)||{},cloudBusy=false,cloudState=null,cloudDirectory=cloudMeta.directory||null,cloudDirectoryAuto=false,cloudZones=[],cloudZonesFor='',cloudAccountKey='',cloudZoneKey='',scanState=null,scanBusy=false;
  var cloudInfo=E('p',{role:'status','aria-live':'polite'}),cloudSummary=E('p',{class:'rp-help'}),cloudResult=workerPanel('deploy');
  function cloudField(label,control,help){return E('label',{class:'rp-cloud-field'},[E('span',{},label),control,help?E('small',{class:'rp-help'},help):null].filter(function(item){return item!=null;}));}
  var cloudToken=E('input',{type:'password',autocomplete:'new-password','aria-label':_('Cloudflare API 令牌'),placeholder:cloudMeta.token_saved?_('已保存；留空保留原令牌'):_('仅填写受限 API 令牌，不使用 Global API Key')});
  var cloudAccount=E('input',{type:'text',value:cloudMeta.account_id||'','aria-label':_('Cloudflare 账户 ID'),placeholder:_('32 位账户 ID')});
  var cloudAccountChoice=E('select',{'aria-label':_('选择已授权的 Cloudflare 账户')},[E('option',{value:cloudMeta.account_id||''},cloudMeta.account_id?_('已保存账户 · ')+cloudMeta.account_id.slice(0,8):_('先读取已授权账户，或手工填写账户 ID'))]);
  var cloudName=E('input',{type:'text',value:cloudMeta.script||'routepilot-backup','aria-label':_('Worker 名称')});
  var cloudHost=E('input',{type:'text',readonly:true,value:cloudMeta.hostname||'','aria-label':_('绑定的子域名'),placeholder:_('选择根域名后自动组成')}),cloudPrefix=E('input',{type:'text',value:cloudMeta.hostname?cloudMeta.hostname.split('.')[0]:'backup','aria-label':_('子域名前缀'),placeholder:'backup'}),prefixInitialized=!cloudMeta.hostname;
  var cloudZone=E('select',{'aria-label':_('选择账户下可用的根域名')},[E('option',{value:cloudMeta.zone_id||''},cloudMeta.zone_id?_('已保存区域 · ')+cloudMeta.zone_id.slice(0,8):_('请先读取账户下可用域名'))]);
  var cloudRelay=E('input',{type:'text',value:savedRelayRows.filter(function(r){return r.enabled;}).map(function(r){return r.address;}).join(','),'aria-label':_('公共转发 PROXYIP')});
  function saveCloud(){
   var input={token:cloudToken.value,account_id:cloudAccount.value.trim(),script:cloudName.value.trim(),hostname:cloudHost.value.trim(),zone_id:cloudZone.value,proxyip:savedRelayRows.filter(function(r){return r.enabled;}).map(function(r){return r.address;}).join(',')};
   cloudToken.value='';return submitInput('cloud-input','cloud-save',input);
  }
  function cloudAction(action,saveFirst){
   var title={verify:saveFirst?_('保存授权并读取账户 / 域名'):_('只读刷新账户 / 根域名'),target:_('检查名称与域名占用'),deploy:_('创建 Workers 与绑定域名'),check:_('只读核验部署与证书'),promote:_('将部署保存为备用节点'),forget:_('清除本机 API 授权')}[action]||action;
   if(action!=='verify')workerActionStage('deploy');
   var async=['verify','target','deploy','check'].indexOf(action)>=0;startWorkerOperation('deploy',title,action==='verify'?'directory':'cloud',async?action:null,action==='verify'?cloudDirectoryJob:cloudState);
   cloudBusy=true;updateCloud(cloudState);
   return Promise.resolve().then(function(){return saveFirst?saveCloud():null;}).then(function(){return command('cloud-'+action);}).then(function(raw){var result=JSON.parse(raw);acceptWorkerOperation('deploy',result,async);if(!async&&result.saved)return refreshWorkerMetadata();}).catch(function(e){failWorkerOperation('deploy',e.message);notify(e.message,true);}).finally(function(){cloudBusy=false;updateCloud(cloudState);updateWorkerProgress();});
  }
  var cloudSave=E('button',{class:'btn',click:function(){return cloudAction('verify',true);}},_('保存令牌并读取账户 / 域名'));
  var cloudTarget=E('button',{class:'btn',click:function(){return cloudAction('target',true);}},_('检查名称和子域名是否占用'));
  var cloudDeploy=E('button',{class:'btn cbi-button-action',click:function(){return cloudAction('deploy',true);}},_('一键创建 Workers 并绑定域名'));
  var cloudCheck=E('button',{class:'btn',click:function(){return cloudAction('check',false);}},_('检查部署与证书状态'));
  var cloudPromote=E('button',{class:'btn',click:function(){return cloudAction('promote',false);}},_('将新部署设为备用节点（不启用）'));
  var cloudForget=E('button',{class:'btn',click:function(){return cloudAction('forget',false);}},_('清除本机 API 令牌'));
  var tokenGuide=E('details',{class:'rp-guide-step'},[E('summary',{},_('没有 API 令牌？按这里创建（新手指引）')),E('div',{class:'rp-guide-body'},[
   E('ol',{},[E('li',{},[E('a',{href:'https://dash.cloudflare.com/profile/api-tokens',target:'_blank',rel:'noopener noreferrer'},_('打开 Cloudflare → 我的个人资料 → API 令牌')),_('，选择创建令牌 → 创建自定义令牌。')]),
    E('li',{},_('权限添加：账户 → Workers Scripts → 编辑；区域 → Zone → 读取；区域 → DNS → 读取；区域 → Workers Routes → 编辑。不要使用 Global API Key。')),
    E('li',{},_('账户资源仅选择自己的目标账户；区域资源仅选择要绑定域名所属的区域。绑定使用 Workers Custom Domains 接口，由 Cloudflare 创建记录和证书；本插件不直接改写其他 DNS 记录。')),
    E('li',{},_('设置合适的到期时间。创建后复制令牌，仅粘贴到本页私密输入框，不发聊天、不放普通备份。')),
    E('li',{},_('无需先复制账户 ID：留空后读取账户。有需要时也可在高级项手工填写 32 位账户 ID。子域名请用尚未使用的名称，例如 backup.你的域名。点击保存并读取可用域名，选择根域名，再填一个未使用的子域名前缀并部署。'))]),
   E('p',{class:'rp-help'},_('令牌保存在路由器私有文件，页面不会回显。清除本机令牌不会撤销 Cloudflare 令牌；需要撤销时回到 Cloudflare API 令牌页面。不会自动订购或升级付费计划。'))])]);
  var cloudDirectoryInfo=E('p',{class:'rp-help',role:'status','aria-live':'polite'});
  var cloudDirectoryRefresh=E('button',{class:'btn',click:function(){cloudDirectoryAuto=true;return cloudAction('verify',false);}},_('刷新账户与根域名（只读）'));
  function updateDirectory(data){
   if(data&&data.state&&data.started_at)cloudDirectoryJob=data;
   if(data&&Array.isArray(data.accounts)&&(!cloudDirectory||Number(data.read_at||0)>=Number(cloudDirectory.read_at||0))){cloudDirectory=data;}
   var source=cloudDirectory;
   if(cloudToken.value){cloudDirectoryInfo.textContent=_('令牌尚未保存；保存后才能读取它授权的账户和域名。');cloudDirectoryRefresh.disabled=true;return;}
   var hasToken=cloudState&&cloudState.configured===false?false:cloudMeta.token_saved||cloudState&&cloudState.token_saved;
   cloudDirectoryRefresh.disabled=!!(cloudBusy||!hasToken||data&&['queued','running'].indexOf(data.state)>=0&&Date.now()/1000-Number(data.started_at)<300);
   if(!source){cloudDirectoryInfo.textContent=data&&data.state==='failed'?data.message||_('域名读取失败，请重试；不能据此认定账户没有域名。'):_('尚未读取可用根域名；已保存绑定保持不变。');return;}
   var account=cloudAccount.value||source.discovered_account_id||'',accounts=source.accounts||[];
   var accountKey=JSON.stringify(accounts)+'|'+account;
   if(accountKey!==cloudAccountKey){cloudAccountKey=accountKey;cloudAccountChoice.replaceChildren(E('option',{value:''},_('请选择已授权账户')));accounts.forEach(function(a){cloudAccountChoice.appendChild(E('option',{value:a.id},a.name+' · '+a.id.slice(0,8)));});if(account&&!accounts.some(function(a){return a.id===account;}))cloudAccountChoice.appendChild(E('option',{value:account},_('保留原账户（本次目录未确认）')));}
   cloudAccountChoice.value=account;if(!cloudAccount.value&&account)cloudAccount.value=account;
   var zones=Array.isArray(source.all_zones)?source.all_zones.filter(function(z){return z.account_id===account;}):source.discovered_account_id===account||source.account_id===account?source.zones||[]:[];
   cloudZones=zones;cloudZonesFor=account;
   var zoneKey=account+JSON.stringify(zones);
   if(zoneKey!==cloudZoneKey){cloudZoneKey=zoneKey;var value=cloudZone.value||((cloudMeta.account_id||'')===account?cloudMeta.zone_id:'')||'';cloudZone.replaceChildren(E('option',{value:''},_('请选择根域名')));zones.forEach(function(z){cloudZone.appendChild(E('option',{value:z.id},z.name));});if(!zones.some(function(z){return z.id===value;}))value='';if(!value&&zones.length===1)value=zones[0].id;cloudZone.value=value;var chosen=zones.filter(function(z){return z.id===value;})[0];if(!prefixInitialized&&chosen&&cloudMeta.hostname&&cloudMeta.hostname.endsWith('.'+chosen.name)){cloudPrefix.value=cloudMeta.hostname.slice(0,-chosen.name.length-1);prefixInitialized=true;}if(chosen)syncCloudHostname();}
   cloudDirectoryInfo.textContent=(data&&['queued','running'].indexOf(data.state)>=0?_('正在只读刷新；已保存绑定不变。 '):'')+_('当前账户可用根域名：')+zones.length+_(' 个；仅含此令牌授权范围。')+(source.read_at?_(' 读取时间：')+when(source.read_at):'')+(Date.now()/1000-Number(source.read_at)>900?_(' · 历史目录，请只读刷新'):'')+(data&&data.state==='failed'?' · '+(data.message||_('本次刷新失败'))+_('；保留上次目录'):'');
  }
  var cloudCard=E('section',{class:'cbi-section rp-backup-card'},[E('h3',{},_('创建自己的 Workers 备用线路')),E('p',{},_('已有备用可以继续使用；下面用于另外创建可由路由器管理的 Worker 和子域名。创建和绑定不切换主线路。')),cloudInfo,cloudSummary,cloudResult,E('div',{class:'rp-directory-toolbar'},[cloudDirectoryRefresh,cloudDirectoryInfo]),tokenGuide,
   E('div',{class:'rp-cloud-grid'},[cloudField(_('私密 API 令牌'),cloudToken,_('首次填写；已保存时留空即可保留。')),cloudField(_('Cloudflare 账户'),cloudAccountChoice,_('选择「账户与协议」中团队所属的账户。首次绑定需允许读取该账户的 Zero Trust 组织。')),cloudField(_('账户 ID（可选，手工填写）'),cloudAccount),cloudField(_('Worker 名称'),cloudName,_('以 routepilot- 开头；不会覆盖其他项目。')),cloudField(_('可用根域名'),cloudZone,_('显示此令牌可读取且已生效的根域名；绑定权限在占用检查和部署时另行确认。')),cloudField(_('子域名前缀'),cloudPrefix,_('例如 backup，不填写 https:// 或根域名。')),cloudField(_('最终绑定的子域名'),cloudHost,_('系统会检查占用；不用先到 Cloudflare 添加这条 DNS。'))]),
   E('p',{class:'rp-help'},_('公共转发 PROXYIP 在下面的候选池管理；部署采用已保存的启用顺序。修改候选不会立即改动云端或当前节点。')),
   E('div',{class:'rp-backup-actions'},[cloudSave,cloudTarget,cloudDeploy,cloudCheck,cloudPromote]),E('details',{class:'rp-danger-area'},[E('summary',{},_('危险操作：清除本机授权')),E('p',{},_('仅删除路由器保存的 API 令牌和域名目录；不会删除云端资源、撤销 Cloudflare 令牌或切换当前线路。')),cloudForget])]);
  function updateCloud(state){
   cloudState=state;if(state&&Array.isArray(state.projects)&&Number(state.projects_read_at||0)>=workerProjectsAt){workerProjectRows=state.projects;workerProjectsAt=Number(state.projects_read_at||0);}var busy=cloudBusy||workerOperationBusy('deploy')||(state&&['running','queued'].indexOf(state.state)>=0&&Date.now()/1000-Number(state.started_at)<300),configured=!!(cloudMeta.configured||(state&&state.configured));
   if(state&&state.configured===false){configured=false;cloudMeta={configured:false};cloudDirectory=null;cloudZones=[];cloudZonesFor='';cloudZoneKey='';cloudDirectoryInfo.textContent=_('本机 API 令牌已清除，需重新授权后读取域名。');}
   cloudInfo.textContent=state&&state.message?state.message:configured?_('已保存 Cloudflare 配置，云端部署状态尚未检查。'):_('尚未保存 API 令牌；可先展开下方创建指引。');
   var saved=(state&&state.configured)?state:cloudMeta,dirty=!!cloudToken.value||['account_id','script','hostname','zone_id'].some(function(k){var controls={account_id:cloudAccount,script:cloudName,hostname:cloudHost,zone_id:cloudZone,proxyip:cloudRelay};return String(controls[k].value||'').trim()!==String(saved[k]||'');});cloudSummary.textContent=(saved.hostname?_('当前保存目标：')+saved.script+' · '+saved.hostname+'。 ':'')+_('改动后先保存并读取区域 / 部署；HTTPS 就绪后仍需独立联网检测。')+(dirty?_(' 表单有尚未保存的改动。'):'');
   updateDirectory(state&&(Array.isArray(state.accounts)||state.directory)?state.directory||state:null);
   dirty=!!cloudToken.value||['account_id','script','hostname','zone_id'].some(function(k){var controls={account_id:cloudAccount,script:cloudName,hostname:cloudHost,zone_id:cloudZone,proxyip:cloudRelay};return String(controls[k].value||'').trim()!==String(saved[k]||'');});
   workerFlow.cloudDirty=dirty;workerFlow.deployed=!!(state&&state.state==='ready'&&!dirty);
   var alreadySaved=backupNode&&backupNode.configured&&workerProjectRows.some(function(row){return row.saved_as_backup&&row.script===cloudName.value.trim()&&row.hostname===cloudHost.value.trim();});if(alreadySaved)cloudSummary.textContent+=_(' 当前部署已保存为备用，无需重复保存；可继续配置或检测 PROXYIP。');
   var primary=!configured||cloudToken.value?cloudSave:alreadySaved?cloudCheck:workerFlow.deployed?cloudPromote:cloudDeploy;[cloudSave,cloudDeploy,cloudPromote,cloudCheck].forEach(function(button){button.setAttribute('class',button===primary?'btn cbi-button-action':'btn');});
   cloudSave.disabled=!!busy;var validTarget=cloudZonesFor===cloudAccount.value.trim().toLowerCase()&&!cloudToken.value&&cloudZones.some(function(z){return z.id===cloudZone.value;})&&!!cloudHost.value;cloudDeploy.disabled=!!(busy||!validTarget||workerActions.deploy&&workerActions.deploy.result&&workerActions.deploy.result.state==='unknown');cloudTarget.disabled=!!(busy||!validTarget);cloudCheck.disabled=!!(busy||!configured||dirty);cloudForget.disabled=!!(busy||!configured);cloudPromote.disabled=!!(alreadySaved||busy||dirty||!state||state.state!=='ready'||uci.get('routepilot','main','backend')==='worker');
  }
  function syncCloudHostname(){var zone=cloudZones.filter(function(z){return z.id===cloudZone.value;})[0];var prefix=String(cloudPrefix.value||'').trim().toLowerCase();cloudHost.value=zone&&/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(prefix)?prefix+'.'+zone.name:'';}
  cloudPrefix.addEventListener('input',function(){prefixInitialized=true;syncCloudHostname();updateCloud(cloudState);});
  cloudAccountChoice.addEventListener('change',function(){cloudAccount.value=cloudAccountChoice.value;cloudZones=[];cloudZonesFor='';cloudZoneKey='';cloudZone.replaceChildren(E('option',{value:''},_('账户已变更，请重新读取域名')));cloudZone.value='';cloudHost.value='';updateDirectory(cloudDirectory);updateCloud(cloudState);});
  cloudToken.addEventListener('input',function(){cloudZones=[];cloudZonesFor='';cloudZoneKey='';if(!cloudToken.value)updateDirectory(cloudMeta.directory);else updateDirectory(null);updateCloud(cloudState);});cloudAccount.addEventListener('input',function(){cloudZoneKey='';cloudZone.value='';updateDirectory(cloudDirectory);updateCloud(cloudState);});
  [cloudName,cloudHost,cloudRelay].forEach(function(field){field.addEventListener('input',function(){updateCloud(cloudState);});});cloudZone.addEventListener('change',function(){syncCloudHostname();updateCloud(cloudState);});updateCloud(null);updateDirectory(cloudDirectory);
  var scanInfo=E('p',{role:'status','aria-live':'polite'},_('尚未优选；原备用入口保持不变。')),scanRows=E('div');
  function scanAction(action){workerActionStage('scan');startWorkerOperation('scan',action==='backup-scan'?_('优选 Cloudflare 入口'):_('保存推荐备用入口'),'scan',null,scanState);scanBusy=true;updateScan(scanState);return command(action).then(function(raw){var r=JSON.parse(raw);acceptWorkerOperation('scan',r,action==='backup-scan');if(action==='backup-scan-apply')return refreshWorkerMetadata();}).catch(function(e){failWorkerOperation('scan',e.message);notify(e.message,true);}).finally(function(){scanBusy=false;updateWorkerProgress();});}
  var scanStart=E('button',{class:'btn',click:function(){return scanAction('backup-scan');}},_('开始自动优选（约 1～4 分钟）'));
  var scanApply=E('button',{class:'btn',click:function(){return scanAction('backup-scan-apply');}},_('应用推荐入口（不切换）'));
  var scanCard=E('section',{class:'cbi-section rp-backup-card'},[E('h3',{},_('自动优选 Cloudflare 入口 IP')),E('p',{},_('从 Cloudflare 公布的 IPv4 网段中，小范围测试最多 8 个入口。每个入口实际连接你的 Worker，验证 Google、GitHub 和小文件下载；不会扫描局域网或切换设备。')),scanInfo,workerPanel('scan'),E('div',{class:'rp-backup-actions'},[scanStart,scanApply]),scanRows,E('p',{class:'rp-help'},_('综合两站响应延迟排序，短测下载作为参考。只推荐两站均通过的入口；限流单独显示。不代表全网最快、固定国家或带宽上限；结果 5 分钟有效。应用仅改变备用入口，域名证书身份和主线设置保持不变，之后需重新独立检测。'))]);
  function updateScan(state){
   scanState=state;var busy=scanBusy||workerOperationBusy('scan')||(state&&['queued','running'].indexOf(state.state)>=0&&Date.now()/1000-Number(state.started_at)<300);
   scanStart.disabled=!!busy;scanApply.disabled=!!(busy||!state||state.state!=='done'||!state.best_ip||Date.now()/1000-Number(state.finished_at)>300||!backupNode||state.fingerprint!==backupNode.fingerprint||uci.get('routepilot','main','backend')==='worker');
   scanStart.setAttribute('class',scanApply.disabled?'btn cbi-button-action':'btn');scanApply.setAttribute('class',scanApply.disabled?'btn':'btn cbi-button-action');
   if(state)scanInfo.textContent=state.message||(busy?_('正在优选，已完成 ')+(state.rows||[]).length+'/'+(state.total||8)+(state.progress!=null?' · '+state.progress+'%':''):_('等待结果'));
   scanRows.replaceChildren();if(!state||!Array.isArray(state.rows))return;
   state.rows.forEach(function(r){var checks=r.checks||[],all=checks.length===2&&checks.every(function(c){return c.ok;}),avg=checks.length?Math.round(checks.reduce(function(n,c){return n+c.ms;},0)/checks.length):null;scanRows.appendChild(E('p',{class:'rp-help'},(r.ip===state.best_ip?'★ ':'')+r.ip+' · '+(all?_('两站通过'):_('未完整通过'))+(avg!=null?' · '+avg+' ms':'')+(r.download&&r.download.ok?' · '+(r.download.mibps*8*1048576/1000000).toFixed(2)+' Mbps（短测）':r.download&&r.download.warning?' · '+r.download.warning:'')));});
  }
  updateScan(null);

  updateRelays(null,null);
  var connectionRows=E('div'),connectionDirectory=E('div'),connectionHistory=E('details',{class:'rp-stat-details'},[E('summary',{},_('连接记录与开关说明')),connectionRows,connectionDirectory,E('p',{class:'rp-help'},_('开关立即生效，无需保存；不会提交尚未保存的其他设置。'))]);
  function updateConnectionHistory(data){connectionRows.replaceChildren();var rows=data&&Array.isArray(data.events)?data.events.slice(-6).reverse():[];rows.forEach(function(row){var label=row.state==='running'?_('连接已恢复 / 已运行'):row.recovering?_('短暂中断，等待原客户端恢复'):row.state==='error'?_('连接异常'):row.state==='disabled'?_('已关闭'):_('状态变化');connectionRows.appendChild(E('p',{class:'rp-help'},when(row.time)+' · '+label+(row.fallback==='direct'?_(' · 已撤回接管，保持直连'):row.fallback==='unknown'?_(' · 网络路径未确认'):'')));});if(!rows.length)connectionRows.appendChild(E('p',{class:'rp-help'},_('尚无连接变化记录')));}
  var overviewBadge=E('span',{class:'rp-status-badge'},_('读取中'));
  var overviewMeta=E('p',{class:'rp-help'}),overviewLine=E('p',{class:'rp-overview-line',role:'status','aria-live':'polite'});
  var overviewFacts={},overviewPowerNonce=null;
  var overviewFactsNode=E('dl',{class:'imok-overview-facts'},[['enabled','独立开关'],['protocol','连接协议'],['exit','当前出口'],['scope','接管范围'],['applied','配置应用'],['website','网站检测']].map(function(item){var value=overviewFacts[item[0]]=E('dd',{},'—');return E('div',{},[E('dt',{},_(item[1])),value]);}));
  var overviewHero=E('section',{class:'cbi-section rp-overview-hero','data-tone':'unknown'},[E('div',{class:'rp-hero-heading'},[E('div',{class:'imok-connection-heading'},[E('h3',{},_('连接状态')),overviewBadge]),powerButton]),overviewFactsNode,overviewLine,overviewMeta,switchFeedback]);
  var trafficRange='today',latestTraffic=null,trafficNote=E('p',{class:'rp-data-note',role:'status'},_('正在读取流量观测…'));
  var trafficMeta=E('p',{class:'rp-help'}),metricFields={};
  var metricCards=['proxy','direct'].map(function(key){var value=E('strong',{class:'rp-metric-number'},'—'),detail=E('p',{class:'rp-help'}),speed=E('p',{class:'rp-help'});var today=E('strong',{},'—'),total=E('strong',{},'—');metricFields[key]={value:value,detail:detail,speed:speed,today:today,total:total};return E('section',{class:'cbi-section rp-metric-card rp-'+key},[E('div',{class:'rp-metric-heading'},[E('span',{class:'rp-metric-icon','aria-hidden':'true'},key==='proxy'?'↗':'⇄'),E('h3',{},key==='proxy'?_('代理出口 · 已观测'):_('直连线路 · 已观测')),value]),E('dl',{class:'imok-traffic-context'},[E('div',{},[E('dt',{},_('今日')),E('dd',{},today)]),E('div',{},[E('dt',{},_('累计')),E('dd',{},total)])]),detail,speed]);});
  var rangeButtons={};
  var rangeControls=E('div',{class:'rp-segments',role:'group','aria-label':_('流量统计时段')},[['today',_('今日')],['total',_('累计')]].map(function(item){var b=rangeButtons[item[0]]=E('button',{class:'btn','aria-pressed':item[0]===trafficRange?'true':'false',click:function(){trafficRange=item[0];Object.keys(rangeButtons).forEach(function(k){rangeButtons[k].setAttribute('aria-pressed',k===trafficRange?'true':'false');});updateTraffic(latestTraffic);}},item[1]);return b;}));
  var weekChart=E('div',{class:'rp-week-chart','aria-label':_('近七天已观测流量，蓝色代理，青色直连')}),weekLegend=E('p',{class:'rp-help'},[E('span',{class:'rp-legend-proxy'},_('● 代理')),E('span',{class:'rp-legend-direct'},_('● 直连')),_(' · 缺失日期显示 —，不补零')]);
  var trafficCard=E('section',{class:'cbi-section rp-traffic-section'},[E('div',{class:'rp-hero-heading'},[E('h3',{},_('流量观测')),rangeControls]),trafficNote,E('div',{class:'rp-metric-grid'},metricCards),E('div',{class:'rp-trend-heading'},[E('strong',{},_('近 7 天')),weekLegend]),weekChart,E('details',{class:'rp-stat-details'},[E('summary',{},_('统计口径与采样详情')),trafficMeta])]);
  var nodeCount=E('span',{class:'rp-group-state'},'—'),nodeSummary=E('p'),nodeTime=E('p',{class:'rp-help'});
  var nodeRefresh=E('button',{class:'btn',click:function(){nodeRefresh.disabled=true;return refreshDirectory().finally(function(){nodeRefresh.disabled=false;});}},_('刷新出口目录'));
  var nodeCard=E('section',{class:'cbi-section imok-node-card'},[E('div',{class:'rp-hero-heading'},[E('h3',{},[_('账户出口'),nodeCount]),E('a',{href:base+'/cloudflare/routing'},_('管理全部出口 →'))]),nodeSummary,nodeTime,E('div',{class:'rp-summary-actions'},[nodeRefresh])]);
  if(page!=='overview')connectionDirectory.appendChild(E('details',{class:'rp-stat-details'},[E('summary',{},_('账户目录时间与刷新')),nodeTime,nodeRefresh]));
  var versionInfo=E('p',{class:'rp-help'},_('客户端版本正在读取…'));
  var quickCard=E('section',{class:'cbi-section','aria-label':_('快捷操作')},[E('div',{class:'rp-quick-grid'},[['cloudflare/routing','↗',_('出口与分流'),_('查看当前出口和接管范围')],['cloudflare/dns','◎',_('DNS 与联网检测'),_('检查解析、网页并清理缓存')],['cloudflare/workers','⌁',_('Workers 备用'),_('部署、优选和独立检测')]].map(function(q){return E('a',{class:'rp-quick-tile',href:base+'/'+q[0]},[E('span',{class:'rp-quick-icon','aria-hidden':'true'},q[1]),E('span',{},[E('strong',{},q[2]),E('small',{},q[3])])]);}))]);
  if(page==='overview'&&data.runtimeRead)data.runtimeRead.then(function(info){if(!isCurrentView()||!info||!(info.update_available||!info.installed||info.check&&info.check.state==='failed'))return;quickCard.appendChild(E('p',{class:'rp-help'},[info.update_available?_('WARP 有新版本可供检查与手动升级。'):!info.installed?_('首次使用请先安装路由器的 WARP 运行环境。'):_('WARP 版本检查失败，最新版本暂未确认。'),E('a',{href:base+'/runtime'},_(' 前往 WARP 安装与更新'))]));});
  function bytes(n){if(!Number.isFinite(n)||n<0)return '—';var units=['B','KiB','MiB','GiB','TiB'],i=0;while(n>=1024&&i<units.length-1){n/=1024;i++;}return (i?n.toFixed(n<10?2:1):String(Math.round(n)))+' '+units[i];}
  function deviceUsagePanel(){
   var status=E('p',{class:'rp-help',role:'status','aria-live':'polite'}),results=E('div',{});
   var refreshButton=E('button',{type:'button',class:'btn',click:refreshUsage},_('刷新流量'));
   var card=panel(_('设备流量'),_('读取 GL.iNet 记录的累计用量。计数起点取决于固件，部分流量路径可能不在统计内。'),[status,results]);
   card.querySelector('.imok-panel-heading').appendChild(refreshButton);
   function refreshUsage(){
    refreshButton.disabled=true;status.textContent=_('正在读取设备流量…');results.replaceChildren();
    return fs.exec('/usr/libexec/routepilot-device-usage',[]).then(function(r){
     if(!isCurrentView())return;
     if(r.code)throw Error();
     var data=JSON.parse(r.stdout);
     if(data.state==='unsupported'){status.textContent=_('当前固件未提供按设备流量计数。');return;}
     if(data.state!=='ready'||!Array.isArray(data.devices)||data.devices.length>1024||!data.devices.every(function(row){
      return row&&typeof row.name==='string'&&typeof row.mac==='string'&&['upload','download','total'].every(function(key){return row[key]===null||Number.isSafeInteger(row[key])&&row[key]>=0;});
     }))throw Error();
     if(!data.devices.length){status.textContent=_('当前没有可显示的设备。');return;}
     status.textContent='';
     var columns=[_('设备'),_('上传'),_('下载'),_('合计')];
     var head=E('thead',{},E('tr',{},columns.map(function(label){return E('th',{scope:'col'},label);})));
     var rows=data.devices.map(function(row){
      var label=row.name?[E('strong',{},row.name),E('small',{class:'rp-help'},row.mac)]:row.mac;
      return E('tr',{},[E('th',{scope:'row'},label),E('td',{},bytes(row.upload)),E('td',{},bytes(row.download)),E('td',{},bytes(row.total))]);
     });
     results.replaceChildren(E('div',{class:'imok-device-usage-scroll',tabindex:'0',role:'region','aria-label':_('设备累计流量')},[E('table',{class:'imok-device-usage-table','aria-label':_('设备累计流量')},[head,E('tbody',{},rows)])]));
    }).catch(function(){if(isCurrentView())status.textContent=_('设备流量读取失败，请刷新重试。');}).finally(function(){refreshButton.disabled=false;});
   }
   refreshUsage();
   return card;
  }
  function validTrafficCounts(v){return v&&['proxy_up','proxy_down','direct_up','direct_down'].every(function(k){return Number.isSafeInteger(v[k])&&v[k]>=0;});}
  function updateTraffic(v){
   latestTraffic=v;var age=v?Date.now()/1000-Number(v.time):Infinity,live=v&&v.schema===1&&age>=-5&&age<=20&&v.state==='observing';
   var limited=!v||v.offload!=='not_detected';trafficNote.textContent=!live?(v&&v.message?v.message:!v?_('尚无采集数据，不能确认当前用量。'):_('采集已暂停或过期；以下仅为历史观测，不是当前用量。')):limited?_('仅统计未加速部分，可能远低于实际用量；图表与累计均为观测下限。'):_('观测下限，不作账单；历史可能存在采集中断。');
   trafficCard.setAttribute('data-stale',live?'false':'true');if(v&&v.clock_synced===false)trafficNote.textContent+=_(' 路由器时间尚未同步，新增流量暂不分配到日期。');
   var bucket=v&&v.days&&v.days[v.today],counts=trafficRange==='total'?v&&v.total:bucket&&bucket.bytes,valid=validTrafficCounts(counts);
   ['proxy','direct'].forEach(function(k){var f=metricFields[k];var todayCounts=bucket&&bucket.bytes,totalCounts=v&&v.total;f.today.textContent=validTrafficCounts(todayCounts)?bytes(todayCounts[k+'_up']+todayCounts[k+'_down']):'—';f.total.textContent=validTrafficCounts(totalCounts)?bytes(totalCounts[k+'_up']+totalCounts[k+'_down']):'—';f.value.textContent=valid?bytes(counts[k+'_up']+counts[k+'_down']):'—';f.detail.textContent=valid?(live?'':_('历史观测，停止于 ')+when(v.time)+' · ')+_('↑ 上传 ')+bytes(counts[k+'_up'])+_(' · ↓ 下载 ')+bytes(counts[k+'_down']):_('此时段没有采集数据');var last=v&&Array.isArray(v.history)&&v.history[v.history.length-1],rate=live&&last&&last.rate;f.speed.textContent=rate&&Number.isFinite(rate[k+'_down'])&&Number.isFinite(rate[k+'_up'])?_('已观测速率 ↑ ')+bytes(rate[k+'_up'])+'/s ↓ '+bytes(rate[k+'_down'])+'/s':_('当前速率：未知');});
   weekChart.replaceChildren();var today=v&&/^\d{4}-\d{2}-\d{2}$/.test(v.today||'')?v.today:null,days=[],max=1;
   if(today){var end=new Date(today+'T12:00:00Z');for(var i=6;i>=0;i--){var stamp=new Date(end.getTime()-i*86400000).toISOString().slice(0,10),b=v.days&&v.days[stamp],c=b&&b.bytes,ok=validTrafficCounts(c),proxy=ok?c.proxy_up+c.proxy_down:null,direct=ok?c.direct_up+c.direct_down:null;days.push({day:stamp,proxy:proxy,direct:direct,ok:ok,counts:c});if(ok)max=Math.max(max,proxy+direct);}}
   days.forEach(function(d){var text=d.ok?bytes(d.proxy+d.direct):'—',bar=E('div',{class:'rp-day-bars','aria-hidden':'true'},d.ok?[E('span',{class:'rp-bar-direct',style:'height:'+(d.direct/max*100)+'%'}),E('span',{class:'rp-bar-proxy',style:'height:'+(d.proxy/max*100)+'%'})]:E('span',{class:'rp-no-data'},'—'));weekChart.appendChild(E('div',{class:'rp-day',role:'img',tabindex:'0',title:d.day+': '+(d.ok?_('代理上传 ')+bytes(d.counts.proxy_up)+_(' / 下载 ')+bytes(d.counts.proxy_down)+_('；直连上传 ')+bytes(d.counts.direct_up)+_(' / 下载 ')+bytes(d.counts.direct_down):_('没有采集数据')),'aria-label':d.day+': '+(d.ok?_('代理 ')+bytes(d.proxy)+_('，直连 ')+bytes(d.direct):_('没有采集数据'))},[E('span',{class:'rp-day-number'},text),bar,E('span',{class:'rp-day-label'},d.day.slice(5))]));});
   if(!days.length)weekChart.appendChild(E('p',{class:'rp-help'},_('采集开始后显示每日趋势。')));
   trafficMeta.textContent=v?_('累计为自此时间起的观测下限：')+when(v.started_at)+_(' · 最后采样：')+when(v.time)+_(' · 路由器时区：')+String(v.timezone||_('未知'))+_('。每 5 分钟保存一次，掉电可能丢失未保存部分。仅含受管 IPv4 公网转发；不含 IPv6、局域网、路由器本机和隧道封装开销。')+(v.gaps?_(' 已记录不完整采样区间或边界：')+v.gaps+_(' 次。'):'')+(validTrafficCounts(v.unallocated)&&Object.keys(v.unallocated).some(function(k){return v.unallocated[k]>0;})?_(' 有无法归属到日期的流量，仅纳入累计。'):''):_('升级前的流量无法补算。');
   versionInfo.textContent='imOK '+(buildInfo.package||_('版本待读取'))+' · WARP '+(v&&v.client_version?v.client_version:_('版本未知'))+_(' · 仅显示本机读到的版本；是否有更新请核对官方发布记录。');
  }
  function updateOverview(v,t){
   var fresh=feedback.fresh(v,Date.now()),running=fresh&&v.state==='running',tone=!fresh?'unknown':running?'good':v.state==='error'?'bad':'warn';
   overviewHero.setAttribute('data-tone',tone);overviewBadge.textContent=!fresh?_('？ 状态未知'):running?(v.backend==='worker'?_('✓ 备用线路运行中（TCP）'):_('✓ 隧道已连接')):v.state==='error'?_('! 连接故障，请查看详情'):v.state==='disabled'?_('○ 已关闭'):v.state==='preparing'?_('◷ 登录准备中'):_('◷ 等待连接');
   overviewLine.textContent=!fresh?_('状态已过期，当前连接未确认'):running?(v.backend==='worker'?_('Workers 备用'):_('WARP 主线路'))+' · '+_('实际协议：')+(v.protocol||_('待确认'))+' · '+directoryLabel(v.exit,catalog)+' · IP '+(v.exit_ip||'—'):v.message||_('等待连接');
   overviewMeta.textContent=_('状态更新时间：')+when(v&&v.time)+_(' · 接管范围：')+(v&&v.scope==='lan'?_('所选局域网'):_('所选设备'))+_(' · 隧道连接不等于所有网站可访问。')+(v&&v.protocol_warning?' '+v.protocol_warning:'');
   var recent=catalog.available&&Date.now()-catalog.read_at<=60000,count=catalog.available?catalog.networks.length:null;
   overviewFacts.enabled.textContent=switchEnabled?_('已启用'):_('已关闭');overviewFacts.protocol.textContent=fresh&&v&&v.protocol?v.protocol:_('未确认');overviewFacts.exit.textContent=fresh&&running?(v.backend==='worker'?(v.backup_name||_('Workers 备用')):directoryLabel(v.exit,catalog)):_('当前出口未确认');overviewFacts.scope.textContent=fresh?(v.scope==='lan'?_('所选局域网'):_('所选设备'))+(Array.isArray(v.devices)?' · '+v.devices.length+_(' 条接管策略'):''):_('未确认');if(switchPending)overviewPowerNonce=switchPending.nonce;var configuredNonce=overviewPowerNonce||uci.get('routepilot','main','apply_nonce');overviewFacts.applied.textContent=fresh&&['running','disabled','preparing'].indexOf(v.state)>=0&&configuredNonce&&v.applied_nonce===configuredNonce?_('当前配置已确认应用'):_('尚未确认当前配置已应用');
   nodeCount.textContent=count==null?_('目录待确认'):_('共 ')+String(count)+_(' 个可选网络');
   var verified=feedback.wizardProxyVerification(t,v,wizardConfig(),Date.now()),proof=verified==='success',proofText=proof?_('1 个最近检测通过'):verified==='error'?_('当前出口检测失败'):_('当前出口尚无有效检测');
   overviewFacts.website.textContent=proof?_('近期所测代理网站通过'):verified==='error'?_('所测网站存在失败'):verified==='warning'?_('所测网站访问受限'):verified==='busy'?_('检测中'):_('尚无有效检测');
   nodeSummary.textContent=(v&&v.backend!=='worker'?_('当前出口：')+directoryLabel(v.exit,catalog)+' · ':'')+(v&&v.backend==='worker'?_('当前运行备用线路；账户出口未接管。'):(fresh?(running?_('1 个当前连接'):_('0 个当前连接')):_('当前连接数未知'))+' · '+proofText);

   nodeTime.textContent=catalog.available?(recent?_('目录读取时间：'):_('目录可能已变化，上次读取：'))+new Date(catalog.read_at).toLocaleString()+(proof?_(' · 网页检测通过时间：')+when(t.finished_at)+_('（5 分钟内有效）'):''):_('目录暂不可用，请刷新；不代表账户没有出口。');
  }
  var rulesCard=E('div',{class:'cbi-section'},[E('h3',{},_('规则更新反馈')),rulesProgress,rulesTime,rulesCounts]);
  var rulesCatalog=null,rulesInfoBusy=false,rulesQueryBusy=false,rulesMutationBusy=false,rulesInfoAt=0,rulesQuerySerial=0,rulesInfoSerial=0,rulesPage=1,rulesPages=1,rulesSearchApplied='';
  var ruleNames={china:_('中国 IP'),china_domains:_('中国域名'),gfw:_('代理域名'),whitelist:_('直连域名')},rulesRefreshButtons=[];
  var rulesPlan=E('div',{'aria-live':'polite'},_('正在读取当前更新计划…')),rulesVersion=E('div',{'aria-live':'polite'}),rulesSources=E('div'),rulesChanges=E('div'),rulesCandidate=E('div'),rulesPersonalSummary=E('div'),rulesOperation=E('p',{role:'status','aria-live':'polite'});
  var rulesKind=E('select',{class:'cbi-input-select','aria-label':_('规则分类')},Object.keys(ruleNames).map(function(k){return E('option',{value:k},ruleNames[k]);}));rulesKind.value='china';
  var rulesView=E('select',{class:'cbi-input-select','aria-label':_('规则内容范围')},[E('option',{value:'effective'},_('合并规则（订阅 + 个人覆盖）')),E('option',{value:'current'},_('订阅原始规则')),E('option',{value:'personal_added'},_('个人新增')),E('option',{value:'personal_excluded'},_('个人排除')),E('option',{value:'added'},_('本次订阅新增')),E('option',{value:'removed'},_('本次订阅删除')),E('option',{value:'candidate'},_('待确认订阅规则')),E('option',{value:'candidate_added'},_('待确认订阅新增')),E('option',{value:'candidate_removed'},_('待确认订阅删除'))]);rulesView.value='effective';
  var rulesSearch=E('input',{class:'cbi-input-text',type:'search',maxlength:253,placeholder:_('搜索域名或 IP 网段'),'aria-label':_('搜索规则内容')});
  var rulesRows=E('div',{class:'imok-rule-rows','aria-live':'polite'}),rulesPageInfo=E('span',{'aria-live':'polite'},_('尚未读取规则内容'));
  function rulesRefreshButton(){var button=E('button',{class:'btn',click:function(){return readRulesInfo(true);}},_('刷新规则状态'));rulesRefreshButtons.push(button);return button;}
  var rulesRefresh=rulesRefreshButton();
  var rulesSearchButton=E('button',{class:'btn cbi-button-action',click:function(){rulesSearchApplied=rulesSearch.value.trim();return readRulesPage(1);}},_('搜索规则'));
  var rulesPrevious=E('button',{class:'btn',click:function(){return readRulesPage(rulesPage-1);}},_('上一页'));
  var rulesNext=E('button',{class:'btn',click:function(){return readRulesPage(rulesPage+1);}},_('下一页'));
  var rulesAccept=E('button',{class:'btn cbi-button-action',click:function(){return confirmRuleChange('accept');}},_('核对并应用异常候选'));
  var rulesRollback=E('button',{class:'btn',click:function(){return confirmRuleChange('rollback');}},_('恢复上一版规则'));
  var rulesAddInput=E('input',{class:'cbi-input-text',type:'text',maxlength:253,placeholder:_('域名或公网 IPv4/CIDR'),'aria-label':_('个人新增规则')});
  var rulesAddButton=E('button',{class:'btn cbi-button-action',click:function(){var value=rulesAddInput.value.trim();if(!value){rulesOperation.textContent=_('请先输入需要新增的域名或 IP 网段。');return;}return confirmPersonalChange('add',value);}},_('新增到本分类'));
  var rulesImmediateUpdate=E('button',{class:'btn cbi-button-action',click:function(){return self.requestRulesUpdate();}},_('立即更新已保存来源'));
  function rulesStamp(ts){return ts?new Date(ts*1000).toLocaleString():_('暂无记录');}
  function rulesShort(value){return value?String(value).slice(0,12):_('未知');}
  function rulesOverrideRevision(info){return info&&info.overrides&&info.overrides.revision||'';}
  function sourceValue(picker){var element=typeof picker.option.getUIElement==='function'?picker.option.getUIElement('main'):null;return element&&typeof element.getValue==='function'?String(element.getValue()||''):String(picker.widget.value||picker.initial||'');}
  function updateSourcePickers(info){
   Object.keys(sourcePickers).forEach(function(kind){var picker=sourcePickers[kind],current=sourceValue(picker),presets=(info.source_presets||[]).filter(function(p){return Array.isArray(p.kinds)&&p.kinds.indexOf(kind)>=0;});
    var signature=JSON.stringify(presets);if(picker.signature!==signature){picker.signature=signature;picker.presets=presets;picker.select.replaceChildren(E('option',{value:'custom'},_('自定义地址 / 保留当前地址')));presets.forEach(function(p){picker.select.appendChild(E('option',{value:p.id},p.label+(p.mirror&&String(p.label).indexOf('镜像')<0?_(' · 分发镜像，可能滞后'):'')));});}
    picker.select.value=picker.manual?'custom':(presets.filter(function(p){return p.url===current;})[0]||{}).id||'custom';picker.select.setAttribute('data-selected',picker.select.value!=='custom'?'preset':picker.touched?'custom':'none');
    var historical=info.current&&info.current.sources&&info.current.sources[kind];picker.downloaded.textContent=_('已下载版本来源：')+(historical||_('旧版本未记录来源'));
   });
  }
  function rulesControls(){
   var busy=rulesQueryBusy||rulesMutationBusy;rulesRefreshButtons.forEach(function(button){button.disabled=rulesInfoBusy||rulesMutationBusy;});rulesSearchButton.disabled=busy||!rulesCatalog;rulesKind.disabled=busy;rulesView.disabled=busy;Array.prototype.forEach.call(rulesView.children,function(option){if(String(option.value||option.getAttribute&&option.getAttribute('value')||'').indexOf('candidate')===0)option.disabled=!rulesCatalog||!rulesCatalog.candidate;});rulesSearch.disabled=busy;rulesPrevious.disabled=busy||rulesPage<=1;rulesNext.disabled=busy||rulesPage>=rulesPages;rulesAccept.disabled=rulesInfoBusy||rulesMutationBusy||!rulesCatalog||!rulesCatalog.candidate;rulesRollback.disabled=rulesInfoBusy||rulesMutationBusy||!rulesCatalog||!rulesCatalog.previous;rulesAddInput.disabled=rulesMutationBusy||!rulesCatalog;rulesAddButton.disabled=rulesInfoBusy||rulesMutationBusy||!rulesCatalog;rulesImmediateUpdate.disabled=rulesMutationBusy;
  }
  function showRulesCatalog(info){
   var current=info.current||{},effective=info.effective||current,overrides=info.overrides||{},application=info.application||{},schedule=info.schedule||{},candidate=info.candidate;
   rulesPlan.replaceChildren(E('p',{},_('已保存的自动更新间隔：')+(schedule.hours?String(schedule.hours)+_(' 小时'):_('待确认'))),E('p',{},_('最近尝试：')+rulesStamp(schedule.last_attempt_at)+' · '+_('最近成功：')+rulesStamp(schedule.last_success_at)),E('p',{},_('下次计划：')+(!schedule.next_due_at&&info.candidate?_('等待异常候选确认，自动更新暂缓'):rulesStamp(schedule.next_due_at))+(schedule.state==='paused'?_('（当前暂停，恢复运行后再执行）'):schedule.state==='running'?_('（正在更新）'):schedule.retry_pending?_('（失败后的重试计划）'):'')),E('p',{},(schedule.message||'')+(schedule.retries_exhausted?_(' · 已完成3次重试，将按正常间隔检查'):schedule.retry_count?_(' · 退避重试阶段：')+schedule.retry_count+'/'+(schedule.retry_limit||3):'')));
   var appliedText=({applied:_('已在当前连接中生效'),pending:_('已保存，等待运行中应用'),inactive:_('当前未接管网络，规则已保存'),unknown:_('当前运行版本尚未确认')})[application.state]||_('当前运行版本尚未确认');
   rulesVersion.replaceChildren(E('p',{},_('当前已保存版本：')+rulesShort(effective.revision)+' · '+_('当前订阅版本：')+rulesShort(current.revision)+' · '+rulesStamp(current.time)),E('p',{},_('运行状态：')+appliedText+(application.revision?' · '+_('运行版本：')+rulesShort(application.revision):'')),E('p',{class:'rp-help'},_('已保存不等于已生效；订阅更新保留个人新增与排除。')));
   rulesSources.replaceChildren();var changeItems=[];rulesChanges.replaceChildren();Object.keys(ruleNames).forEach(function(k){var change=current.changes&&current.changes[k],source=current.sources&&current.sources[k];rulesSources.appendChild(E('p',{},[E('strong',{},ruleNames[k]+_('：')),E('span',{style:'overflow-wrap:anywhere;word-break:break-word;'},source||_('当前版本未记录来源')),E('small',{},current.hashes&&current.hashes[k]?' · SHA-256 '+rulesShort(current.hashes[k]):'')]));changeItems.push(ruleNames[k]+_('：新增 ')+(change?change.added:'—')+_(' 条，删除 ')+(change?change.removed:'—')+_(' 条'));});rulesChanges.appendChild(E('p',{},changeItems.join('； ')));
   rulesPersonalSummary.replaceChildren(E('p',{},_('当前已保存订阅版本：')+rulesShort(current.revision)+' · '+_('个人覆盖版本：')+rulesShort(overrides.revision)),E('p',{},_('合并规则版本：')+rulesShort(effective.revision)+' · '+appliedText),E('p',{class:'rp-help'},_('合并内容是否已生效，以运行状态为准。')));var personalCounts=Object.keys(ruleNames).map(function(k){var count=overrides.counts&&overrides.counts[k]||{};return ruleNames[k]+_('：新增 ')+(count.added||0)+_('，排除 ')+(count.excluded||0);});rulesPersonalSummary.appendChild(E('p',{},personalCounts.join('； ')));
   updateSourcePickers(info);
   rulesCandidate.replaceChildren();if(candidate){var notices=Object.keys(candidate.declines||{}).map(function(k){var d=candidate.declines[k];return (ruleNames[k]||k)+' '+d.before+' → '+d.after;});rulesCandidate.appendChild(E('p',{role:'alert'},_('检测到规则数量异常下降，本次候选尚未覆盖当前版本：')+notices.join('；')));rulesCandidate.appendChild(E('p',{},_('候选版本：')+rulesShort(candidate.revision)+' · '+_('确认有效期至：')+rulesStamp(candidate.expires_at)));rulesCandidate.appendChild(E('p',{class:'rp-help'},_('可先到“规则管理”核对候选及其增删内容；确认后才保存候选，个人覆盖继续保留。')));rulesCandidate.appendChild(rulesAccept);}
   rulesControls();
  }
  function readRulesInfo(force){
   if(!ruleDataPage||!isCurrentView()||rulesMutationBusy||rulesInfoBusy||!force&&Date.now()-rulesInfoAt<10000)return Promise.resolve();
   rulesInfoBusy=true;rulesControls();var serial=++rulesInfoSerial;
   return command('rules-info').then(JSON.parse).then(function(info){if(!isCurrentView()||serial!==rulesInfoSerial)return;var old=rulesCatalog&&rulesCatalog.current&&rulesCatalog.current.revision,oldOverrides=rulesOverrideRevision(rulesCatalog),oldCandidate=rulesCatalog&&rulesCatalog.candidate&&rulesCatalog.candidate.revision,candidateChanged=oldCandidate!==(info.candidate&&info.candidate.revision),candidateView=rulesView.value.indexOf('candidate')===0;rulesCatalog=info;if(rulesOperation._infoError){rulesOperation.textContent='';rulesOperation._infoError=false;}if(candidateView&&!info.candidate)rulesView.value='effective';rulesInfoAt=Date.now();showRulesCatalog(info);if(ruleSection==='manage'&&(old!==(info.current||{}).revision||oldOverrides!==rulesOverrideRevision(info)||candidateView&&candidateChanged)){rulesQuerySerial++;rulesQueryBusy=false;rulesRows.replaceChildren();return readRulesPage(1);}}).catch(function(e){if(isCurrentView()&&serial===rulesInfoSerial){rulesOperation._infoError=true;rulesOperation.textContent=_('规则状态读取失败：')+e.message;}}).finally(function(){if(serial===rulesInfoSerial){rulesInfoBusy=false;rulesControls();}});
  }
  function readRulesPage(pageNumber){
   if(ruleSection!=='manage'||!isCurrentView()||!rulesCatalog||rulesMutationBusy)return Promise.resolve();
   var serial=++rulesQuerySerial,revision=(rulesCatalog.current||{}).revision,overridesRevision=rulesOverrideRevision(rulesCatalog),candidateRevision=rulesCatalog.candidate&&rulesCatalog.candidate.revision,payload={kind:rulesKind.value,view:rulesView.value,search:rulesSearchApplied,page:Math.max(1,pageNumber),page_size:100,revision:revision,overrides_revision:overridesRevision};rulesQueryBusy=true;rulesRows.setAttribute('aria-busy','true');rulesPageInfo.textContent=_('正在读取规则内容…');rulesControls();
   return submitInput('rules-input','rules-query',payload).then(function(result){if(!isCurrentView()||serial!==rulesQuerySerial)return;if(result.revision!==revision||result.overrides_revision!==overridesRevision||result.kind!==payload.kind||result.view!==payload.view||result.search!==payload.search||payload.view.indexOf('candidate')===0&&result.candidate_revision!==candidateRevision)throw Error(_('规则版本或查询条件已改变，请刷新后重试'));rulesPage=result.page;rulesPages=result.pages;rulesRows.replaceChildren();(result.entries||[]).forEach(function(line){var origin=result.origins&&result.origins[line]||'subscription',row=[E('code',{},line)],action=null,label='';if(payload.view.indexOf('candidate')!==0){if(origin==='excluded'||payload.view==='personal_excluded'){action='restore';label=_('撤销排除');}else if(origin==='personal'||payload.view==='personal_added'){action='remove_added';label=_('删除个人新增');}else if(['current','effective','added','removed'].indexOf(payload.view)>=0){action='exclude';label=_('长期排除');}}if(origin==='personal')row.push(E('small',{},_('个人新增')));if(origin==='excluded')row.push(E('small',{},_('已排除，不参与生效')));if(action)row.push(E('button',{class:'btn',click:function(){return confirmPersonalChange(action,line,{kind:payload.kind,revision:revision,overrides_revision:overridesRevision});}},label));rulesRows.appendChild(E('div',{class:'rp-rule-entry rp-rule-entry-manage'},row));});if(!result.entries||!result.entries.length)rulesRows.appendChild(E('p',{class:'rp-help'},_('没有符合条件的规则。可清空搜索，或选择其他分类、内容范围。')));rulesPageInfo.textContent=_('共 ')+result.total+_(' 条 · 第 ')+result.page+'/'+Math.max(1,result.pages)+_(' 页 · 每页最多 100 条');}).catch(function(e){if(isCurrentView()&&serial===rulesQuerySerial){rulesRows.replaceChildren(E('p',{role:'alert'},_('规则内容读取失败：')+e.message));rulesPageInfo.textContent=_('查询未完成，可重新搜索或刷新');}}).finally(function(){if(serial===rulesQuerySerial){rulesQueryBusy=false;rulesRows.setAttribute('aria-busy','false');rulesControls();}});
  }
  function submitRuleMutation(action,payload,confirm){
   if(rulesMutationBusy||!isCurrentView())return;rulesMutationBusy=true;rulesOperation._infoError=false;rulesInfoSerial++;rulesQuerySerial++;rulesInfoBusy=false;rulesQueryBusy=false;rulesControls();confirm.disabled=true;rulesOperation.textContent=_('正在提交规则变更…');ui.hideModal();return submitInput('rules-input',action,payload).then(function(result){if(isCurrentView()){rulesOperation.textContent=result.message||_('规则已保存，正在确认实际生效状态。');if(action==='rules-edit'&&payload.action==='add')rulesAddInput.value='';}}).catch(function(e){if(isCurrentView())rulesOperation.textContent=_('规则变更未完成：')+e.message;}).finally(function(){rulesMutationBusy=false;rulesControls();if(isCurrentView())return readRulesInfo(true);});
  }
  function confirmPersonalChange(action,value,bound){
   if(ruleSection!=='manage'||rulesMutationBusy||!rulesCatalog)return;var current=rulesCatalog.current||{},kind=bound&&bound.kind||rulesKind.value,payload={kind:kind,action:action,value:value,revision:bound&&bound.revision||current.revision,overrides_revision:bound&&bound.overrides_revision||rulesOverrideRevision(rulesCatalog)};
   var title=({add:_('确认个人新增'),exclude:_('确认长期排除'),restore:_('确认撤销排除'),remove_added:_('确认删除个人新增')})[action],message=action==='exclude'?_('此条目将从本分类的实际生效规则中排除。排除记录会长期保存，订阅更新不会把它重新加入；可在“个人排除”中撤销。'):action==='add'?_('此条目将作为个人规则加入本分类，长期保留，不会被订阅更新覆盖。'):action==='restore'?_('撤销个人排除后，此条目是否生效取决于当前订阅或个人新增内容。'):_('仅删除个人新增记录；如果当前订阅也包含此条目，它仍会按订阅生效。');
   var confirm=E('button',{class:'btn cbi-button-action',click:function(){return submitRuleMutation('rules-edit',payload,confirm);}},_('确认变更'));
   showImokModal(title,[E('p',{},(ruleNames[kind]||kind)+' · '+value),E('p',{},message),E('p',{class:'rp-help'},_('本分类覆盖只影响使用该分类的分流模式。若要始终直连或代理，请到“自定义分流”。规则变更保存后仍需确认运行中生效。')),E('div',{class:'rp-summary-actions'},[confirm,E('button',{class:'btn',click:ui.hideModal},_('取消'))])]);
  }
  function confirmRuleChange(action){
   if(rulesMutationBusy||!rulesCatalog)return;var info=rulesCatalog,current=info.current||{},target=action==='accept'?info.candidate:info.previous;if(!target)return;
   var payload={revision:current.revision,confirm:true};payload[action==='accept'?'candidate_revision':'previous_revision']=target.revision;
   var message=action==='accept'?_('此候选因规则数量异常下降而被暂缓。确认后以此候选替换已保存订阅，个人覆盖继续保留。'):_('确认恢复上一版已保存订阅？个人覆盖、来源和更新频率设置保持当前值；后续自动更新仍按保存的计划执行。');
   var confirm=E('button',{class:'btn cbi-button-action',click:function(){return submitRuleMutation(action==='accept'?'rules-accept':'rules-rollback',payload,confirm);}},action==='accept'?_('确认应用此候选'):_('确认恢复此版本'));
   showImokModal(action==='accept'?_('确认应用异常候选'):_('确认恢复上一版规则'),[E('p',{},message),E('p',{},_('当前订阅版本：')+rulesShort(current.revision)+' → '+_('目标版本：')+rulesShort(target.revision)),E('p',{class:'rp-help'},_('规则变更可能影响受管设备的分流；保存成功后仍需确认运行状态。版本已变化或候选已过期时，操作会拒绝执行。')),E('div',{class:'rp-summary-actions'},[confirm,E('button',{class:'btn',click:ui.hideModal},_('取消'))])]);
  }
  rulesKind.addEventListener('change',function(){return readRulesPage(1);});rulesView.addEventListener('change',function(){return readRulesPage(1);});rulesSearch.addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();rulesSearchApplied=rulesSearch.value.trim();return readRulesPage(1);}});
  rulesControls();
  var rulesNav=E('nav',{class:'rp-section-nav','aria-label':_('规则与更新页面')},Object.keys(ruleSections).map(function(key){return E('a',{class:'btn',href:base+'/rules/'+ruleSections[key],'aria-current':key===page?'page':null},ruleSectionNames[ruleSections[key]]);}));
  var rulesStatusCard=E('section',{class:'cbi-section','aria-label':_('规则版本与执行计划')},[E('h3',{},_('规则版本与执行计划')),E('div',{class:'imok-settings-pair'},[panel(_('更新计划'),_('自动更新使用已保存的来源与间隔。'),[rulesPlan]),panel(_('保存版本与运行状态'),null,[rulesVersion])]),E('section',{class:'rp-rule-summary imok-rule-source-summary'},[E('h4',{},_('当前下载版本来源与校验摘要')),E('a',{class:'btn',href:base+'/rules/sources'},_('编辑来源与频率')),rulesSources]),E('section',{class:'rp-rule-summary'},[E('h4',{},_('本次订阅更新变化')),rulesChanges]),rulesCandidate,E('div',{class:'rp-summary-actions'},[rulesRefresh,rulesRollback,rulesImmediateUpdate])].concat(ruleSection==='overview'?[rulesOperation]:[]));
  var rulesBrowser=E('section',{class:'cbi-section','aria-label':_('规则内容浏览')},[E('p',{class:'rp-help'},[_('个人新增和排除不会被订阅更新覆盖。本分类覆盖只影响使用该分类的分流模式；始终直连/代理请到'),E('a',{href:base+'/rules/custom'},_('自定义分流')),_('。常规“导出配置”不包含个人新增和排除；升级会保留，迁移需另备份。')]),panel(_('规则版本与个人覆盖'),null,[rulesPersonalSummary,E('div',{class:'rp-summary-actions'},[rulesRefreshButton()])]),E('div',{class:'imok-rule-tools'},[E('div',{class:'imok-rule-filter'},[E('div',{class:'rp-summary-actions rp-rule-controls'},[E('label',{},[_('规则分类 '),rulesKind]),E('label',{},[_('内容范围 '),rulesView])]),E('div',{class:'rp-summary-actions rp-rule-controls'},[E('label',{},[_('搜索内容 '),rulesSearch]),rulesSearchButton])]),panel(_('添加个人规则'),_('仅对当前所选分类生效，订阅更新会保留。'),[E('div',{class:'rp-summary-actions rp-rule-controls'},[E('label',{},[_('个人新增 '),rulesAddInput]),rulesAddButton])])]),rulesRows,E('div',{class:'rp-summary-actions'},[rulesPrevious,rulesPageInfo,rulesNext])].concat(ruleSection==='manage'?[rulesOperation]:[]));
  var rulesSourcesIntro=E('section',{class:'cbi-section','aria-label':_('来源与频率说明')},[E('p',{class:'rp-help'},[_('选择预设或编辑 HTTPS 地址后保存；自动更新按计划执行，立即下载请到'),E('a',{href:base+'/rules/overview'},_('更新概况')),_('。镜像可能有缓存延迟，不是独立规则；来源选择不改变线路入口或出口。')]),rulesRefreshButton()].concat(ruleSection==='sources'?[rulesOperation]:[]));
  var rulesCustomIntro=E('section',{class:'cbi-section'},[E('p',{class:'rp-help'},_('始终直连和代理规则独立保存，订阅更新不会清空；修改后保存并应用。'))]);
  var testCard=E('div',{class:'cbi-section'},[E('h3',{},_('网站连通性检测')),testsInfo,testButton,E('table',{class:'table'},[E('tr',{class:'tr table-titles'},[E('th',{},_('网站')),E('th',{},_('检测线路')),E('th',{},_('结果')),E('th',{},_('HTTPS 耗时'))])].concat(testRows)),E('p',{class:'rp-help'},_('检测 HTTPS 能否建立连接；耗时不是 Ping，也不代表视频速度或网站登录一定可用。'))]);
  var dnsInfo=E('p',{role:'status','aria-live':'polite'},_('正在读取独立 DNS 状态…')),dnsCounts=E('p'),dnsEffective=E('p'),dnsResults=E('div'),dnsTestInfo=E('p',{role:'status','aria-live':'polite'},_('尚未执行 DNS 检测'));
  var clearPending=false,dnsSubmitting=false,dnsJobPending=null;
  var dnsClearButton=E('button',{class:'btn',disabled:true,click:function(){
   if(clearPending||feedback.dnsState(liveDNS,wizardStatus,Date.now())!=='running')return;
   clearPending=true;dnsClearButton.disabled=true;
   command('dns-clear').then(JSON.parse).then(function(result){
    if(!result.ok||!liveDNS||['runtime_id','generation','process_instance'].some(function(k){return result[k]!==liveDNS[k];}))throw Error(_('连接已改变，请刷新后确认缓存状态'));
    notify(_('已清理 imOK 独立 DNS 缓存，移除 ')+result.removed+_(' 条；未清理系统、SSR 缓存或动态分流规则。'));
   }).catch(function(e){notify(e.message,true);}).finally(function(){clearPending=false;dnsClearButton.disabled=feedback.dnsState(liveDNS,wizardStatus,Date.now())!=='running'||!liveDNS.control_available;});
  }},_('清理本插件 DNS 缓存'));
  var dnsProbeButton=E('button',{class:'btn cbi-button-action',disabled:true,click:function(){
   if(feedback.dnsState(liveDNS,wizardStatus,Date.now())!=='running')return;
   if(dnsSubmitting)return;dnsSubmitting=true;dnsProbeButton.disabled=true;dnsTestInfo.textContent=_('正在提交 DNS 检测…');
   command('dns-test').then(JSON.parse).then(function(result){
    dnsTestInfo.textContent=result.message;
    if(result.state==='submitted'||result.state==='running')dnsJobPending={id:result.request_id||null,time:Date.now()};
   }).catch(function(e){dnsTestInfo.textContent=e.message;dnsProbeButton.disabled=false;}).finally(function(){dnsSubmitting=false;});
  }},_('检测当前 DNS 解析'));
  var dnsCacheCard=E('div',{class:'cbi-section'},[E('h3',{},_('独立 DNS 状态与缓存')),dnsInfo,dnsEffective,dnsCounts,dnsClearButton,E('p',{class:'rp-help'},_('显示已生效的 DNS，表单未保存的修改不会用于检测。清理只作用于本插件缓存，不重启隧道，也不清除动态分流规则。'))]);
  var dnsTestCard=E('div',{class:'cbi-section'},[E('h3',{},_('DNS 解析检测')),E('p',{class:'rp-help'},_('从路由器测试当前受管 DNS 入口的国内与代理域名解析。上游按生效规则显示，结果不能代替设备端到端验收。检测会产生正常 DNS 查询，不会保存表单或切换出口。')),dnsProbeButton,dnsTestInfo,dnsResults]);
  function updateDNS(data,test,status){
   liveDNS=data;var now=Date.now(),state=feedback.dnsState(data,status,now);
   dnsInfo.textContent=({running:_('独立 DNS 正在运行'),inactive:_('分流未运行，独立 DNS 当前不可用'),stale:_('DNS 状态已过期或连接已改变'),unavailable:_('暂未取得独立 DNS 状态')})[state];
   dnsCounts.textContent='';dnsEffective.textContent='';
   var valid=state==='running';dnsClearButton.disabled=clearPending||!valid||!data.control_available;
   if(valid){
    var entries=0,hits=0,requests=0,errors=0;data.policies.forEach(function(p){entries+=p.cache_entries;hits+=p.cache_hits;requests+=p.requests;errors+=p.errors;});
    dnsCounts.textContent=_('缓存条目：')+entries+' · '+_('命中：')+hits+' / '+_('查询：')+requests+' · '+_('转发异常：')+errors;
    dnsEffective.textContent=_('生效国内上游：')+data.upstreams.dns_domestic+' · '+String(data.upstreams.transport||'').toUpperCase()+' · '+_('代理上游：')+(data.upstreams.proxy_dns_override?data.upstreams.proxy_dns_override+_(' · DoH，经备用线路加密'):data.upstreams.dns_foreign+_(' · 普通 DNS，非加密上游'));
    if(!data.control_available)dnsInfo.textContent+=_('；缓存控制暂不可用，转发仍独立运行');
   }
   var ts=feedback.dnsTestState(test,status,data,now);
   if(dnsJobPending&&now-dnsJobPending.time>50000)dnsJobPending=null;
   var awaiting=dnsJobPending&&(!test||dnsJobPending.id&&test.request_id!==dnsJobPending.id);
   if(dnsJobPending&&!awaiting&&ts!=='busy')dnsJobPending=null;
   dnsProbeButton.disabled=dnsSubmitting||!valid||ts==='busy'||!!dnsJobPending;
   dnsTestInfo.textContent=awaiting?_('等待本次 DNS 检测作业，最长约 40 秒…'):({busy:_('正在检测，最长约 40 秒…'),success:_('本机受管 DNS 入口解析通过'),error:_('部分 DNS 解析检测失败'),warning:_('部分 DNS 入口未就绪或没有完整解析结果'),stale:_('历史 DNS 结果已过期或检测环境已改变，请重新检测'),pending:_('暂未取得有效的 DNS 检测结果'),unavailable:_('当前 DNS 未运行，尚不能检测')})[ts];
   if(test&&test.finished_at)dnsTestInfo.textContent+=' · '+_('检测时间：')+when(test.finished_at);
   var rows=(test&&Array.isArray(test.results)?test.results:[]).map(function(r){return E('tr',{},[E('td',{},r.label||_('受管范围 ')+r.policy),E('td',{},r.domain+' · '+String(r.protocol||'').toUpperCase()),E('td',{},(r.configured_route==='domestic'?_('国内上游：'):_('代理上游：'))+r.upstream),E('td',{},(ts==='stale'||awaiting?_('历史：'):'')+(r.message||r.state)+(r.addresses&&r.addresses.length?' · '+r.addresses.join(', '):'')),E('td',{},r.ms!=null?r.ms+' ms':'—')]);});
   dnsResults.replaceChildren(E('table',{class:'table'},[E('thead',{},E('tr',{},['受管范围','测试域名 / 接入','按规则选择的上游','解析结果','耗时'].map(function(t){return E('th',{},_(t));}))),E('tbody',{},rows)]));
  }
  var receiveState=E('p',{role:'status','aria-live':'polite'},_('正在读取接收观测…')),receiveCounts=E('p'),receiveCoverage=E('p');
  var receiveCard=E('div',{class:'cbi-section'},[E('h3',{},_('WARP 客户端 UDP 接收观测')),receiveState,receiveCounts,receiveCoverage,E('p',{class:'rp-help'},_('仅观察客户端公网 UDP，已识别隧道与其他公网 UDP 均包含在统计中。目标约 100ms 采样、每秒更新；队列表示内核接收内存占用，峰值为最近约 60 秒的采样值，可能漏掉更短突发。新增丢弃从各连接建立观测基线后累计，不含启动前或漏采段，不是端到端丢包率，也不能单独证明速度波动原因。不会调整缓冲或分流设置。'))]);
  function updateReceive(data,status){
   if(status&&status.backend==='worker'){receiveState.textContent=_('当前使用 TCP 备用线路；此处的 WARP UDP 观测不适用。');receiveCounts.textContent='';receiveCoverage.textContent='';return;}
   var state=feedback.receiveState(data,status,Date.now()),names={unavailable:_('接收观测不可用，不能确认队列或丢包情况。'),stale:_('接收观测已过期或连接已改变，旧数据不能代表当前网络。'),inactive:_('当前没有运行中的隧道观测。'),no_sockets:_('暂未观测到客户端公网 UDP 连接，不能据此认定没有丢包。'),observing:_('正在进行只读接收观测。'),partial:_('正在观测，但存在漏采或计数重置，数据不完整。')};
   receiveState.textContent=names[state];receiveCounts.textContent='';receiveCoverage.textContent='';
   if(state!=='observing'&&state!=='partial')return;
   receiveCounts.textContent=_('当前接收积压：')+(data.queue_now/1048576).toFixed(2)+' MiB · '+_('最近采样峰值：')+(data.peak_queue/1048576).toFixed(2)+' MiB · '+_('本次观测新增丢弃：')+data.observed_drops;
   receiveCoverage.textContent=_('连接数：')+data.socket_count+' · '+_('已识别隧道：')+data.known_ingress_count+' · '+_('其他公网 UDP：')+data.other_public_udp_count+' · '+_('观测开始：')+when(data.started_at)+' · '+_('最近更新：')+when(data.time);
  }
  var pendingApply=null,pendingUpdate=null;
  function when(ts){return ts?new Date(ts*1000).toLocaleString():_('暂无成功记录');}
  function showCounts(parent,counts){parent.replaceChildren();var names={china:_('中国 IP'),china_domains:_('中国域名'),gfw:_('代理域名'),whitelist:_('直连域名')};Object.keys(names).forEach(function(k){parent.appendChild(E('span',{style:'display:inline-block;margin-right:18px'},names[k]+'：'+(counts&&counts[k]!=null?Number(counts[k]).toLocaleString():'—')));});}
  function updateFeedback(v){if(v.state==='running'&&!feedback.updateRunning(v,Date.now()))v=Object.assign({},v,{state:'unknown',phase:_('更新状态已过期，结果未知；可重新更新')});lastRule=v;rulesProgress.textContent=(v.state==='running'?_('更新中 · ')+String(v.percent||0)+'% · ':'')+String(v.phase||_('尚未更新'))+(v.state==='blocked'?' · '+_('候选尚未应用，等待确认'):'');rulesProgress.style.color=v.state==='failed'?'#b42318':v.state==='success'?'#087443':'#2254d1';rulesTime.textContent=_('最后成功更新：')+when(v.last_success_at);showCounts(rulesCounts,v.counts);if(pendingUpdate&&v.request_id===pendingUpdate.nonce){pendingUpdate.box.replaceChildren(E('p',{},rulesProgress.textContent),E('p',{},rulesTime.textContent));var counts=E('div');showCounts(counts,v.counts);pendingUpdate.box.appendChild(counts);if(['success','failed','blocked','unknown'].indexOf(v.state)>=0)pendingUpdate=null;}}
  function submitSettings(update){
   if(saveInFlight)return saveInFlight;
   if(update&&uci.get('routepilot','main','enabled')!=='1'&&uci.get('routepilot','main','preparing')!=='1'){notify(_('规则更新需要主线路已开启或登录环境已就绪；当前仅可浏览已有规则。'),true);return Promise.resolve(false);}
   if(update&&feedback.updateRunning(lastRule,Date.now())){var b=E('div',{},lastRule.phase);pendingUpdate={nonce:lastRule.request_id,box:b};showImokModal(_('规则正在更新'),[b,E('button',{class:'btn',click:ui.hideModal},_('关闭'))]);return Promise.resolve();}
   var nonce=String(Date.now()),box=E('div',{},E('p',{},_('正在提交本插件配置…')));if(update)uci.set('routepilot','main','update_nonce',nonce);else uci.set('routepilot','main','apply_nonce',nonce);
   pendingApply=update?null:{nonce:nonce,box:box,started:Date.now(),update:false};if(update)pendingUpdate={nonce:nonce,box:box};
   showImokModal(update?_('规则更新进度'):_('应用配置'),[box,E('button',{class:'btn',click:function(){ui.hideModal();}},_('关闭'))]);
   saveInFlight=Promise.resolve().then(function(){return update?null:m.save();}).then(function(){return uci.save();}).then(function(){return commitConfig('routepilot');}).then(function(){committedOrganization=uci.get('routepilot','main','organization')||'';delete wizardActions.org;updateWizard();if(!update&&ruleSection==='sources'){Object.keys(sourcePickers).forEach(function(kind){sourcePickers[kind].saved.textContent=_('当前已保存地址：')+String(uci.get('routepilot','main',kind+'_url')||_('未设置'));});rulesInfoAt=0;readRulesInfo(true);}box.textContent=update?_('请求已提交，等待后台开始更新…'):_('配置已提交，正在等待实际应用结果…');return {nonce:nonce};}).catch(function(e){box.textContent=_('未完成：')+e.message;pendingApply=null;pendingUpdate=null;if((center)&&wizardActions.org)failWizardStep('org');return false;}).finally(function(){saveInFlight=null;});return saveInFlight;
  }
  self.applySettings=function(){return submitSettings(false);};self.requestRulesUpdate=function(){return submitSettings(true);};
  var runtimeFiles=['status.json','update.json','connectivity.json','receive.json','dns-state.json','dns-tests.json','backup-node.json','backup-test.json','cloud-state.json','backup-scan.json','relay-tests.json','relay-observed.json','warp-state.json','exit-observation.json','traffic.json','connection-events.json','cloud-directory-state.json'];
  var runtimeSlots={overview:[0,2,14,15],account:[0,12,13],endpoint:[0,13],routing:[0,2,13],devices:[0],dns:[0,2,3,4,5,15],workers:[0,6,7,8,9,10,11,13,15,16],'rules-overview':[0,1],'rules-sources':[0],'rules-manage':[0],'rules-custom':[0],backup:[]}[page]||[];
  function applyRuntimeFiles(files){
   if(!isCurrentView())return;
   if(!feedback.diagnosticValid(files[2]))files[2]=null;
   if(!feedback.dnsTestValid(files[5]))files[5]=null;
   var hasDirectory=page==='workers';if(files[15])updateConnectionHistory(files[15]);if(hasDirectory)updateDirectory(files[16]);wizardStatus=files[0];wizardTest=files[2];
   if(page==='workers'){updateBackup(files[6],files[7],files[0]);updateCloud(files[8]);updateScan(files[9]);updateRelays(files[10],files[11]);}
   if(page==='dns'){updateReceive(files[3],files[0]);updateDNS(files[4],files[5],files[0]);}
   if(center){warpState=files[12]||warpState;exitObservation=files[13]||exitObservation;updateCenter();}
   if(page==='overview'){updateOverview(files[0],files[2]);updateTraffic(files[14]);}
   if(page==='workers'){updateWorkerProgress();updateWorkerFlow();}
   if(hasDirectory&&!cloudDirectoryAuto&&!cloudBusy&&!workerActions.deploy&&cloudMeta.token_saved&&!cloudToken.value&&(!cloudDirectory||cloudDirectory.stale)&&!(cloudState&&['queued','running'].indexOf(cloudState.state)>=0)){cloudDirectoryAuto=true;cloudAction('verify',false);}
   var v=files[0],fresh=feedback.fresh(v,Date.now());if(v){
    if(switchPending){if(fresh&&v.applied_nonce===switchPending.nonce&&v.state===(switchEnabled?'running':'disabled')){switchText.textContent=switchEnabled?_('已开启，正在按保存的范围和规则接管网络。'):_('已关闭，本插件已退出接管；恢复此前的网络方式。');powerButton.disabled=false;switchPending=null;}else if(fresh&&v.state==='error'&&v.attempted_nonce===switchPending.nonce){switchText.textContent=_('开启失败：')+v.message;powerButton.disabled=false;switchPending=null;}else if(Date.now()-switchPending.started>45000){switchText.textContent=_('尚未收到切换确认，请查看实际运行状态。');powerButton.disabled=false;switchPending=null;}}
var exitName=v.backend==='worker'?(v.backup_name||_('Workers 备用')):directoryLabel(v.exit,catalog),message=v.message||v.state;if(!catalog.skipped&&v.state==='preparing'&&message==='登录环境已就绪；请完成邮箱登录，再自动读取出口')message=catalog.available?_('登录环境已就绪，分流尚未开启；已读到 ')+catalog.networks.length+_(' 个账户网络（未验证连通性）'):_('登录环境已就绪；出口目录暂未取得，可重新检查账户，或到分流设置刷新目录');status.textContent=String(message)+' · '+_('出口：')+exitName+' · IP '+String(v.exit_ip||'—')+' · '+(v.scope==='lan'?_('所选局域网'):_('接管设备：')+(Array.isArray(v.devices)?v.devices.length:0));status.style.color=fresh&&v.state==='running'?'#087443':v.state==='error'?'#b42318':'#555';if(!fresh){status.textContent=_('运行状态已过期，无法确认 WARP 或直连是否生效；请检查服务。');status.style.color='#a15c00';}
    if(pendingApply){var p=pendingApply;if(fresh&&v.applied_nonce===p.nonce&&['running','disabled','preparing'].indexOf(v.state)>=0){p.box.textContent=p.update?_('配置已应用，等待规则更新反馈…'):_('已应用成功：')+status.textContent;pendingApply=null;}else if(fresh&&v.state==='error'&&v.attempted_nonce===p.nonce){p.box.textContent=_('应用失败：')+v.message;pendingApply=null;pendingUpdate=null;}else if(Date.now()-p.started>45000){p.box.textContent=_('已提交，但尚未收到应用确认。请检查页面顶部的实际运行状态。');pendingApply=null;}}
   }else status.textContent=_('尚无运行状态，未能确认服务是否生效。');
   if(ruleSection==='overview'&&files[1])updateFeedback(files[1]);else rulesProgress.textContent=_('尚无更新记录；点击“规则与更新 → 立即更新规则”查看完整反馈。');
   var t=files[2];if(t){var detecting=feedback.diagnosticRunning(t,Date.now()),staleTest=feedback.diagnosticStale(t,v,Date.now())||((center)&&(v&&v.applied_nonce!==wizardConfig().nonce||v&&v.backend!=='worker'&&v.exit!==configuredExit()||t.state==='done'&&(!Number.isFinite(Number(t.finished_at))||Date.now()/1000-Number(t.finished_at)>300))); testButton.disabled=detecting||((center)&&(!feedback.fresh(v,Date.now())||v.state!=='running'));testsInfo.textContent=detecting?_('正在检测，最长约 10 秒…'):((t.state==='running'?_('检测已中断或超时，结果未知；可以重新检测。'):_('检测时间：')+when(t.finished_at))+' · '+_('代理出口：')+String(t.exit_ip||'—')+(staleTest?' · '+_('历史结果已过期，不能代表当前网络，请重新检测'):''));(t.results||[]).forEach(function(r){var c=testCells[r.id];if(!c)return;c.result.textContent=(staleTest?_('历史：'):'')+(r.message||_('等待检测'));c.result.style.color=staleTest?'#777':r.state==='ok'?'#087443':r.state==='pending'?'#2254d1':r.state==='restricted'?'#a15c00':'#b42318';c.delay.textContent=r.ms!=null?r.ms+' ms':'—';});}else{testButton.disabled=!!((center)&&(!feedback.fresh(v,Date.now())||v.state!=='running'));testsInfo.textContent=_('暂未取得有效的网站检测结果，请重新检测。');Object.keys(testCells).forEach(function(k){testCells[k].result.textContent=_('未检测');testCells[k].result.style.color='';testCells[k].delay.textContent='—';});}
   if(page==='account'||page==='routing'){renderDirectory();}
   if(page==='endpoint')return pollEndpoint();
   if(ruleDataPage)return readRulesInfo(false);
  }
  function readRuntimeFiles(){return Promise.all(runtimeFiles.map(function(file,index){return runtimeSlots.indexOf(index)>=0?fs.read('/var/run/routepilot/'+file).then(JSON.parse).catch(function(){return null;}):Promise.resolve(null);})).then(applyRuntimeFiles);}
  var initialOverviewAt=0,initialOverviewRead=data.initialOverviewRead&&data.initialOverviewRead.then(function(files){if(!isCurrentView())return;if(feedback.fresh(files[0],Date.now()))initialOverviewAt=Date.now();return applyRuntimeFiles(files);});
  poll.add(function(){
   if(!isCurrentView())return Promise.resolve();
   if(initialOverviewRead){var first=initialOverviewRead;initialOverviewRead=null;return first.then(function(){if(!isCurrentView()||Date.now()-initialOverviewAt<2000)return;return readRuntimeFiles();});}
   return readRuntimeFiles();
  },2);
  function requestChecksum(text){
   var bytes=new TextEncoder().encode(text),size=(bytes.length+72)&~63,padded=new Uint8Array(size);padded.set(bytes);padded[bytes.length]=128;new DataView(padded.buffer).setUint32(size-4,bytes.length*8);
   var h=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19],k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
   function r(x,n){return x>>>n|x<<32-n;}var words=new Int32Array(64),view=new DataView(padded.buffer);
   for(var off=0;off<size;off+=64){for(var i=0;i<16;i++)words[i]=view.getInt32(off+i*4);for(i=16;i<64;i++){var x=words[i-15],y=words[i-2];words[i]=(words[i-16]+(r(x,7)^r(x,18)^x>>>3)+words[i-7]+(r(y,17)^r(y,19)^y>>>10))|0;}
    var a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],v=h[7];for(i=0;i<64;i++){var t=(v+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+k[i]+words[i])|0,u=((r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c)))|0;v=g;g=f;f=e;e=d+t|0;d=c;c=b;b=a;a=t+u|0;}[a,b,c,d,e,f,g,v].forEach(function(value,n){h[n]=h[n]+value|0;});
   }return h.map(function(value){return (value>>>0).toString(16).padStart(8,'0');}).join('');
  }
  function requestID(){if(!window.crypto||!window.crypto.getRandomValues)throw Error(_('浏览器不支持安全请求标识，请更新浏览器后重试'));var words=new Uint32Array(4);window.crypto.getRandomValues(words);return Array.prototype.map.call(words,function(v){return v.toString(16).padStart(8,'0');}).join('');}
  function boundRequest(payload){var id=requestID(),text=JSON.stringify({request_id:id,payload:payload});return {id:id,text:text,token:id+':'+requestChecksum(text)};}
  function backup(){return command('settings-export').then(function(raw){var b=new Blob([raw],{type:'application/json'}),u=URL.createObjectURL(b);E('a',{href:u,download:'routepilot-settings-v3.json'}).click();URL.revokeObjectURL(u);}).catch(function(e){notify(_('导出未完成：')+e.message,true);});}
  function restore(file){
   if(!file||file.size>262144){notify(_('备份文件过大或无效'),true);return Promise.resolve();}
   var importRequest;return file.text().then(function(raw){var b=JSON.parse(raw);if(['routepilot-masque-v2','routepilot-masque-v3'].indexOf(b.format)<0)throw Error(_('备份格式无效'));importRequest=boundRequest(b);return fs.write('/var/run/routepilot/settings-import-input',importRequest.text,384);}).then(function(){return command('settings-import',importRequest.token);}).then(function(raw){var result=JSON.parse(raw);if(!result.imported||result.request_id!==importRequest.id)throw Error(_('未收到本次导入确认'));return uci.unload('routepilot');}).then(function(){return uci.load('routepilot');}).then(function(){notify(_('已导入并保持关闭。请刷新页面；入口偏好需核对后单独保存应用，登录和备用凭据需另行配置。'));}).catch(function(e){notify(_('导入未完成：')+e.message,true);});
  }
  var backupCard=E('div',{class:'imok-settings-pair imok-backup-layout'},[panel(_('导出配置'),_('v3 普通设置备份，不是整机镜像。'),[E('p',{},_('包含账户组织名称、协议偏好、出口与接管范围、设备策略、DNS、规则来源与更新间隔、自定义分流和主线路入口偏好。导入后保持关闭并默认选主线路，入口偏好待核对后单独应用。')),E('button',{class:'btn',click:backup},_('导出配置')),E('p',{class:'rp-help'},_('常规配置导出不包含“规则管理”中的个人新增和排除，也不包含 WARP 更新检查频率、运行环境、已下载规则或历史统计。路由器登录身份、API 令牌、备用节点凭据、客户端独立身份及私密订阅凭据也不随本文件迁移。')),]),panel(_('导入与恢复'),_('选择文件后按原流程立即导入，导入完成后保持关闭。'),[E('label',{class:'imok-import-field'},[_('导入配置文件：'),E('input',{type:'file',accept:'.json',change:function(ev){restore(ev.target.files[0]);}})]),E('p',{},_('导入后保持关闭，不修改原 SSR 节点或分流。')),E('p',{class:'rp-help'},_('支持 v2/v3 JSON，文件上限 256 KiB。导出前会检查可导入性，超过上限会明确报错，不截断设置。'))])]);
  function saveOrganizationOnly(){var org=organizationOption.formvalue('main');if(org==null)org=uci.get('routepilot','main','organization')||'';return submitInput('organization-input','warp-org-save',{organization:String(org).trim()}).then(function(result){uci.set('routepilot','main','organization',result.organization);uci.set('routepilot','main','apply_nonce',result.nonce);committedOrganization=result.organization;delete wizardActions.org;updateWizard();return result;});}
  function countryName(code){return ({US:'美国',KR:'韩国',JP:'日本',SG:'新加坡',HK:'香港',TW:'台湾',DE:'德国',GB:'英国',CN:'中国',AU:'澳大利亚',CA:'加拿大'})[code]||code||_('未知');}
  var centerState=E('div',{role:'status','aria-live':'polite'}),centerCountry=E('span',{class:'rp-summary-tag',role:'status','aria-live':'polite'}),centerWarpAuth=E('span',{class:'rp-summary-tag'}),centerAPIAuth=E('span',{class:'rp-summary-tag'}),centerAuth=E('div',{class:'rp-auth-badges'},[centerWarpAuth,centerAPIAuth,centerCountry]),warpInfo=E('p',{role:'status','aria-live':'polite'});
  var centerReadBusy=false;
  function observationCurrent(row,st){return row&&st&&row.state==='done'&&feedback.fresh(st,Date.now())&&st.state==='running'&&['runtime_id','generation','applied_nonce','exit','backend','endpoint_generation'].every(function(k){return row[k]===st[k];})&&Date.now()/1000-row.time>=-5&&Date.now()/1000-row.time<=300;}
  function updateCenter(){
   var st=wizardStatus,fresh=feedback.fresh(st,Date.now()),running=fresh&&st&&st.state==='running';
   centerState.textContent=running?((st.backend==='worker'?_('当前使用 Workers 备用'):_('当前使用 WARP 主线路'))+' · '+_('实际协议：')+(st.protocol||_('尚未确认'))+(st.protocol_warning?_('（上次确认）'):'')+' · '+_('出口：')+(st.backend==='worker'?(st.backup_name||_('Workers 备用')):(catalog.available?directoryLabel(st.exit,catalog):String(st.exit||_('待确认'))))+' · IP '+(st.exit_ip||'—')):!fresh?_('○ 状态过期，当前连接未确认'):st&&st.state==='disabled'?_('○ 已关闭'):st&&st.message||_('○ 尚无运行状态');
   centerState.className=running?'rp-center-ok':'rp-help';
   var valid=observationCurrent(exitObservation,st);
   centerCountry.textContent=valid?_('本次观测出口地区：')+countryName(exitObservation.country)+' ('+exitObservation.country+')':exitObservation?_('○ 地区观测已过期或线路改变，可重新检测'):_('○ 出口地区尚未检测');
   var cfg=cloudState&&cloudState.configured!=null?cloudState:cloudMeta;
   centerWarpAuth.textContent=catalog.available||registeredHere||running&&st.backend!=='worker'?_('✓ WARP 已注册'):catalog.skipped?_('○ 本页未读取 WARP 注册状态'):_('○ WARP 注册待确认');
   centerAPIAuth.textContent=cfg&&cfg.configured?_('✓ API 令牌已保存'):_('○ API 尚未读取');
   warpInfo.textContent=warpState&&warpState.state==='available'?_('读取到的客户端协议：')+warpState.protocol+' · '+({organization:_('组织策略'),local_policy:_('本机管理配置'),local:_('客户端设置'),mdm:_('本机管理文件'),routepilot:_('本路由器配置'),external_mdm:_('外部管理文件')}[warpState.source]||_('来源待确认'))+' · '+warpState.message:_('尚未读取到主线路协议信息；新安装先完成本组登录。');
   var labels={advanced:catalog.available?_('账户可读取'):_('账户待确认'),basic:running?_('已生效'):_('待确认'),devices:uci.get('routepilot','main','scope')==='lan'?_('当前接管范围为「局域网」，此处策略仅在切换到「按设备选择」后生效。'):_('逐台选择分流模式，启用的设备共用所选代理出口。'),dns:liveDNS&&feedback.dnsState(liveDNS,st,Date.now())==='running'?_('DNS 运行中'):_('DNS 待确认'),workers:running&&st.backend==='worker'?_('备用运行中'):backupNode&&backupNode.configured?_('已配置，未接管'):_('未配置')};
   Object.keys(centerGroupBadges||{}).forEach(function(k){centerGroupBadges[k].textContent=labels[k]||'';});
   centerRefresh.disabled=centerReadBusy;centerObserve.disabled=centerReadBusy||!running;
  }
  function centerRead(action){centerReadBusy=true;updateCenter();return command(action).then(function(raw){var result=JSON.parse(raw);if(!isCurrentView())return;if(action==='warp-info')warpState=result;else exitObservation=result;}).catch(function(e){notify(e.message,true);}).finally(function(){centerReadBusy=false;updateCenter();});}
  var centerRefresh=E('button',{class:'btn',click:function(){return centerRead('warp-info');}},_('刷新协议信息（只读）'));
  var centerObserve=E('button',{class:'btn',click:function(){return centerRead('exit-observe');}},_('检测实际出口国家 / 地区（不切换）'));
  var centerGroups={},centerGroupBadges={};
  workerInventory=E('section',{class:'rp-worker-inventory','aria-label':_('Workers 节点与部署记录')});
  function openWorkerStage(key){Object.keys(workerStages).forEach(function(k){workerStages[k].setAttribute('data-current',k===key?'true':'false');if(workerStageLinks[k])workerStageLinks[k].setAttribute('aria-pressed',k===key?'true':'false');});}
  function workerWorkspaceView(stages){
   var names={deploy:_('部署与授权'),relay:_('PROXYIP 候选池'),scan:_('入口优选'),use:_('导入、检测与启用')};
   var nav=E('nav',{class:'imok-worker-nav','aria-label':_('Workers 阶段选择')},Object.keys(names).map(function(key){var state=workerStageStates[key]=E('small',{},_('正在读取…'));return workerStageLinks[key]=E('button',{class:'btn imok-stage-link',type:'button','aria-controls':'imok-worker-'+key,'aria-pressed':'false',click:function(){workerPinned=true;openWorkerStage(key);}},[E('span',{},names[key]),state]);}));
   var showAll=E('button',{class:'btn',type:'button','aria-pressed':'false',click:function(){workerPinned=true;var all=showAll.getAttribute?showAll.getAttribute('aria-pressed')!=='true':false;showAll.setAttribute('aria-pressed',all?'true':'false');workerWorkspace.setAttribute('data-show-all',all?'true':'false');showAll.textContent=all?_('只显示当前阶段'):_('展开全部设置');}},_('展开全部设置'));
   nav.appendChild(showAll);workerWorkspace=E('div',{class:'imok-worker-workspace','data-show-all':'false'},[nav,E('div',{class:'imok-worker-content'},stages)]);return workerWorkspace;
  }

  function workerPanel(key){if(!workerPanels[key])workerPanels[key]=E('div',{class:'rp-operation-feedback',role:'status','aria-live':'polite'});return workerPanels[key];}
  function workerOperationBusy(key){var op=workerActions[key];return !!(op&&(op.submitting||op.pending||op.waitingSwitch));}
  function startWorkerOperation(key,title,kind,action,job){workerActions[key]={title:title,kind:kind,action:action,started:Date.now(),submitting:true,pending:null,baseline:feedback.workerJobKey(job)};updateWorkerProgress();}
  function acceptWorkerOperation(key,result,async){var op=workerActions[key];if(!op)return;op.submitting=false;op.finished=Date.now();if(async&&result&&result.state==='submitted'){op.pending={started:op.started,baseline:op.baseline,action:op.action};}else if(async&&result&&result.state==='running'){op.result={state:'waiting',message:_('已有后台任务正在运行；本次没有重复创建或检测。请等待下方实际状态更新。')};}else if(async){op.result={state:'unknown',message:_('提交响应未给出明确任务状态，请只读检查结果；不能据此确认创建成功。')};}else{op.result={state:'success',message:result&&result.message||_('✓ 操作已完成并保存。')};}updateWorkerProgress();}
  function failWorkerOperation(key,message,state){var op=workerActions[key]||{};op.submitting=false;op.waitingSwitch=false;op.pending=null;op.finished=Date.now();op.result={state:state||'error',message:message};workerActions[key]=op;updateWorkerProgress();}
  function refreshWorkerMetadata(){return fs.exec('/usr/libexec/routepilot-backup-list',[]).then(function(r){if(r.code)throw Error(_('保存操作已完成，但节点列表读取失败；请刷新页面确认。'));var next=JSON.parse(r.stdout);if(next.cloud){cloudMeta=next.cloud;workerProjectRows=next.cloud.projects||[];workerProjectsAt=Number(next.cloud.projects_read_at||0);}if(next.node){backupNode=next.node;updateBackup(next.node,backupTest,wizardStatus);}updateWorkerProgress();});}
  function progressText(key,state,job,kind){
   var elapsed=_('已等待 ')+state.elapsed+_(' 秒'),count=state.total?_('已完成 ')+state.completed+'/'+state.total:'';
   if(state.state==='busy')return (state.phase==='awaiting'?_('请求已提交，正在等待后台接收确认'):state.message||_('后台正在处理'))+' · '+(count?count+' · ':'')+elapsed;
   if(state.state==='unknown')return _('较长时间未收到完成确认，当前结果未知；请只读检查状态，避免反复创建。')+' · '+elapsed;
   if(state.state==='error')return '✕ '+(state.message||_('操作未完成；请检查具体原因后重试。'));
   if(state.state==='waiting')return '! '+(state.message||_('仍在等待 DNS 或证书生效；稍后只读检查，无需重复创建。'));
   if(state.state==='warning')return '! '+(state.message||_('发现冲突或访问受限，请核对后再继续。'));
   if(key==='deploy'&&state.state==='success')return job&&job.state==='ready'?_('✓ Worker 与域名 HTTPS 已确认就绪；这不等于备用线路已经启用。'):job&&job.state==='verified'?_('✓ 账户和根域名读取完成；尚未因此创建节点。'):job&&job.state==='available'?_('✓ 名称和子域名占用检查通过；尚未创建节点。'):_('✓ 操作完成。');
   if(key==='relay'&&job&&job.state==='done'){
    var rows=job.results||[],passed=rows.filter(function(r){return r.state==='ok';}).length,enabled=relayValues().filter(function(r){return r.enabled;}),current=!relayDirty&&enabled.length&&enabled.every(function(r){var proof=relayRowProof(r.address);return relayCurrent(proof)&&proof.state==='ok'&&proof.forced===true;});
    state.state=current?'success':'warning';return (current?'✓ ':'! ')+_('逐项检测结束：通过 ')+passed+'/'+rows.length+(current?_('；请点击“应用已通过的主备顺序”，检测不会自动保存应用。'):_('；结果已过期、节点 / 候选改变或有项目未通过，请查看逐项结果。'));
   }
   if(key==='scan'&&job&&job.state==='done'){
    var age=Date.now()/1000-Number(job.finished_at),current=backupNode&&age>=-5&&age<=300&&(job.fingerprint===backupNode.fingerprint||job.applied_fingerprint===backupNode.fingerprint);
    state.state=current&&job.best_ip?'success':'warning';return current&&job.best_ip?'✓ '+_('本轮推荐入口：')+job.best_ip+(job.applied_fingerprint===backupNode.fingerprint?_('；已保存，下一步独立检测。'):_('；尚未应用。可应用推荐入口，也可保留原入口进入下一步。')):'! '+_('没有当前有效的推荐结果；历史结果或没有通过的入口不能当作已完成优化。');
   }
   if(key==='use'&&job&&job.state==='done'){
    state.state=workerFlow.tested?'success':'warning';return workerFlow.tested?'✓ '+_('当前节点基础联网检测通过，可手动启用；检测不会切换现有主线路。'):'! '+_('独立检测已结束，但未通过、已过期或节点已变化；当前不能据此启用备用。');
   }
   return state.message||_('尚未开始；执行操作后会在这里显示接收确认、真实进度和结果。');
  }
  function updateWorkerInventory(){
   if(!workerInventory)return;var node=backupNode||(data[5]&&data[5].node),st=wizardStatus,running=feedback.fresh(st,Date.now())&&st.state==='running'&&st.backend==='worker';
   var signature=JSON.stringify([node,workerProjectRows,running,workerFlow.tested]);if(signature===workerInventory._signature)return;workerInventory._signature=signature;
   var saved=E('div',{class:'rp-node-summary'},[E('h4',{},_('当前保存的备用节点')),E('p',{},node&&node.configured?'✓ '+_('已保存到路由器 · ')+(node.name||node.sni||_('备用节点')):_('○ 还没有保存备用节点；创建就绪后需点“将新部署设为备用节点”，或在“备用节点设置”导入已有节点。'))]);
   if(node&&node.configured){saved.appendChild(E('p',{},_('验证域名：')+node.sni+' · '+_('连接入口：')+node.address+':'+node.port));saved.appendChild(E('p',{},running?_('✓ 备用正在运行'):st&&st.backend==='worker'?_('! 已选择备用，实际运行状态待确认'):_('○ 备用未启用，现有主线路仍在使用')));saved.appendChild(E('p',{class:'rp-help'},workerFlow.tested?_('✓ 当前节点有有效的独立检测结果'):_('○ 当前节点未检测、结果已过期或节点已改变；请到“独立测试与手动启用”重新检测')));}
   saved.appendChild(E('button',{class:'btn',click:function(){workerPinned=true;openWorkerStage('use');}},_('查看保存节点 / 进入独立检测')));
   var created=E('div',{class:'rp-created-nodes'},[E('h4',{},_('本路由器创建的云端节点'))]);
   if(!workerProjectRows.length)created.appendChild(E('p',{class:'rp-help'},_('暂无本路由器创建记录。已有导入节点仍显示在上方；授权或读取域名不会自动创建节点。')));
   workerProjectRows.forEach(function(row){created.appendChild(E('div',{class:'rp-created-node'},[E('strong',{},row.script),E('span',{},row.hostname),E('span',{class:'rp-help'},(row.uploaded?_('✓ Worker 已上传'):_('○ 已预留，上传待确认'))+' · '+(row.bound?_('✓ 域名已绑定'):_('○ 域名尚未确认'))+' · '+(row.saved_as_backup?_('✓ 已保存为当前备用'):_('○ 未保存为当前备用')))]));});
   created.appendChild(E('p',{class:'rp-help'},_('云端项目来自本机部署记录，不代表当前都能联网。创建、绑定、保存为备用、手动启用是四种不同状态。')));
   workerInventory.replaceChildren(E('h3',{},_('备用节点与云端部署记录')),workerActivity,saved,created);
  }
  function updateWorkerProgress(){
   if(!workerInventory)return;
   var jobs={deploy:cloudState||cloudDirectoryJob,relay:relayTest,scan:scanState,use:backupTest};
   ['deploy','relay','scan','use'].forEach(function(key){
    var op=workerActions[key],job=op&&op.completedJob|| (op&&op.kind==='directory'?cloudDirectoryJob:jobs[key]),kind=op?op.kind:key==='deploy'?'cloud':key==='use'?'backup':key;
    var display=op&&op.submitting?{state:'busy',phase:'submitting',elapsed:Math.max(0,Math.floor((Date.now()-op.started)/1000)),completed:0,total:0,message:_('正在提交操作，请勿重复点击…')}:op&&op.result?Object.assign({elapsed:0,completed:0,total:0},op.result):feedback.workerJobProgress(job,op&&op.pending,Date.now(),kind,key==='use'?5:0);
    var message=op&&op.result?display.message:progressText(key,display,job,kind);
    if(op&&op.pending&&display.matched&&['busy','unknown'].indexOf(display.state)<0){op.pending=null;op.completedJob=job;}
    if(key==='deploy'&&workerFlow.cloudDirty&&display.state==='success'&&!(op&&op.result)){display.state='warning';message=_('这是上次保存目标的结果；表单有未保存编辑，不能代表新目标已经创建。');}
    if(op&&op.waitingSwitch){display.state='busy';display.elapsed=Math.max(0,Math.floor((Date.now()-op.started)/1000));message=_('切换请求已提交，正在等待实际运行状态确认…')+' · '+_('已等待 ')+display.elapsed+_(' 秒');}
    if(op&&op.pending&&display.state==='unknown'){op.pending=null;op.result={state:'unknown',message:message,elapsed:display.elapsed};}
    if(op)op.display=display;workerDisplays[key]=display;workerMessages[key]=message;
    var panel=workerPanel(key),title=op&&op.title||({deploy:_('部署与授权操作结果'),relay:_('PROXYIP 检测 / 应用结果'),scan:_('入口优选结果'),use:_('备用节点独立检测结果')})[key],stamp=op&&op.finished&&!workerOperationBusy(key)?when(op.finished/1000):job&&job.finished_at&&!workerOperationBusy(key)?when(job.finished_at):'',signature=JSON.stringify([title,display,message,stamp,workerFlow.tested]);
    if(panel._signature===signature)return;panel._signature=signature;panel.setAttribute('data-state',display.state);panel.setAttribute('aria-busy',display.state==='busy'?'true':'false');
    var children=[E('strong',{},title),E('p',{},message)];if(display.total)children.push(E('progress',{max:display.total,value:Math.min(display.completed,display.total),'aria-label':_('实际已完成项目')}));
    if(key==='deploy'&&job&&['deploy','check'].indexOf(job.action)>=0&&!(op&&op.kind==='directory')&&!(op&&op.result)){
     var waitingForNew=!!(op&&(op.submitting||op.pending)&&!display.matched);var steps=[_('项目归属与占用'),_('Worker 上传 / 存在'),_('子域名绑定'),_('DNS 与 HTTPS 证书')],index={connecting:0,checking:0,uploading:1,binding:2,waiting_dns:3}[job.phase],ready=job.state==='ready'&&!workerFlow.cloudDirty&&!waitingForNew;if(waitingForNew)index=-1;else if(job.state==='waiting_dns')index=3;children.push(E('ol',{class:'rp-job-steps'},steps.map(function(label,i){var tone=ready?'success':Number.isFinite(index)&&i<index?'success':i===index?display.state==='error'?'error':'busy':'pending';return E('li',{'data-state':tone},(tone==='success'?'✓ ':tone==='error'?'✕ ':tone==='busy'?'… ':'○ ')+label+(i===index&&display.state==='busy'?_(' · 正在处理'):''));})));
    }
    if(stamp)children.push(E('small',{},_('结果记录时间：')+stamp));
    if(['success','warning','waiting'].indexOf(display.state)>=0){var next=key==='deploy'?backupNode&&backupNode.configured&&backupNode.sni===cloudHost.value?'relay':'deploy':key==='relay'?'relay':key==='scan'?'use':'use',text=key==='deploy'?next==='relay'?_('下一步：配置 / 检测 PROXYIP'):_('前往创建部署，检查或保存为备用节点'):key==='relay'?relayApply&&!relayApply.disabled?_('下一步：应用已通过的主备顺序'):_('查看候选与逐项结果'):key==='scan'?_('保留当前入口，前往独立检测'):_('查看保存节点与检测结果');children.push(E('button',{class:'btn',click:function(){workerPinned=true;openWorkerStage(next);}},text));}
    panel.replaceChildren.apply(panel,children);
   });updateWorkerInventory();
   var keys=['deploy','relay','scan','use'],recent=keys.filter(function(k){return workerActions[k];}).sort(function(a,b){return workerActions[b].started-workerActions[a].started;})[0]||keys.filter(function(k){return jobs[k]&&jobs[k].started_at;}).sort(function(a,b){return Number(jobs[b].started_at)-Number(jobs[a].started_at);})[0];
   if(recent){var d=workerDisplays[recent],a=workerActions[recent],title=({deploy:_('部署与授权'),relay:_('PROXYIP'),scan:_('优选入口'),use:_('保存与独立检测')})[recent],text=(a?a.title:title)+' · '+workerMessages[recent],signature=JSON.stringify([recent,d&&d.state,text]);if(workerActivity._signature!==signature){workerActivity._signature=signature;workerActivity.setAttribute('data-state',d&&d.state||'idle');workerActivity.replaceChildren(E('strong',{},_('最近操作结果')),E('p',{},text));}}
  }
  function workerStage(key,title,card){var heading=E('h3',{class:'rp-worker-stage-heading'},[E('span',{},title),workerBadges[key]=E('span',{class:'rp-group-state'})]),box=E('section',{class:'rp-worker-stage',id:'imok-worker-'+key,'data-stage':key},[heading,E('div',{class:'rp-worker-stage-body'},card)]);workerStages[key]=box;return box;}
  function workerActionStage(key){workerPinned=true;openWorkerStage(key);}
  function updateWorkerFlow(){
   if(!Object.keys(workerStages).length)return;
   var node=backupNode||(data[5]&&data[5].node),configured=!!(node&&node.configured),now=Date.now()/1000;
   var imported=configured&&!(cloudMeta.configured||cloudState&&cloudState.configured),deployed=!!workerFlow.deployed;
   var relayApplied=configured&&!relayDirty&&!!relaySavedHash&&node.relay_order_hash===relaySavedHash;
   var scanCurrent=scanState&&scanState.state==='done'&&configured&&now-Number(scanState.finished_at)>=-5&&now-Number(scanState.finished_at)<=300&&(scanState.fingerprint===node.fingerprint||scanState.applied_fingerprint===node.fingerprint);
   var relayEnabled=relayValues().filter(function(r){return r.enabled;}),relayPassed=!relayDirty&&relayTest&&relayTest.state==='done'&&relayEnabled.length&&relayEnabled.every(function(r){var proof=relayRowProof(r.address);return relayCurrent(proof)&&proof.state==='ok'&&proof.forced===true;});
   var scanned=!!(scanCurrent&&scanState.best_ip),scanApplied=!!(scanned&&scanState.applied_fingerprint===node.fingerprint),tested=!!workerFlow.tested;
   var states={deploy:imported?_('已有导入节点'):deployed&&configured&&node.sni===cloudHost.value?_('✓ 已部署并设为备用'):deployed?_('部署就绪，待设为备用'):_('○ 待部署 / 检查'),relay:relayDirty?_('有未保存编辑'):relayApplied?_('✓ 顺序已应用'):relayPassed?_('✓ 检测通过，待应用顺序'):_('○ 待检测 / 应用'),scan:scanApplied?_('✓ 已应用，待整线检测'):scanned?_('✓ 推荐可用，待应用'):_('○ 可选：保留原入口'),use:tested?_('✓ 检测有效，可手动启用'):_('○ 待独立检测')};
   Object.keys(workerBadges).forEach(function(k){var op=workerActions[k],d=workerDisplays[k];workerBadges[k].textContent=d&&d.state==='busy'?'… '+(op&&op.title||_('后台正在处理'))+' · '+_('已等待 ')+d.elapsed+'s':d&&d.state==='error'?_('✕ 操作失败，查看本项结果'):d&&d.state==='unknown'?_('! 结果未确认，查看本项提示'):states[k];if(workerStageStates[k])workerStageStates[k].textContent=workerBadges[k].textContent;workerStages[k].setAttribute('data-state',d&&['busy','error','unknown','waiting'].indexOf(d.state)>=0?d.state:states[k].indexOf('✓')===0?'success':'pending');});
   if(workerPinned)return;
   var cloudRunning=cloudBusy||workerOperationBusy('deploy')||cloudState&&['queued','running'].indexOf(cloudState.state)>=0&&now-Number(cloudState.started_at)<300;
   var scanRunning=scanBusy||workerOperationBusy('scan')||scanState&&['queued','running'].indexOf(scanState.state)>=0&&now-Number(scanState.started_at)<300;
   var key=cloudRunning?'deploy':relayBusy||workerOperationBusy('relay')||relayTaskRunning()?'relay':scanRunning?'scan':workerFlow.testing||tested?'use':!configured||!imported&&!(deployed&&node.sni===cloudHost.value)?'deploy':!relayApplied?'relay':!scanned?'scan':scanApplied?'use':'scan';
   openWorkerStage(key);
  }
  function centerGroup(id,title,description,children){
   var state=centerGroupBadges[id]=E('span',{class:'rp-group-state'}),body=[E('p',{class:'rp-group-status'},state)];if(description)body.push(E('p',{class:'rp-help'},description));
   var box=E('section',{class:'rp-center-group imok-settings-section',id:'rp-center-'+id,'aria-label':title},E('div',{class:'rp-center-body'},body.concat(children.filter(Boolean))));
   centerGroups[id]=box;return box;
  }
  var endpointLoaded=false,endpointBusy=false,endpointDirty=false,endpointView=null,endpointTracking=null,recoveryTracking=null;
  var endpointMode=E('select',{class:'cbi-input-select'},[E('option',{value:'auto'},_('默认：官方自动')),E('option',{value:'candidate'},_('固定：从官方候选选择')),E('option',{value:'custom'},_('手写：自定义 IPv4 入口'))]);
  var endpointAddress=E('input',{class:'cbi-input-text',type:'text',placeholder:'IPv4:端口'});
  var endpointHours=E('select',{class:'cbi-input-select'},[0,6,12,24,72,168].map(function(h){return E('option',{value:String(h)},h?String(h)+_(' 小时'):_('关闭定期读取'));}));
  var endpointCandidates=E('select',{class:'cbi-input-select'},[E('option',{value:''},_('读取后选择候选'))]);
  var endpointResult=E('p',{'aria-live':'polite'},_('正在读取入口设置…'));
  var endpointDetails=E('div',{}),endpointModeHelp=E('p',{class:'rp-help','aria-live':'polite'});
  var endpointCandidateField=E('label',{class:'rp-cloud-field'},[E('span',{},_('固定候选入口（尚未逐项测速）')),endpointCandidates]);
  var endpointCustomField=E('label',{class:'rp-cloud-field'},[E('span',{},_('手写 IPv4:端口（自定义未经检测）')),endpointAddress]);
  function updateEndpointMode(){
   var mode=endpointMode.value;
   endpointCandidateField.hidden=mode!=='candidate';endpointCandidates.disabled=mode!=='candidate';
   endpointCustomField.hidden=mode!=='custom';endpointAddress.disabled=mode!=='custom';
   endpointModeHelp.textContent=mode==='candidate'?_('选择官方客户端本地候选中的一个 IPv4 入口。应用并检测后固定使用；失败将尝试恢复并验证原入口。定期同步候选不会自动更换固定入口。'):mode==='custom'?_('填写公网 IPv4:端口，例如 162.159.197.2:8443。应用并检测后固定使用；自定义地址不保证可用，失败将尝试恢复并验证原入口。'):_('无需选择或填写 IP。应用并检测后清除现有固定入口，由官方客户端管理入口；失败将尝试恢复并验证原设置。');
   endpointModeHelp.textContent+=_(' 仅切换选项不会生效；点击“应用并检测”会短暂重连，核对隧道连接和原预期出口，不代表测速或手机端验收。');
  }
  [endpointMode,endpointAddress,endpointHours].forEach(function(el){el.addEventListener('change',function(){endpointDirty=true;updateEndpointMode();});});
  endpointAddress.addEventListener('input',function(){endpointDirty=true;});
  endpointCandidates.addEventListener('change',function(){endpointDirty=true;updateEndpointMode();});
  function showEndpoint(v){
   endpointView=v;
   var restoreFields=!endpointLoaded||!endpointDirty,preferred=restoreFields?(v.settings.mode==='manual'?v.settings.address:v.settings.mode==='preserve'&&v.override_observed?v.configured_override:''):endpointCandidates.value;
   if(restoreFields){endpointMode.value=preferred?((v.candidates&&v.candidates.entries||[]).indexOf(preferred)>=0?'candidate':'custom'):'auto';endpointAddress.value=preferred||'';endpointHours.value=String(v.settings.hours);}
   endpointLoaded=true;updateEndpointMode();
   var c=v.candidates||{},r=v.collection||{},a=v.application||{},available=v.capability&&v.capability.supported;
   endpointCandidates.replaceChildren(E('option',{value:''},_('选择候选（尚未逐项测速）')));
   (c.entries||[]).forEach(function(addr){endpointCandidates.appendChild(E('option',{value:addr,disabled:addr.charAt(0)==='['?true:null},addr+(addr.charAt(0)==='['?_(' · 当前环境不支持 IPv6'):'')));});
   endpointCandidates.value=(c.entries||[]).indexOf(preferred)>=0?preferred:'';
   endpointDetails.replaceChildren(E('p',{},_('客户端配置的固定入口：')+(v.override_observed?(v.configured_override||_('未固定，交给官方客户端')):_('暂不能确认'))),E('p',{},_('配置入口不等于实际远端观测；自定义地址未经检测。')),E('p',{},_('最近读取：')+(r.checked_at?new Date(r.checked_at*1000).toLocaleString():_('尚未读取'))+' · '+(r.message||_('默认每24小时读取客户端本地候选'))),E('p',{},_('最近成功：')+(c.collected_at?new Date(c.collected_at*1000).toLocaleString():_('无'))+' · '+_('源配置时间：')+(c.source_mtime?new Date(c.source_mtime*1000).toLocaleString():_('未知'))),E('p',{},available?_('当前客户端支持入口 set/reset。'):_('客户端能力尚未确认，暂不能应用入口。')));
   var message=v.settings.staged?_('导入的入口偏好尚未应用，请核对后单独保存。'):v.pending?((wizardStatus&&wizardStatus.state==='disabled')?_('设置已保存，等待主线路开启后由后台应用。'):_('设置已保存，等待后台处理本次请求…')):a.state==='applied'?_('入口设置已确认。')+(a.connected?_(' 已完成连接检测；'):'')+(a.exit_verified?_('原预期出口验证通过。'):_('请另行检测实际出口。')):a.state==='rejected'?_('应用前校验未通过；当前连接未改变，请核对能力和原出口基线。'):a.state==='verifying'?_('正在连接并验证原出口…'):a.state==='rolling_back'?_('应用未通过，正在恢复并验证原入口…'):a.state==='unknown'?_('恢复结果未知，可能直连；请人工检查，不会继续自动切换。'):a.state==='failed'?_('入口应用失败；')+(a.rollback_confirmed?_('已恢复原入口设置。'):_('恢复未确认，请手动检查。')):a.state==='applying'?_('入口事务正在处理或结果未确认；请读取状态，必要时人工核对。'):_('保持现有入口。');
   if(endpointTracking){
    if(v.settings.revision!==endpointTracking.revision){message=_('本次请求已被另一个设置请求替代，请核对当前结果。');endpointTracking=null;}
    else if(a.revision===endpointTracking.revision&&['applied','preserved','failed','rejected','unknown'].indexOf(a.state)>=0)endpointTracking=null;
    else if(Date.now()-endpointTracking.started>120000){message=_('等待超过120秒，结果尚未确认；请读取当前状态，勿将保存成功当成已应用。');endpointTracking=null;}
   }
   var recovery=v.recovery_block||{};
   if(recovery.new_request_block&&recovery.terminal){message=_('入口请求受阻，未保存或未应用：')+recovery.code+' · '+recovery.reason;endpointTracking=null;}
   else if(recovery.new_request_block)message+=' · '+recovery.reason;
   endpointSave.disabled=endpointBusy||!!recovery.new_request_block;
   if(v.warnings&&v.warnings.length)message+=' · '+v.warnings.join('；');
   if(v.warning&&v.warning.message&&v.pending)message+=' · '+v.warning.message;
   if(recoveryTracking&&(!v.recovery_result||v.recovery_result.request_id!==recoveryTracking.id))message+=' · '+_('本次受控恢复已提交，等待后台验证结果');else if(v.recovery_result&&v.recovery_result.message){message+=' · '+_('上次受控恢复：')+when(v.recovery_result.time)+' · '+v.recovery_result.message;if(recoveryTracking&&v.recovery_result.request_id===recoveryTracking.id)recoveryTracking=null;}
   endpointRecover.disabled=endpointBusy||!(v.recovery_offer&&v.recovery_offer.available);
   endpointRecover.hidden=!recovery.startup_pause;
   endpointResult.textContent=message;
  }
  function endpointAction(action,recoveryPayload){
   if(endpointBusy||action==='endpoint-save'&&endpointView&&endpointView.recovery_block&&endpointView.recovery_block.new_request_block)return Promise.resolve();endpointBusy=true;endpointRead.disabled=true;endpointRefresh.disabled=true;endpointSave.disabled=true;endpointSchedule.disabled=true;endpointRecover.disabled=true;
   endpointResult.textContent=action==='endpoint-save'?_('正在提交，应用可能短暂重连…'):_('正在读取…');
   var entryRequest,auxMessage=null,entryPayload={mode:endpointMode.value==='auto'?'auto':'manual',address:endpointMode.value==='candidate'?endpointCandidates.value:endpointMode.value==='custom'?endpointAddress.value.trim():'',hours:Number(endpointHours.value),recovery:false};return Promise.resolve().then(function(){if(['endpoint-save','endpoint-schedule','endpoint-recover'].indexOf(action)>=0){if(action==='endpoint-save'&&entryPayload.mode==='manual'&&!entryPayload.address)throw Error(_('请先选择候选入口或填写 IPv4:端口'));entryRequest=boundRequest(action==='endpoint-schedule'?{hours:Number(endpointHours.value)}:action==='endpoint-recover'?recoveryPayload:entryPayload);return fs.write('/var/run/routepilot/'+(action==='endpoint-schedule'?'endpoint-schedule-input':action==='endpoint-recover'?'endpoint-recover-input':'endpoint-input'),entryRequest.text,384);}}).then(function(){return command(action,entryRequest&&entryRequest.token);}).then(function(raw){if(action==='endpoint-info')return raw;if(entryRequest){var saved=JSON.parse(raw);if(saved.request_id!==entryRequest.id)throw Error(_('未收到本次入口请求确认'));if(action==='endpoint-save'){endpointDirty=false;endpointTracking={revision:saved.settings.revision,started:Date.now()};}if(action==='endpoint-recover')recoveryTracking={id:entryRequest.id,started:Date.now()};if(action==='endpoint-schedule')auxMessage=saved.message;}return command('endpoint-info');}).then(function(raw){showEndpoint(JSON.parse(raw));if(auxMessage)endpointResult.textContent+=' · '+auxMessage;}).catch(function(e){var failed=_('未完成：')+e.message;endpointResult.textContent=failed;if(action==='endpoint-save')return command('endpoint-info').then(function(raw){showEndpoint(JSON.parse(raw));if(!(endpointView.recovery_block&&endpointView.recovery_block.new_request_block))endpointResult.textContent=failed;}).catch(function(){endpointResult.textContent=failed;});}).finally(function(){endpointBusy=false;endpointRead.disabled=false;endpointRefresh.disabled=false;endpointSchedule.disabled=false;endpointRecover.disabled=!(endpointView&&endpointView.recovery_offer&&endpointView.recovery_offer.available);endpointSave.disabled=!!(endpointView&&endpointView.recovery_block&&endpointView.recovery_block.new_request_block);});
  }
  function pollEndpoint(){if(endpointTracking&&Date.now()-endpointTracking.started>120000){endpointTracking=null;endpointResult.textContent=_('等待超过120秒，结果尚未确认；请读取当前状态，勿将保存成功当成已应用。');return Promise.resolve();}if(recoveryTracking&&Date.now()-recoveryTracking.started>120000){recoveryTracking=null;endpointResult.textContent=_('恢复请求等待超时，结果尚未确认；请读取当前状态。');}if((endpointTracking||recoveryTracking)&&!endpointBusy)return endpointAction('endpoint-info');return Promise.resolve();}
  var endpointRead=E('button',{class:'btn',click:function(){return endpointAction('endpoint-info');}},_('读取当前设置 / 应用结果'));
  var endpointRefresh=E('button',{class:'btn',click:function(){return endpointAction('endpoint-refresh');}},_('读取客户端最新本地候选（不切换）'));
  var endpointSave=E('button',{class:'btn cbi-button-action',click:function(){return endpointAction('endpoint-save');}},_('应用并检测（可能重连）'));
  var endpointSchedule=E('button',{class:'btn',click:function(){return endpointAction('endpoint-schedule');}},_('只保存同步间隔（不重连）'));
  var endpointRecover=E('button',{class:'btn',hidden:true,click:function(){
   var offer=endpointView&&endpointView.recovery_offer;if(!offer||!offer.available)return;
   var confirm=E('input',{class:'cbi-input-text',placeholder:'RESTORE'});
   showImokModal(_('受控恢复原入口'),[E('p',{},_('仅恢复原事务入口并验证原出口，可能短暂直连。证据或验证不一致时仍保持阻断。总开关关闭时也会临时启动客户端验证，完成后仍保持关闭，不开启设备接管。请输入 RESTORE 确认。')),confirm,E('button',{class:'btn',click:function(){if(confirm.value!=='RESTORE'){notify(_('请输入 RESTORE'),true);return;}ui.hideModal();return endpointAction('endpoint-recover',{confirm:'RESTORE',revision:offer.revision,context:offer.context});}},_('确认恢复并验证')),E('button',{class:'btn',click:function(){ui.hideModal();}},_('取消'))]);
  }},_('受控恢复原入口并验证'));
  var endpointCard=E('section',{class:'rp-guide-step rp-endpoint-card'},[E('h3',{class:'rp-guide-heading'},_('入口选择与候选同步')),E('p',{class:'rp-help'},_('候选只读取客户端本地配置，不强制拉取、不测速、不自动换入口。应用入口可能短暂重连并允许宽带直连，随后验证原出口；入口不决定出口国家。')),E('label',{class:'rp-cloud-field'},[E('span',{},_('待应用入口方式（当前配置见上方）')),endpointMode]),endpointModeHelp,endpointCandidateField,endpointCustomField,E('label',{class:'rp-cloud-field'},[E('span',{},_('官方客户端本地候选同步间隔')),endpointHours]),E('div',{class:'rp-summary-actions'},[endpointRead,endpointRefresh,endpointSchedule,endpointSave,endpointRecover]),endpointResult]);
  updateEndpointMode();
  function buildCenter(node,force){
   if(centerComposed===node&&!force)return [node];
   function fields(group){return E('div',{},centerFields[group].map(function(o){return node.querySelector('[id="cbi-routepilot-main-'+o.option+'"]');}).filter(Boolean));}
   function selectFields(keys){return keys.map(function(key){return node.querySelector('[id="cbi-routepilot-main-'+key+'"]');}).filter(Boolean);}
   var guide=node.querySelector('.rp-guide'),protocol=node.querySelector('[id="cbi-routepilot-main-warp_protocol"]');
   var controls=page==='account'?[centerRefresh,centerObserve]:[centerObserve];
   var top=E('section',{class:'cbi-section rp-center-summary'},[E('h3',{},_('当前连接与只读状态')),centerState,E('div',{class:'rp-summary-bottom'},[page==='workers'?centerAuth:E('div',{class:'rp-auth-badges'},[centerWarpAuth,centerCountry]),E('div',{class:'rp-summary-actions'},controls)])]);
   updateCenter();
   var groups=[E('nav',{class:'rp-section-nav','aria-label':_('Cloudflare 设置页面')},pages.filter(function(p){return sectionGroups[p[0]];}).map(function(p){return E('a',{class:'btn',href:base+'/cloudflare/'+p[0],'aria-current':p[0]===page?'page':null},p[1]);})),top];
   if(pageGroup==='advanced')groups.push(centerGroup('advanced',_('主线路账户与连接协议'),_('已有账户无需重复注册；保存并应用协议后才重连。'),[panel(_('协议设置'),_('保存的协议选择与当前读取到的协议分别确认。'),[protocol,warpInfo]),guide]));
   if(pageGroup==='endpoint')groups.push(centerGroup('endpoint',_('主线路入口'),'',[E('div',{class:'imok-settings-pair imok-entry-layout'},[panel(_('当前入口与最近结果'),_('这里是客户端读取结果；编辑项保留在右侧，不会因读取而提交。'),[endpointDetails],'imok-readonly-panel'),endpointCard])]));
   if(pageGroup==='basic')groups.push(centerGroup('basic',_('账户出口、国家 / 地区与接管范围'),_('选择账户中已部署的出口后检测实际 IP 和地区；入口优选不决定地区。'),[E('div',{class:'imok-route-layout'},[panel(_('代理出口'),_('目录读取不切换出口，选择后仍需保存并应用。'),selectFields(['exit','_discover','_exit_directory'])),panel(_('接管范围与分流'),_('按当前范围显示适用设置；未启用的设备与网络不接管。'),selectFields(['scope','lan_interfaces','lan_mode','bypass_clients','ipv6_mode','_device_mode','_info']))])]));
   if(pageGroup==='devices'){
    var scopeHint=centerGroupBadges.devices=E('p',{class:'rp-help'});
    var devicePanel=panel(_('设备策略'),null,[
     E('div',{class:'imok-device-grid'},[centerDeviceNode]),
     E('p',{class:'imok-device-note'},_('域名分流依赖普通 DNS，共用 CDN IP 的网站可能互相影响。使用加密 DNS 的设备可优先选「国内 IP 直连，其余代理」。'))
    ],'imok-device-panel');
    devicePanel.setAttribute('id','rp-center-devices');
    devicePanel.setAttribute('aria-label',_('设备分流'));
    var heading=devicePanel.querySelector('.imok-panel-heading');
    heading.appendChild(scopeHint);
    var addDevice=centerDeviceNode.querySelector('.cbi-section-create');
    if(addDevice)heading.appendChild(addDevice);
    var actionHeader=centerDeviceNode.querySelector('.cbi-section-table-titles .cbi-section-actions');
    if(actionHeader)actionHeader.textContent=_('操作');
    groups.push(devicePanel,deviceUsagePanel());
   }
   if(pageGroup==='dns')groups.push(centerGroup('dns',_('DNS 与联网检测'),_('只读检测和清缓存不会保存其他表单；历史结果失效时会标记。'),[E('div',{class:'imok-settings-pair imok-dns-settings'},[panel(_('DNS 配置'),_('编辑值只在保存后生效；下方检测使用已应用的设置。'),[fields('dns')]),E('div',{class:'imok-panel-stack'},[dnsCacheCard,dnsTestCard])]),E('div',{class:'imok-dns-tests'},[testCard]),E('details',{class:'imok-diagnostics'},[E('summary',{},_('连接记录与 UDP 接收观测')),connectionHistory,receiveCard])]));
   if(pageGroup==='workers')groups.push(centerGroup('workers',_('Workers 备用与 Cloudflare API 授权'),_('备用需手动检测和启用；以下设置不自动切换主线路。'),[workerInventory,E('p',{class:'rp-help'},E('a',{href:'#',click:function(ev){if(ev&&ev.preventDefault)ev.preventDefault();workerPinned=true;openWorkerStage('use');}},_('已有节点？保留原入口，前往独立检测 →'))),workerWorkspaceView([workerStage('deploy',_('创建部署与 API 授权'),cloudCard,!(cloudMeta&&cloudMeta.configured)),workerStage('relay',_('PROXYIP 主用与备用'),relayCard,false),workerStage('scan',_('优选 Cloudflare 入口'),scanCard,false),workerStage('use',_('独立测试与手动启用'),workerBackupCard,!!(cloudMeta&&cloudMeta.configured))])]));
   groups.push(E('p',{class:'rp-save-hint'},page==='endpoint'?_('本页使用各操作按钮单独提交；只保存同步间隔不会重连，应用入口可能重连。'):page==='workers'?_('云端部署和备用节点按钮分别立即执行其注明的操作；读取与检测不会启用备用。'):_('页面底部“保存并应用”只提交本页设置；读取、检测和清缓存不会提交编辑。')));node.replaceChildren.apply(node,groups);centerComposed=node;updateCenter();if(page==='workers'){updateWorkerProgress();updateWorkerFlow();}return [node];
  }
  if((ruleSection==='sources'||ruleSection==='custom')&&typeof m.renderContents==='function'){
   var renderRules=m.renderContents.bind(m);m.renderContents=function(){return renderRules.apply(null,arguments).then(function(node){
    function take(keys){return keys.map(function(k){return node.querySelector('[id="cbi-routepilot-main-'+k+'"]');}).filter(Boolean);}
    var contents;if(ruleSection==='sources'){
     var schedule=take(['update_hours']),sources=['china_url','china_domains_url','gfw_url','whitelist_url'].map(function(key){return panel(ruleNames[key.replace('_url','')],null,take([key]));});
     contents=[panel(_('自动更新频率'),_('单位为小时：24 为每天，168 为每周；修改后保存并应用。'),schedule),E('div',{class:'imok-settings-pair'},sources)];
    }else contents=[E('div',{class:'imok-settings-pair'},[panel(_('始终直连'),_('使用本地宽带出口，保留原有校验与优先级。'),take(['custom_direct_domains','custom_direct_ips'])),panel(_('始终代理'),_('使用当前代理线路，保存后仍需确认实际生效。'),take(['custom_proxy_domains','custom_proxy_ips']))])];
    node.replaceChildren(E('section',{class:'rp-rule-settings imok-settings-section','aria-label':ruleSection==='sources'?_('规则来源与更新频率'):_('自定义分流设置')},contents));return node;
   });};
  }

  if(center&&typeof m.renderContents==='function'){var renderMap=m.renderContents.bind(m);m.renderContents=function(){return renderMap.apply(null,arguments).then(function(node){buildCenter(node,true);return node;});};}

  var content=page==='overview'?[overviewHero,trafficCard,nodeCard,quickCard,connectionHistory,versionInfo]:ruleSection==='overview'?[rulesNav,rulesCard,rulesStatusCard]:ruleSection==='sources'?[rulesNav,rulesSourcesIntro]:ruleSection==='manage'?[rulesNav,rulesBrowser]:ruleSection==='custom'?[rulesNav,rulesCustomIntro]:page==='backup'?[backupCard]:[];
  return (center||formPage?m.render():Promise.resolve(null)).then(function(node){if(center){content=buildCenter(node);}else if(node){var title=node.querySelector('h2');if(title)title.remove();if(typeof node.querySelectorAll==='function')Array.prototype.forEach.call(node.querySelectorAll('h3'),function(heading){if(heading.textContent.trim()===pageInfo[1])heading.remove();});var intro=node.querySelector('.cbi-map-descr');if(intro)intro.remove();content.push(node);}if(['overview','account','routing'].includes(page))Promise.resolve().then(function(){if(isCurrentView())return refreshDirectory();}).catch(function(){});if(ruleDataPage)Promise.resolve().then(function(){return readRulesInfo(true);});if(page==='endpoint')Promise.resolve().then(function(){if(isCurrentView()&&!endpointLoaded&&!endpointBusy)return endpointAction('endpoint-info');});if(page==='account')Promise.resolve().then(function(){if(isCurrentView())return centerRead('warp-info');}).catch(function(){});return finishPage(E('div',{class:'rp-layout','data-page':page},[E('link',{rel:'stylesheet',href:L.resource(imokStylesheet)}),E('div',{class:'rp-header'},[E('span',{class:'rp-brand'},'imOK'),E('h2',{},pageInfo[1]),E('p',{class:'rp-caption'},pageInfo[2])]),E('div',{class:'rp-page-content'},page==='overview'?[E('div',{class:'imok-overview-main'},[overviewHero,trafficCard,nodeCard]),E('div',{class:'imok-overview-secondary'},[quickCard]),E('section',{class:'cbi-section imok-technical','aria-label':_('技术详情')},[E('h3',{},_('详情与诊断')),connectionHistory,versionInfo])]:content)]));});
 },
 handleSaveApply:function(){return this.applySettings();},
 handleReset:null
});

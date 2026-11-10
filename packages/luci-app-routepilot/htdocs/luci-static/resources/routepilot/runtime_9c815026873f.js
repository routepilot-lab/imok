'use strict';
'require baseclass';
'require fs';
'require ui';
'require poll';
'require request';
'require rpc';

function validStatus(value){
 if(!value||value.schema!==1||typeof value.runtime_exists!=='boolean'||typeof value.update_available!=='boolean'||typeof value.recovery_required!=='boolean'||typeof value.installed!=='boolean'||typeof value.supported!=='boolean'||!value.settings||![0,24,72,168].includes(value.settings.hours)||!value.check||typeof value.check!=='object'||!value.job||typeof value.job!=='object'||!Array.isArray(value.backups))throw Error('WARP 维护状态格式异常，请刷新；未推断为已安装或最新版本');
 if(value.installed&&typeof value.local_version!=='string'&&!(value.local_version==null&&value.job.state==='running')||value.latest_version!=null&&typeof value.latest_version!=='string'||Array.isArray(value.job)||Array.isArray(value.check)||value.job.message!=null&&typeof value.job.message!=='string'||value.check.message!=null&&typeof value.check.message!=='string'||value.backups.some(function(b){return !b||typeof b.id!=='string'||!(/^[a-f0-9]{32}$/).test(b.id)||!Number.isFinite(b.created_at)||b.version!=null&&typeof b.version!=='string';}))throw Error('WARP 维护状态字段异常，请刷新核对');
 if(value.environment!=null&&(!value.environment||Array.isArray(value.environment)||typeof value.environment.can_prepare!=='boolean'||!Array.isArray(value.environment.issues)||value.environment.issues.some(function(v){return typeof v!=='string';})))throw Error('环境检测结果格式异常，请刷新核对');
 if(value.online!=null&&(!value.online||Array.isArray(value.online)||!['idle','preparing','ready','expired','installing','installed','failed'].includes(value.online.state)||value.online.message!=null&&typeof value.online.message!=='string'||value.online.state==='ready'&&typeof value.online.version!=='string'))throw Error('自动准备状态格式异常，请刷新核对');
 return value;
}
function readStatus(){return fs.exec('/usr/libexec/routepilot-runtime-status',[]).catch(function(){throw Error('无法连接路由器维护接口，请检查管理连接和登录权限');}).then(function(r){if(!r||typeof r.stdout!=='string')throw Error('路由器维护响应无效，请刷新重试');var d;try{d=JSON.parse(r.stdout);}catch(_){throw Error('未取得有效的 WARP 维护状态');}if(r.code||d&&d.error)throw Error(d&&d.error||'没有读取 WARP 维护状态的权限');return validStatus(d);});}
return baseclass.extend({
 load:function(){return readStatus().catch(function(e){return {load_error:e.message};});},
 render:function(initial,stylesheet,bindRequest){
  var self=this,data=initial,working=false,reading=false,readSerial=0,prepared=false,shaValue='',selectedHours=null,selectedBackup='',uploaded=false,modalActive=false;
  var root=E('div',{class:'rp-layout','data-page':'runtime'}),body=E('div',{class:'rp-page-content'}),notice=E('p',{class:'imok-runtime-notice',role:'status','aria-live':'polite'});
  root.appendChild(E('link',{rel:'stylesheet',href:stylesheet}));root.appendChild(E('div',{class:'rp-header'},[E('span',{class:'rp-brand'},'imOK'),E('h2',{},'WARP 安装与更新'),E('p',{class:'rp-caption'},'先在路由器安装 WARP，再登录账户与选择出口。定期检查只提示新版，安装和升级由你确认。')]));root.appendChild(notice);root.appendChild(body);
  function timeLabel(t){return Number.isFinite(t)&&t>0?new Date(t*1000).toLocaleString():'尚未检查';}
  function button(label,action,primary,disabled){return E('button',{class:'btn cbi-button'+(primary?' cbi-button-apply':''),type:'button',disabled:disabled?true:null,click:function(){if(working)return;return Promise.resolve().then(action).catch(function(e){message(e.message,true);});}},label);}
  function message(text,error){notice.textContent=text;notice.setAttribute('data-tone',error?'danger':'neutral');notice.setAttribute('role',error?'alert':'status');}
  function panel(title,children){return E('section',{class:'imok-panel'},[E('div',{class:'imok-panel-heading'},[E('h3',{},title)])].concat(children));}
  function link(label,route){return E('a',{class:'btn cbi-button',href:L.url('admin/services/routepilot/'+route)},label);}
  function help(text){return E('p',{class:'rp-help'},text);}
  function ensurePrepared(){if(prepared)return Promise.resolve();return fs.exec('/usr/libexec/routepilot-runtime',['prepare']).then(function(r){if(r.code)throw Error('无法准备维护目录，请检查管理员权限和存储空间');prepared=true;});}
  function maintenanceRequest(action,payload){
   if(working)return Promise.reject(Error('已有操作正在提交，请稍候'));
   working=true;readSerial++;
   var r=bindRequest(payload||{});
   return ensurePrepared().then(function(){return fs.write('/var/run/routepilot/requests/runtime-input-'+r.id+'.json',r.text,384);}).then(function(){return fs.exec('/usr/libexec/routepilot-runtime',[action,r.token]);}).then(function(result){var d;try{d=JSON.parse(result.stdout);}catch(_){throw Error('维护响应无法识别，请刷新确认，勿重复提交');}if(result.code||d&&d.error)throw Error(d&&d.error||'维护请求未完成');if(!d||typeof d!=='object'||d.request_id!==r.id)throw Error('本次维护请求未收到对应确认，请刷新核对');return d;}).finally(function(){working=false;});
  }
  function refresh(){if(reading||working)return Promise.resolve();reading=true;var id=++readSerial;return self.load().then(function(d){if(id!==readSerial||!root.isConnected)return;data=d;draw();}).finally(function(){reading=false;});}
  function modalActions(buttons){return E('div',{class:'right imok-runtime-modal-actions'},buttons);}
  function showPlan(operation,backupID){
   if(operation==='install'&&!/^[a-f0-9]{64}$/i.test(shaValue.trim()))throw Error('请先上传可信的未注册运行环境包，并填写对应的 64 位 SHA-256 校验码');
   return maintenanceRequest('plan',{operation:operation,sha256:shaValue.trim().toLowerCase(),backup_id:backupID||''}).then(function(r){
    if(!r.plan||typeof r.plan.id!=='string')throw Error('未获得有效的维护确认计划');
    var plan=r.plan,error=E('p',{role:'alert'}),ack=E('input',{type:'checkbox'}),submitting=false;
    var title=['install','online-install'].includes(operation)?'确认安装到路由器':operation==='upgrade'?'确认备份并升级 WARP':operation==='recover'?'确认核对维护阻断':'确认回滚 WARP';
    var cancel=button('取消',function(){if(submitting)return;modalActive=false;ui.hideModal();});
    var confirm=button(['install','online-install'].includes(operation)?'确认安装':operation==='upgrade'?'备份并升级':operation==='recover'?'核对并恢复':'确认回滚',function(){
     if(submitting||!ack.checked)return;submitting=true;confirm.disabled=true;cancel.disabled=true;ack.disabled=true;
     return maintenanceRequest('execute',{plan_id:plan.id,confirmation:'CONFIRM'}).then(function(){modalActive=false;ui.hideModal();message('');return refresh();}).catch(function(e){submitting=false;confirm.disabled=!ack.checked;cancel.disabled=false;ack.disabled=false;error.textContent=e.message;});
    },true,true);
    ack.addEventListener('change',function(){confirm.disabled=!ack.checked||submitting;});modalActive=true;
    ui.showModal(title,[help('操作对象：这台路由器内的 WARP 运行环境。'+(plan.version?'目标版本：'+plan.version+'。':'')),help('请使用本地或独立的管理连接，并先关闭 imOK 总开关、结束登录准备。'+(['install','online-install'].includes(operation)?'首次安装不会覆盖已有 WARP 环境。':operation==='upgrade'?'升级会保留原环境，方便回滚。':operation==='rollback'?'将使用所选恢复副本，并保留当前账户配置。':'将核对上次维护记录，不会自动开启隧道。')+'成功后仍需由你手动开启并检查联网。'),E('label',{class:'imok-runtime-ack'},[ack,'我已确认管理连接可用，并同意执行本次操作。']),error,modalActions([cancel,confirm])],'imok-dialog','imok-runtime-dialog');
   });
  }
  function upload(){
   working=true;
   return ensurePrepared().then(function(){return new Promise(function(resolve){
    modalActive=true;
    var file=E('input',{type:'file',accept:'.tgz,.tar.gz','aria-label':'选择未注册的运行环境包'}),error=E('p',{role:'alert'}),uploading=false;
    var progress=E('div',{class:'imok-runtime-upload-progress',role:'progressbar','aria-label':'运行环境上传进度','aria-valuemin':0,'aria-valuemax':100,'aria-valuenow':0},'等待选择文件');
    var cancel=E('button',{class:'btn cbi-button',type:'button',click:function(){if(uploading)return;modalActive=false;working=false;ui.hideModal();resolve({cancelled:true});}},'取消');
    var submit=E('button',{class:'btn cbi-button cbi-button-apply',type:'button',click:function(){
     if(uploading)return;
     var selected=file.files&&file.files[0];
     if(!selected||selected.size<=0||selected.size>256*1024*1024){error.textContent='请选择不超过 256 MB 的未注册运行环境 .tgz 包。';return;}
     uploading=true;cancel.disabled=true;submit.disabled=true;file.disabled=true;error.textContent='';
     var form=new FormData();form.append('sessionid',rpc.getSessionID());form.append('filename','/opt/routepilot/runtime-uploads/runtime.tgz');form.append('filedata',selected);
     request.post(L.env.cgi_base+'/cgi-upload',form,{timeout:0,progress:function(ev){var value=ev.total?Math.min(100,Math.round(ev.loaded/ev.total*100)):0;progress.setAttribute('aria-valuenow',String(value));progress.textContent='正在上传 '+value+'%';}}).then(function(response){var reply=response.json();if(!reply||typeof reply!=='object'||reply.failure)throw Error('upload');modalActive=false;working=false;uploaded=true;ui.hideModal();message('运行环境包已上传，尚未安装。填写可信来源提供的校验码后，再点击校验并安装。');draw();resolve({uploaded:true});}).catch(function(){uploading=false;cancel.disabled=false;submit.disabled=false;file.disabled=false;error.textContent='文件上传未完成，请检查管理连接、路由器存储空间与权限。';});
    }},'上传文件');
    ui.showModal('上传路由器 WARP 运行环境',[help('只接受可信来源的未注册 .tgz 运行环境包；上传成功后仍需校验并确认安装。'),file,progress,error,modalActions([cancel,submit])],'imok-dialog','imok-runtime-dialog');
   });}).finally(function(){working=false;});
  }

  function draw(){
   body.replaceChildren();
   if(data.load_error){body.appendChild(panel('状态暂时无法读取',[E('p',{role:'alert'},data.load_error),button('重新读取状态',refresh)]));return;}
   var busy=working||data.job.state==='running',disabled=busy||!data.supported||data.recovery_required;
   var env=data.environment||{can_prepare:false,issues:['环境检测尚未完成，请刷新本机状态']},online=data.online||{state:'idle'};
   var known=data.installed,check=data.check,checkFailed=check.state==='failed',checking=check.state==='checking',latest=data.latest_version;
   var rows=E('dl',{class:'imok-runtime-facts'},[E('div',{},[E('dt',{},'路由器内 WARP'),E('dd',{},known?(data.local_version||'维护中，版本暂未确认'):data.runtime_exists?'检测到运行文件，状态待核对':'尚未安装')]),E('div',{},[E('dt',{},'官方可用版本'),E('dd',{},latest||'尚未取得有效结果')]),E('div',{},[E('dt',{},'最近成功检查'),E('dd',{},timeLabel(check.checked_at))]),E('div',{},[E('dt',{},'下次计划检查'),E('dd',{},data.settings.hours?(data.next_check_at?timeLabel(data.next_check_at):'等待首次计划检查'):'已关闭定期检查')])]);
   var badgeText=!known?'先安装 WARP':checkFailed?'检查失败，版本待核对':checking?'正在检查':data.update_available?'发现可用新版本':latest?'已与官方目录核对':'等待检查官方版本';
   var version=panel('安装与版本状态',[E('span',{class:'rp-status-badge','data-tone':checkFailed?'danger':data.update_available?'warning':known?'neutral':'warning'},badgeText),rows,E('div',{class:'rp-summary-actions'},[button('检查更新',function(){message('正在提交版本检查…');return maintenanceRequest('check',{}).then(function(){message('');return refresh();});},false,busy),button('刷新本机状态',refresh,false,busy)]),help('检查更新只下载官方版本目录，不重启 WARP、不切换出口。检查失败时，历史版本信息不会当作最新结果。')]);
   var frequency=E('select',{'aria-label':'自动检查频率',change:function(ev){selectedHours=Number(ev.target.value);}},[[24,'每天检查一次'],[72,'每三天检查一次'],[168,'每周检查一次'],[0,'关闭定期检查']].map(function(v){return E('option',{value:String(v[0])},v[1]);}));frequency.value=String(selectedHours==null?data.settings.hours:selectedHours);
   var updates=panel('检查与升级',[E('label',{class:'imok-runtime-field'},['检查频率',frequency]),E('div',{class:'rp-summary-actions'},[button('保存检查频率',function(){return maintenanceRequest('settings',{hours:Number(frequency.value)}).then(function(){selectedHours=null;message('检查频率已保存；没有执行安装或升级。');return refresh();});},false,busy),button('备份并升级',function(){return showPlan('upgrade');},true,disabled||!data.update_available)]),help('发现新版后会在这里提示。升级前再次核对版本、空间和占用，在副本中验证官方签名及安装包，再保留原环境并替换。'),help('升级、安装和回滚都不会自动开启主线路。')]);
   if(!data.supported)body.appendChild(E('p',{class:'imok-runtime-notice','data-tone':'warning',role:'alert'},'当前平台不在安装支持范围。仅 ARM64 OpenWrt 24.10 开放维护操作，其他平台可以查看说明与版本信息。'));
   if(data.recovery_required)body.appendChild(panel('上次维护需要核对',[E('p',{role:'alert'},'检测到未完成的环境替换，已阻止重新启动。请优先选择恢复副本回滚；没有可用副本时，可核对尚未提交的维护记录。'),button('核对未完成维护',function(){return showPlan('recover');},false,busy)]));
   if(data.job.message)body.appendChild(E('p',{class:'imok-runtime-notice','data-tone':data.job.state==='failed'?'danger':data.job.state==='success'?'success':'neutral',role:data.job.state==='failed'?'alert':'status','aria-live':'polite'},data.job.message));
   if(checkFailed)body.appendChild(E('p',{class:'imok-runtime-notice','data-tone':'danger',role:'alert'},check.message||'版本检查失败，请稍后重试。'));
   var statusGrid=E('div',{class:'imok-runtime-grid'},[version,updates]);
   var sha=E('input',{class:'cbi-input-text',type:'text',value:shaValue,placeholder:'粘贴可信来源提供的 64 位 SHA-256',maxlength:64,autocomplete:'off',spellcheck:false,input:function(ev){shaValue=ev.target.value;}});
   var installBody=[help('这是安装到路由器，不是在手机或电脑安装 WARP。插件本身已安装；还需要独立的 Debian 12 ARM64 WARP 运行环境。'),E('ol',{class:'imok-runtime-steps'},[E('li',{},'从可信来源准备未注册的运行环境 .tgz 包，以及发布者提供的 SHA-256。普通 Linux .deb 和 imOK 插件 IPK 不能在这里作为运行环境包导入。'),E('li',{},'上传文件并核对校验码，确认后安装到路由器；已有环境不会被首次安装覆盖。'),E('li',{},'安装成功后，进入账户与协议登录，再选择出口和接管范围，最后手动开启。')]),E('div',{class:'rp-summary-actions'},[E('button',{class:'btn cbi-button',type:'button',disabled:(disabled||known)?true:null,click:function(ev){if(working)return;return upload(ev).catch(function(e){message(e.message,true);});}},uploaded?'重新上传运行环境包':'上传运行环境包')]),E('label',{class:'imok-runtime-field'},['文件校验码（SHA-256）',sha]),E('div',{class:'rp-summary-actions'},[button('校验并安装到路由器',function(){return showPlan('install');},true,disabled||data.runtime_exists),link('安装后：登录 Cloudflare 账户','cloudflare/account')]),help('运行环境包需要自行从可信来源取得，本插件不附带第三方 WARP 程序、账户或密钥。遇到不兼容、空间不足或校验不符，会停止并保留原文件。')];
   var ready=online.state==='ready';
   var setup=panel('首次使用：安装路由器 WARP',[
    help('系统会自动识别路由器并下载所需文件。你不需要寻找安装包，也不需要自己填写校验码。'),
    E('ol',{class:'imok-runtime-steps'},[E('li',{},'自动检测系统、处理器、存储与必要组件。'),E('li',{},'自动下载 Debian 官方基础环境和 Cloudflare 官方 WARP，核对来源、签名与文件。'),E('li',{},'文件准备完成后，由你确认安装；安装后登录自己的账户，再选择出口并手动开启。')]),
    E('dl',{class:'imok-runtime-facts'},[E('div',{},[E('dt',{},'系统与处理器'),E('dd',{},(env.system||'系统待识别')+' · '+(env.architecture||'处理器待识别'))]),E('div',{},[E('dt',{},'路由器可用存储'),E('dd',{},Number.isFinite(env.free_bytes)?(env.free_bytes/1073741824).toFixed(1)+' GB':'待检测')])]),
    E('p',{class:'imok-runtime-notice','data-tone':online.state==='failed'?'danger':ready?'success':'neutral',role:'status'},ready?'文件已自动下载并校验，尚未安装。待安装版本：'+online.version:busy?'系统正在自动准备，请等待完成后确认安装。':online.state==='failed'?(online.message||'自动准备失败，请检查网络与空间后重试；未自动启用。'):online.state==='expired'?'准备文件已过期，请重新自动下载；尚未安装。':env.can_prepare?'环境检查通过，可以自动下载并准备安装。':(env.issues||['当前环境尚不能自动安装']).join('；')),
    E('div',{class:'rp-summary-actions'},[button(ready?'确认安装到路由器':'自动下载并准备安装',function(){if(ready)return showPlan('online-install');message('正在自动准备官方安装文件；下载完成后仍需你确认安装。');return maintenanceRequest('prepare-online',{}).then(function(){message('');return refresh();});},true,disabled||data.runtime_exists||(!ready&&!env.can_prepare)),link('安装后：登录 Cloudflare 账户','cloudflare/account')]),
    help('打开本页只检查环境；点击准备才会下载。安装前会再次确认，完成后不会自动开启主线路。')
   ]);
   if(!known){body.appendChild(setup);body.appendChild(E('details',{class:'imok-panel'},[E('summary',{},'高级：离线导入运行环境')].concat(installBody)));body.appendChild(E('details',{class:'imok-panel'},[E('summary',{},'版本检查与定期计划'),statusGrid]));}else{body.appendChild(statusGrid);body.appendChild(E('details',{class:'imok-panel'},[E('summary',{},'高级：首次安装与离线导入说明')].concat(installBody)));}

   var history=(data.backups||[]),choices=E('select',{'aria-label':'选择 WARP 恢复副本',change:function(ev){selectedBackup=ev.target.value;}},history.map(function(b){return E('option',{value:b.id},(b.version||'版本未确认')+' · '+timeLabel(b.created_at));}));
   if(!history.some(function(b){return b.id===selectedBackup;}))selectedBackup=history.length?history[0].id:'';choices.value=selectedBackup;
   body.appendChild(panel('恢复副本与回滚',[E('div',{class:'imok-runtime-history-controls'},[history.length?E('label',{class:'imok-runtime-field'},['选择恢复副本',choices]):help('还没有 WARP 恢复副本。升级提交时会保留原环境，不会自动删除已有副本。'),button('使用此副本回滚',function(){return showPlan('rollback',choices.value);},false,busy||!data.supported||!history.length)]),help('回滚前会验证副本，并保留当前账户配置。普通“备份迁移”的设置导出不包含完整 WARP 运行环境和注册凭据。恢复副本存储在路由器私有目录，请勿公开分享。')]));
   body.appendChild(E('details',{class:'imok-panel'},[E('summary',{},'软件来源与适用范围'),help('支持范围：ARM64 OpenWrt 24.10；运行环境为 Debian 12 bookworm。官方稳定源：https://pkg.cloudflareclient.com/。'),help('目录检查通过不等于软件已安装、隧道已连接或网站访问成功。公钥变化、依赖不满足或程序自检失败时，不提交升级。'),E('a',{href:'https://pkg.cloudflareclient.com/',target:'_blank',rel:'noreferrer noopener'},'查看 Cloudflare 官方软件源说明')]));
  }
  draw();poll.add(function(){if(!root.isConnected||working||reading||modalActive||data.load_error||(data.job.state!=='running'&&data.check.state!=='checking'))return;return refresh().catch(function(e){message(e.message,true);});},2);return root;
 }
});

'use strict';
'require baseclass';
// Pure display decisions; no network or configuration writes.
return baseclass.extend({
 workerJobKey:function(job){return job?String(job.started_at||'')+'|'+String(job.action||''):'none';},
 workerJobProgress:function(job,pending,now,kind,total){
  if(!job&&!pending)return {state:'idle',phase:'',elapsed:0,completed:0,total:total||0,matched:false};
  var limit=kind==='backup'?100:300,start=pending?pending.started:Number(job&&job.started_at)*1000;if(!Number.isFinite(start))start=now;var elapsed=Math.max(0,Math.floor((now-start)/1000));
  var matched=!!job&&(!pending||(this.workerJobKey(job)!==pending.baseline&&Number(job.started_at)*1000>=pending.started-5000&&(!pending.action||job.action===pending.action)));
  if(!matched)return {state:elapsed>=limit?'unknown':'busy',phase:'awaiting',elapsed:elapsed,completed:0,total:total||0,matched:false};
  var count=Array.isArray(job.rows)?job.rows.length:Array.isArray(job.results)?job.results.length:0;
  var state=job.state,phase=job.phase||'',result={elapsed:elapsed,completed:count,total:Number(job.total)||total||0,phase:phase,matched:true,message:job.message||'',finished_at:job.finished_at};
  result.state=['queued','running'].indexOf(state)>=0?(now/1000-Number(job.started_at)>=limit?'unknown':'busy'):['failed','unreachable','deployment_error'].indexOf(state)>=0?'error':state==='conflict'?'warning':['waiting_dns','dns_unavailable','tls_unavailable','access_restricted'].indexOf(state)>=0?'waiting':['ready','verified','available','done'].indexOf(state)>=0?'success':'idle';
  if(kind==='cloud'&&job.action==='deploy'){
   var index={connecting:0,checking:0,uploading:1,binding:2,waiting_dns:3}[phase];result.total=4;result.completed=result.state==='success'?4:result.state==='waiting'?3:Number.isFinite(index)?index:0;
  }
  return result;
 },

 diagnosticValid: function(test) {
  if(!test||typeof test!=='object'||!['running','done'].includes(test.state)||!Array.isArray(test.results)||test.results.length>5)return false;
  var seen={};return test.results.every(function(row){if(!row||typeof row!=='object'||!['google','youtube','github','taobao','baidu'].includes(row.id)||seen[row.id]||!['pending','ok','failed','restricted'].includes(row.state))return false;seen[row.id]=true;return true;});
 },
 wizardTestToken: function(test) {
  if(!this.diagnosticValid(test))return 'none';
  return JSON.stringify([test.started_at,test.finished_at,test.state,test.exit,test.exit_ip,test.runtime_id,test.generation,test.applied_nonce,(test.results||[]).map(function(r){return [r.id,r.state,r.http,r.ms];}).sort(function(a,b){return String(a[0]).localeCompare(String(b[0]));})]);
 },
 wizardProxyVerification: function(test,status,config,now) {
  var result=this.wizardVerification(test,status,config,now);
  if(['success','error','warning'].indexOf(result)<0)return result;
  var proxy=test.results.filter(function(r){return ['google','youtube','github'].indexOf(r.id)>=0;});
  return proxy.some(function(r){return r.state==='failed';})?'error':proxy.some(function(r){return r.state==='restricted';})?'warning':proxy.every(function(r){return r.state==='ok';})?'success':'stale';
 },
 wizardVerification: function(test, status, config, now) {
  if (!this.fresh(status, now) || status.state !== 'running') return 'pending';
  if (status.applied_nonce !== config.nonce || status.exit !== config.exit) return 'stale';
  if (!this.diagnosticValid(test) || this.diagnosticStale(test, status, now)) return 'stale';
  if (this.diagnosticRunning(test, now)) return 'busy';
  var age=now/1000-Number(test.finished_at), ids=['google','youtube','github','taobao','baidu'];
  if (test.state !== 'done' || !Number.isFinite(age) || age < -5 || age > 300 || !Array.isArray(test.results) || test.results.length !== ids.length) return 'stale';
  if (!ids.every(function(id){return test.results.filter(function(r){return r.id===id;}).length===1;})) return 'stale';
  if (test.results.some(function(r){return r.state==='failed';})) return 'error';
  if (test.results.some(function(r){return r.state==='restricted';})) return 'warning';
  return test.results.every(function(r){return r.state==='ok';}) ? 'success' : 'stale';
 },
 wizardSteps: function(config, catalog, status, test, actions, now) {
  actions=actions||{};
  var readAge=now-Number(catalog.read_at), directory=!!catalog.available&&Number.isFinite(readAge)&&readAge>=-5000&&readAge<=60000;
  var ready=this.fresh(status,now)&&['preparing','running'].indexOf(status.state)>=0;
  var states=[
   {id:'org',state:directory||/^[a-zA-Z0-9-]{1,63}$/.test(config.organization||'')?'success':'pending',reason:directory?'reuse':'organization'},
   {id:'prepare',state:ready||directory?'success':'pending',reason:directory?'reuse':'environment'},
   {id:'auth',state:directory?'success':'pending',reason:catalog.last_success_at?'access_previous':'access'}
  ];
  var token=this.wizardTestToken(test);
  states.forEach(function(step){var action=actions[step.id];if(action&&action.state==='error')step.state='error';else if(action&&action.state==='busy'&&(step.id!=='verify'||action.baseline===token||test&&test.state==='running'))step.state='busy';});
  return states;
 },
 dnsState: function(data,status,now){
  if(!this.fresh(status,now)||status.state!=='running')return 'inactive';
  if(!data||data.schema!==1||!Array.isArray(data.policies))return 'unavailable';
  var age=now/1000-Number(data.time);
  if(!Number.isFinite(age)||age < -1||age>5||!data.runtime_id||!data.generation||!data.process_instance||data.runtime_id!==status.runtime_id||data.generation!==status.generation)return 'stale';
  if(data.state!=='running'||!data.policies.length||data.policies.length>8||!data.upstreams||!data.policies.every(function(p){return p&&typeof p==='object'&&['cache_entries','cache_hits','requests','errors'].every(function(k){return Number.isSafeInteger(p[k])&&p[k]>=0;});}))return 'unavailable';
  return 'running';
 },
 dnsTestValid:function(test){
  return !!test&&typeof test==='object'&&['running','done'].includes(test.state)&&Array.isArray(test.results)&&test.results.length<=32&&test.results.every(function(row){return !!row&&typeof row==='object'&&Number.isSafeInteger(row.policy)&&['domestic','proxy'].includes(row.target)&&['udp','tcp'].includes(row.protocol);});
 },
 dnsTestState:function(test,status,dns,now){
  if(!test)return this.dnsState(dns,status,now)==='running'?'pending':'unavailable';
  if(!this.dnsTestValid(test)||this.dnsState(dns,status,now)!=='running'||this.diagnosticStale(test,status,now)||test.process_instance!==dns.process_instance)return 'stale';
  var age=now/1000-Number(test.started_at);
  if(test.state==='running')return Number.isFinite(age)&&age>=-5&&age<50?'busy':'stale';
  var finishedAge=now/1000-Number(test.finished_at);
  if(test.state!=='done'||!Number.isFinite(finishedAge)||finishedAge < -5||finishedAge>300||!Array.isArray(test.results))return 'stale';
  var keys={};test.results.forEach(function(r){keys[r.policy+'/'+r.target+'/'+r.protocol]=(keys[r.policy+'/'+r.target+'/'+r.protocol]||0)+1;});
  if(test.results.length!==dns.policies.length*4||!dns.policies.every(function(p){return ['domestic','proxy'].every(function(t){return ['udp','tcp'].every(function(proto){return keys[p.index+'/'+t+'/'+proto]===1;});});}))return 'stale';
  if(test.results.some(function(r){return r.state==='failed';}))return 'error';
  return test.results.every(function(r){return r.state==='ok';})?'success':'warning';
 },
 receiveState: function(data, status, now) {
  if (!data || data.schema !== 1) return 'unavailable';
  var age=now/1000-Number(data.time);
  if (!Number.isFinite(age) || age < -1 || age > 5) return 'stale';
  if (data.state !== 'observing') return ['inactive','no_sockets','unavailable'].indexOf(data.state)>=0 ? data.state : 'unavailable';
  if (!this.fresh(status,now) || status.state !== 'running' || !data.runtime_id || data.runtime_id !== status.runtime_id || !data.generation || data.generation !== status.generation) return 'stale';
  if (!['observed_drops','queue_now','peak_queue','socket_count','known_ingress_count','other_public_udp_count'].every(function(k){return Number.isSafeInteger(data[k]) && data[k]>=0;})) return 'unavailable';
  return data.coverage === 'complete' ? 'observing' : data.coverage === 'partial' ? 'partial' : 'unavailable';
 },
 diagnosticRunning: function(state, now) {
  var age = now / 1000 - Number(state && state.started_at);
  return !!state && state.state === 'running' && Number.isFinite(age) && age >= -5 && age < 30;
 },
 updateRunning: function(state, now) {
  var age = now / 1000 - Number(state && state.started_at);
  return !!state && state.state === 'running' && Number.isFinite(age) && age >= -5 && age < 180;
 },
 fresh: function(status, now) {
  var age = now / 1000 - Number(status && status.time);
  return !!status && Number.isFinite(age) && age >= -5 && age <= 15;
 },
 diagnosticStale: function(test, status, now) {
  return !test || test.stale || !this.fresh(status, now) || status.state !== 'running' ||
   !test.generation || test.generation !== status.generation ||
   !test.runtime_id || test.runtime_id !== status.runtime_id ||
   test.exit !== status.exit || test.exit_ip !== status.exit_ip ||
   test.applied_nonce !== status.applied_nonce;
 }
});

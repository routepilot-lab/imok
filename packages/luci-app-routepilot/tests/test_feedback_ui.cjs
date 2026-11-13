const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const resourceRoot = path.join(__dirname, '../htdocs/luci-static/resources');
const helpers = fs.readdirSync(path.join(resourceRoot, 'routepilot')).filter(n => /^feedback_[0-9a-f]{12}\.js$/.test(n));
assert.equal(helpers.length, 1);
class Base {}
Base.extend = function(properties) { class Module extends this {} Object.assign(Module.prototype, properties); return Module; };
const Feedback = new Function('baseclass', fs.readFileSync(path.join(resourceRoot, 'routepilot', helpers[0]), 'utf8'))(Base);
assert.equal(typeof Feedback, 'function');
assert.ok(Feedback.prototype instanceof Base, 'LuCI modules must return a baseclass subclass');
const feedback = new Feedback();
const now = 100000;
const status = {time: 99, state: 'running', generation: 'rules1', runtime_id: 'session1', applied_nonce: 'n1', exit: 'usa', exit_ip: '192.0.2.1'};
const test = {...status, stale: false};
assert.equal(feedback.fresh(status, now), true);
assert.equal(feedback.fresh({...status, time: 80}, now), false);
assert.equal(feedback.fresh({...status, time: 200}, now), false);
assert.equal(feedback.fresh({}, now), false);
assert.equal(feedback.diagnosticStale(test, status, now), false);
for (const change of [{time: 80}, {runtime_id:'session2'}, {state: 'error'}, {generation: 'rules2'}, {applied_nonce: 'n2'}, {exit: 'seoul'}, {exit_ip: '192.0.2.2'}]) {
 assert.equal(feedback.diagnosticStale(test, {...status, ...change}, now), true);
}
assert.equal(feedback.diagnosticStale({...test, generation: undefined}, status, now), true);
assert.equal(feedback.diagnosticStale({...test, stale: true}, status, now), true);
console.log('UI freshness and diagnostic invalidation: passed');

assert.equal(feedback.updateRunning({state:'running',started_at:99}, now), true);
assert.equal(feedback.updateRunning({state:'running',started_at:1}, 200000), false);
assert.equal(feedback.updateRunning({state:'unknown',started_at:99}, now), false);

assert.equal(feedback.diagnosticRunning({state:'running',started_at:99}, now), true);
assert.equal(feedback.diagnosticRunning({state:'running',started_at:60}, now), false);
assert.equal(feedback.diagnosticRunning({state:'done',started_at:99}, now), false);

const receiver={schema:1,state:'observing',time:99,runtime_id:'session1',generation:'rules1',coverage:'complete',observed_drops:3,queue_now:4096,peak_queue:8192,socket_count:1,known_ingress_count:1,other_public_udp_count:0};
assert.equal(feedback.receiveState(receiver,status,now),'observing');
assert.equal(feedback.receiveState({...receiver,coverage:'partial'},status,now),'partial');
assert.equal(feedback.receiveState({...receiver,time:90},status,now),'stale');
assert.equal(feedback.receiveState({...receiver,runtime_id:'old'},status,now),'stale');
assert.equal(feedback.receiveState({...receiver,generation:'old'},status,now),'stale');
assert.equal(feedback.receiveState({...receiver,observed_drops:null},status,now),'unavailable');
assert.equal(feedback.receiveState({...receiver,state:'unavailable'},status,now),'unavailable');
assert.equal(feedback.receiveState({...receiver,state:'no_sockets'},status,now),'no_sockets');

// A previous successful job must not mask a newly submitted request before polling catches up.
const oldJob={action:'deploy',state:'ready',started_at:99,finished_at:99.5};
const pending={started:100000,baseline:feedback.workerJobKey(oldJob),action:'deploy'};
assert.equal(feedback.workerJobProgress(oldJob,pending,101000,'cloud',0).state,'busy');
assert.equal(feedback.workerJobProgress(oldJob,pending,101000,'cloud',0).matched,false);
const uploading={action:'deploy',state:'running',phase:'uploading',started_at:100.1};
assert.deepEqual([feedback.workerJobProgress(uploading,pending,103000,'cloud',0).completed,feedback.workerJobProgress(uploading,pending,103000,'cloud',0).total],[1,4]);
assert.equal(feedback.workerJobProgress({...uploading,state:'waiting_dns'},pending,104000,'cloud',0).state,'waiting');
assert.equal(feedback.workerJobProgress({...uploading,state:'failed',message:'denied'},pending,104000,'cloud',0).state,'error');
assert.equal(feedback.workerJobProgress({...uploading,state:'ready'},pending,104000,'cloud',0).completed,4);
assert.equal(feedback.workerJobProgress({...uploading,action:'verify',state:'verified'},pending,104000,'cloud',0).matched,false);
assert.equal(feedback.workerJobProgress(oldJob,pending,401000,'cloud',0).state,'unknown');
assert.equal(feedback.workerJobProgress(null,null,101000,'cloud',0).state,'idle');
assert.equal(feedback.workerJobProgress({state:'running',started_at:100,results:[{state:'ok'}],total:6},null,101000,'relay').completed,1);
assert.equal(feedback.workerJobProgress({state:'running',started_at:1,results:[]},null,301000,'relay').state,'unknown');
console.log('Worker feedback: old success isolation, real phase counters, certificate wait, errors and missing/expired confirmation passed');

for(const state of ['unreachable','deployment_error'])assert.equal(feedback.workerJobProgress({...uploading,state},pending,104000,'cloud',0).state,'error');

const saved = { enabled: true, nonce: 'n1' };
const summary = (next, proof) => feedback.overviewSummary(saved, next, proof, now);
assert.equal(feedback.overviewSummary({ enabled: false }, null, null, now).reason, 'disabled');
assert.equal(summary({ ...status, time: 80 }).reason, 'stale');
assert.equal(summary({ ...status, state: 'waiting_devices' }).reason, 'devices');
assert.equal(summary({ ...status, egress_guard: 'blocking' }).reason, 'guard');
assert.equal(summary({ ...status, applied_nonce: 'previous' }).reason, 'apply');
assert.equal(summary({ ...status, tunnel_status: 'unknown' }).reason, 'unknown_tunnel');
assert.equal(summary(status, 'error').reason, 'website');
assert.equal(summary(status, 'success').reason, 'verified');
assert.equal(summary(status, 'warning').state, 'partial');
assert.deepEqual(summary(status, null), {
  state: 'unverified',
  reason: 'not_tested',
  page: 'overview#imok-network-diagnostics'
});
assert.equal(summary(status, 'busy').state, 'unverified');
assert.equal(summary(status, 'stale').state, 'unverified');

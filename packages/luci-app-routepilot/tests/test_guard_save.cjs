// Exercise the real imOK save entry points with local LuCI and RPC doubles.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const harness = fs.readFileSync(path.join(__dirname, 'test_feedback_render.cjs'), 'utf8').split('(async()=>{')[0];
const cases = String.raw`
(async () => {
 const steps = [], policies = [];
 let offload = true;
 uci.get = (c, s, k) => {
  const row = s === 'main' ? config : policies.find(policy => policy['.name'] === s);
  return k == null ? row : row?.[k];
 };
 uci.sections = () => policies;
 uci.save = async () => { steps.push('uci.save'); };
 const exec = mockedFS.exec;
 mockedFS.exec = (p, args) => {
  if (p !== '/usr/libexec/routepilot' || args[0] !== 'guard-preflight') return exec(p, args);
  steps.push('preflight');
  return Promise.resolve(offload ? { code: 1, stderr: '检测到防火墙流量卸载，本次未保存。' } : { code: 0 });
 };
 Map.prototype.save = async function (callback) {
  steps.push('map.save');
  if (typeof callback === 'function') await callback();
 };
 const makeSection = Map.prototype.section;
 Map.prototype.section = function () {
  const section = makeSection.apply(this, arguments);
  section.addModalOptions = () => {};
  return section;
 };
 Element.prototype.prepend = function (node) { this.children.unshift(node); };

 location.pathname = '/admin/services/routepilot/cloudflare/routing';
 config.egress_guard = '1';
 policies.push({ '.name': 'device', enabled: '1', mode: 'global', mac: '02:00:00:00:00:01', egress_guard: '1' });
 const routing = new View();
 await routing.render([null, null, [], directory]);
 assert.ok(!routing.map.children.flatMap(section => section.children).some(field => field.option === 'egress_guard'));
 assert.ok(await routing.applySettings());
 assert.deepEqual(steps, ['map.save', 'uci.save']);
 const lanNonce = config.apply_nonce;
 assert.notEqual(lanNonce, 'n1');

 commitError = 'RPC call to uci/commit failed with error -32002: Access denied\n at synthetic stack';
 assert.equal(await routing.applySettings(), false);
 assert.match(lastModal.textContent, /重新登录 LuCI/);
 assert.doesNotMatch(lastModal.textContent, /synthetic stack|配置已提交/);
 commitError = 'RPC call to uci/commit failed with ubus code 6: Permission denied';
 assert.equal(await routing.applySettings(), false);
 assert.match(lastModal.textContent, /等待其他配置应用完成/);
 commitError = null;
 assert.ok(await routing.applySettings());
 const retryNonce = config.apply_nonce;

 // Only selecting device scope would make the explicitly protected row active.
 selections.scope = 'devices';
 steps.length = 0;
 assert.equal(await routing.applySettings(), false);
 assert.deepEqual(steps, ['preflight']);
 assert.equal(config.scope, 'lan');
 assert.equal(config.apply_nonce, retryNonce);

 // A legacy inherited setting never turns on protection, even with the old main flag.
 policies[0].egress_guard = 'inherit';
 steps.length = 0;
 assert.ok(await routing.applySettings());
 assert.deepEqual(steps, ['map.save', 'uci.save']);

 location.pathname = '/admin/services/routepilot/cloudflare/devices';
 config.scope = 'devices';
 policies[0].egress_guard = '0';
 const devices = new View();
 await devices.render([null, null, [{name:'br-lan',up:true,network:'10.1.0.0/24'}], directory]);
 const fields = { enabled: '1', mode: 'global', mac: '02:00:00:00:00:01', ip:'10.1.0.20', egress_guard: '1' };
 const modalMap = { root: E('div'), async save() { steps.push('device.save'); policies[0].egress_guard = fields.egress_guard; } };
 const modalOptions = (map, values) => ({ map, children: Object.keys(values).map(option => ({ option, formvalue: () => values[option] })) });
 const policySection = devices.map.children[0];
 const guardOption = policySection.children.find(field => field.option === 'egress_guard');
 assert.equal(guardOption.modalonly, true);
 assert.equal(guardOption.default, '0');
 policySection.addModalOptions(modalOptions(modalMap, fields), 'device');
 fields.ip = '192.168.8.100';
 steps.length = 0;
 await assert.rejects(() => modalMap.save(), /IPv4 不属于所选 LAN 网段/);
 assert.deepEqual(steps, []);
 fields.ip = '10.1.0.20';
 offload = true;
 steps.length = 0;
 await assert.rejects(() => modalMap.save(), /本次未保存/);
 assert.deepEqual(steps, ['preflight']);
 assert.equal(policies[0].egress_guard, '0');
 assert.ok(modalMap.root.textContent.includes('本次未保存'));
 offload = false;
 steps.length = 0;
 await modalMap.save();
 assert.deepEqual(steps, ['preflight', 'device.save']);
 assert.equal(policies[0].egress_guard, '1');

 // Apply rechecks the modal draft in case the firewall changed meanwhile.
 offload = true;
 steps.length = 0;
 const pendingNonce = config.apply_nonce;
 assert.equal(await devices.applySettings(), false);
 assert.deepEqual(steps, ['preflight']);
 assert.equal(config.apply_nonce, pendingNonce);

 // A conflicted pre-existing setting can still be turned off.
 fields.egress_guard = '0';
 steps.length = 0;
 await modalMap.save();
 assert.deepEqual(steps, ['device.save']);
 assert.equal(policies[0].egress_guard, '0');

 // LuCI can open a modal for a new section before it appears in uci.sections().
 const addedFields = { enabled: '1', mode: 'global', mac: '02:00:00:00:00:02', egress_guard: '1' };
 const newRowMap = { root: E('div'), async save() { steps.push('device.add'); policies.push({ '.name': 'new_device', ...addedFields }); } };
 policySection.addModalOptions(modalOptions(newRowMap, addedFields), 'new_device');
 steps.length = 0;
 await assert.rejects(() => newRowMap.save(), /本次未保存/);
 assert.deepEqual(steps, ['preflight']);
 assert.equal(policies.length, 1);
 offload = false;
 steps.length = 0;
 await newRowMap.save();
 assert.deepEqual(steps, ['preflight', 'device.add']);
 assert.equal(policies[1].egress_guard, '1');
 console.log('Guard preflight follows explicit device policies and checks modal saves and pending apply.');
})().catch(error => { console.error(error); process.exitCode = 1; });
`;
vm.runInNewContext(harness + cases, { require, __dirname, console, Buffer, TextEncoder, setImmediate, process }, { filename: __filename });

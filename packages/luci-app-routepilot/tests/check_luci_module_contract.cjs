// Optional ABI check using a locally obtained LuCI core; no network or router writes.
// node tests/check_luci_module_contract.cjs /absolute/path/to/luci.js
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const crypto = require('node:crypto');
assert.ok(process.argv[2], 'Supply the LuCI core from the intended target; no stub fallback');
const raw = fs.readFileSync(process.argv[2]);
const source = raw.toString('utf8');
const begin = source.indexOf('const env='), end = source.indexOf(';const Headers=Class.extend');
assert.ok(begin >= 0 && end > begin, 'Unsupported LuCI source layout; inspect it before updating this check');
// Execute only the inspected Class definition prefix, never the whole browser runtime.
const Class = new Function(source.slice(begin, end) + ';return Class;')();
const root = path.join(__dirname, '..');
const menu = JSON.parse(fs.readFileSync(path.join(root, 'root/usr/share/luci/menu.d/luci-app-routepilot.json')));
const view = Object.values(menu)[0].action.path;
const viewSource = fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/view', view + '.js'), 'utf8');
const helper = viewSource.match(/require routepilot\.(feedback_[0-9a-f]+) as feedback/)[1];
const moduleSource = fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/routepilot', helper + '.js'), 'utf8');
assert.equal(Class.isSubclass({diagnosticStale(){}}), false, 'Plain object regression must be rejected');
const Feedback = new Function('baseclass', moduleSource)(Class);
assert.ok(Class.isSubclass(Feedback), 'Same constructor predicate as the inspected LuCI loader');
const instance = Feedback.instantiate([]);
const now = Date.now();
const status = {state:'running',time:now/1000,runtime_id:'after',generation:'rules',exit:'usa',exit_ip:'192.0.2.1',applied_nonce:'same'};
assert.equal(instance.diagnosticStale({...status,runtime_id:'before'}, status, now), true);
assert.equal(instance.diagnosticStale({...status}, status, now), false);
const clientName = viewSource.match(/L.require\('routepilot\.(clients_[0-9a-f]+)'\)/)[1];
const clientSource = fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/routepilot', clientName + '.js'), 'utf8');
const Client = new Function('baseclass', 'fs', 'ui', 'poll', clientSource)(Class, {}, {}, {});
assert.ok(Class.isSubclass(Client), 'Client access must satisfy the target LuCI constructor predicate');
const clientInstance = Client.instantiate([]);
assert.equal(typeof clientInstance.load, 'function');
assert.equal(typeof clientInstance.render, 'function');
const pageImports = Array.from(viewSource.matchAll(/'require routepilot\.(page_[a-z]+_[a-f0-9]{12}) as (page[A-Z][A-Za-z]*)';/g));
for (const [,name] of pageImports) {
  const pageSource = fs.readFileSync(path.join(root, 'htdocs/luci-static/resources/routepilot', name + '.js'), 'utf8');
  const Page = new Function('baseclass', pageSource)(Class);
  assert.ok(Class.isSubclass(Page), name + ' must satisfy the target LuCI constructor predicate');
  assert.ok(Page.instantiate([]));
}
console.log(JSON.stringify({page_modules:pageImports.length,luci_core_sha256:crypto.createHash('sha256').update(raw).digest('hex'),abi:'passed',client_abi:'passed',same_exit_recovery:'passed'}));

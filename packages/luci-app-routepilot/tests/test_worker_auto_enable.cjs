const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const harness = fs.readFileSync(path.join(__dirname, 'test_feedback_render.cjs'), 'utf8')
  .split('(async()=>{')[0];
const cases = String.raw`
(async () => {
  location.pathname = '/admin/services/routepilot/cloudflare/workers';
  config.backend = 'masque';
  const nodeInfo = {configured:true, fingerprint:'fixture-node', name:'Fixture', address:'example.invalid', port:443};
  const baseRead = mockedFS.read.bind(mockedFS);
  const baseExec = mockedFS.exec.bind(mockedFS);
  let proof;
  let commands;
  let sequence = 0;
  mockedFS.read = file => {
    if (file.endsWith('backup-node.json')) return Promise.resolve(JSON.stringify(nodeInfo));
    if (file.endsWith('backup-test.json')) return Promise.resolve(JSON.stringify(proof));
    return baseRead(file);
  };
  mockedFS.exec = (file, args) => {
    if (args[0] === 'backup-test' || args[0] === 'backup-enable') {
      commands.push(args[0]);
      return Promise.resolve({code:0, stdout:JSON.stringify(args[0] === 'backup-test'
        ? {state:'submitted',job_id:'job-'+sequence} : {backend:'worker',nonce:'requested'})});
    }
    return baseExec(file, args);
  };
  for (const usable of [true, false]) {
    config.backend = 'masque';
    commands = [];
    sequence++;
    proof = {state:'done',usable:true,finished_at:Date.now()/1000-301,fingerprint:'fixture-node',results:[]};
    const rendered = await new View().render([null,null,[],{},null,{node:nodeInfo,nodes:[nodeInfo]},null,{}]);
    await tick();
    const button = rendered.find(e => e.tag==='button' && e.textContent==='手动启用备用');
    assert.ok(button);
    assert.equal(button.disabled, false);
    await button.attrs.click();
    assert.deepEqual(commands, ['backup-test']);
    proof = {state:'done',usable:true,job_id:'unrelated',finished_at:Date.now()/1000,
      fingerprint:'fixture-node',results:Array.from({length:5},(_,i)=>({id:String(i),state:'ok'}))};
    await tick();
    assert.deepEqual(commands, ['backup-test'], 'a different job cannot authorize enabling');
    proof = {...proof, job_id:'job-'+sequence, usable};
    await tick();
    assert.deepEqual(commands, usable ? ['backup-test','backup-enable'] : ['backup-test']);
    if (!usable) assert.ok(rendered.textContent.includes('未启用备用'));
  }
  console.log('Workers: expired proof starts a new test and only the matching successful job can continue enabling.');
})().catch(error => { console.error(error); process.exitCode = 1; });
`;
vm.runInNewContext(harness + cases, { require, __dirname, console, Buffer, TextEncoder, setImmediate, process }, { filename: __filename });

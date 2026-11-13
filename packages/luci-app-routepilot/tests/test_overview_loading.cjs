// First status uses the real view load/render; synthetic RPCs never touch a router.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let harness=fs.readFileSync(path.join(__dirname,'test_feedback_render.cjs'),'utf8').split('(async()=>{')[0];
harness=harness.replace('add(fn){tick=fn;}','add(fn,interval){tick=fn;globalThis.pollInterval=interval;}');
const cases=String.raw`
(async()=>{
 location.pathname='/admin/services/routepilot/overview';
 const originalRead=mockedFS.read.bind(mockedFS),before=JSON.stringify(config);
 current={...current,protocol:'MASQUE',state:'running',time:Date.now()/1000};
 let resolveStatus;
 mockedFS.read=p=>p.endsWith('/status.json')?new Promise(resolve=>{readCalls.push(p);resolveStatus=resolve;}):originalRead(p);
 readCalls=[];const view=new View(),data=await view.load();
 assert.ok(data.initialOverviewRead instanceof Promise,'snapshot must not block page load');
 const node=await view.render(data);await new Promise(setImmediate);
 assert.ok(!node.textContent.includes('隧道已连接'),'pending read must not fabricate connected status');
 const firstPoll=tick(),readsBefore=readCalls.length;
 resolveStatus(JSON.stringify(current));await firstPoll;await new Promise(setImmediate);
 assert.ok(node.textContent.includes('隧道已连接'),'initial snapshot paints before the next poll');
 assert.equal(readCalls.length,readsBefore,'first poll reuses the startup request');
 assert.equal(pollInterval,2);
 mockedFS.read=originalRead;const readsAfter=readCalls.length;await tick();
 assert.equal(readCalls.length-readsAfter,6,'later polls include the two relocated diagnostic snapshots');
 assert.equal(JSON.stringify(config),before);assert.deepEqual(commandCalls,[]);
 // A failed startup status cannot suppress the first recovery poll or report success.
 mockedFS.read=p=>p.endsWith('/status.json')?Promise.reject(Error('synthetic transport error')):originalRead(p);
 const failedView=new View(),failedData=await failedView.load(),failedNode=await failedView.render(failedData);await new Promise(setImmediate);
 assert.ok(!failedNode.textContent.includes('隧道已连接'));
 mockedFS.read=originalRead;await tick();assert.ok(failedNode.textContent.includes('隧道已连接'));
 // An abandoned page cannot paint a delayed snapshot into its former view.
 let release;const delayed=[null,null,[],directory,null,{nodes:[]},null,{}];
 delayed.initialOverviewRead=new Promise(resolve=>{release=resolve;});
 const abandoned=await new View().render(delayed);location.pathname='/admin/services/routepilot/backup';
 release([current,null,test]);await new Promise(setImmediate);
 assert.ok(!abandoned.textContent.includes('隧道已连接'));
 console.log('Overview startup: nonblocking read, immediate true status, first-poll reuse, unchanged interval, failure recovery and abandoned-page protection passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
`;
vm.runInNewContext(harness+cases,{require,__dirname,console,Buffer,TextEncoder,setImmediate,process},{filename:__filename});

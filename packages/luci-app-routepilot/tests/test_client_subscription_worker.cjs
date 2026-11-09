const assert=require('node:assert/strict'),path=require('node:path'),{pathToFileURL}=require('node:url'),crypto=require('node:crypto');
(async()=>{
 const {default:worker}=await import(pathToFileURL(path.join(__dirname,'../root/usr/share/routepilot/client-subscription/worker.mjs')));
 const token=Buffer.alloc(32,7).toString('base64url'),uri='masque://162.159.197.2:443?privateKey=SYNTHETIC#Test',body=Buffer.from(uri+'\n').toString('base64'),revision=crypto.createHash('sha256').update(body).digest('hex'),key='S_'+crypto.createHash('sha256').update(token).digest('hex'),env={[key]:JSON.stringify({schema:1,body,revision})};
 const url='https://imok-clients-000000000001.example.workers.dev/s/'+token;
 for(const method of ['GET','HEAD']){const r=await worker.fetch(new Request(url,{method}),env);assert.equal(r.status,200);assert.ok(r.headers.get('Cache-Control').includes('no-store'));assert.equal(r.headers.get('Referrer-Policy'),'no-referrer');assert.equal(await r.text(),method==='HEAD'?'':body+'\n');}
 for(const target of [url.replace('https:','http:'),url+'/extra',url.replace(token,'A'.repeat(43)),url.replace('/s/','/admin/'),url.split('/s/')[0]+'/']){const r=await worker.fetch(new Request(target),env);assert.equal(r.status,404);assert.ok(!(await r.text()).includes('SYNTHETIC'));}
 assert.equal((await worker.fetch(new Request(url,{method:'POST',body:'overwrite'}),env)).status,405);
 assert.equal((await worker.fetch(new Request(url),{[key]:'malformed'})).status,503);
 const changed=Buffer.from(uri.replace(':443?',':8443?')+'\n').toString('base64');env[key]=JSON.stringify({schema:1,body:changed,revision:crypto.createHash('sha256').update(changed).digest('hex')});
 assert.equal((await worker.fetch(new Request(url),env)).status,200);assert.equal((await (await worker.fetch(new Request(url),env)).text()).trim(),changed);
 delete env[key];assert.equal((await worker.fetch(new Request(url),env)).status,404);
 console.log('Private subscription Worker: GET/HEAD, stable URL updates, invalid-token isolation, no public writes, no-store and disable passed');
})().catch(e=>{console.error(e);process.exit(1);});

// Client module contract and isolated loading; never executes an enrollment.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=path.join(__dirname,'..'),menu=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/luci/menu.d/luci-app-routepilot.json')));
const source=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/view',menu['admin/services/routepilot'].action.path+'.js'),'utf8');
const name=source.match(/L.require\('routepilot\.(clients_[a-f0-9]{12})'\)/)[1],moduleSource=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/routepilot',name+'.js'),'utf8');
assert.equal(name,'clients_'+crypto.createHash('sha256').update(moduleSource).digest('hex').slice(0,12));
class Base{}Base.extend=function(props){class Mod extends Base{}Object.assign(Mod.prototype,props);return Mod;};
const requests=[],data={schema:1,clients:[],capabilities:{shadowrocket:true,vnet:false}};
const moduleFS={exec(file,args){requests.push([file,args]);return Promise.resolve({code:0,stdout:JSON.stringify(data)});}};
const Client=new Function('baseclass','fs','ui','poll',moduleSource)(Base,moduleFS,{},{});
assert.ok(Client.prototype instanceof Base);
(async()=>{
 const c=new Client();assert.deepEqual(await c.load(),data);assert.deepEqual(requests,[['/usr/libexec/routepilot-clients-list',[]]]);
 for(const response of [{code:0,stdout:'null'},{code:1,stdout:'{"error":"权限不足"}'},{code:0,stdout:'{'},{code:0,stdout:'{"clients":{}}'},{code:0,stdout:JSON.stringify({schema:1,clients:[null],capabilities:{}})},{code:0,stdout:JSON.stringify({schema:1,clients:[{id:'a'.repeat(32),name:'Bad',organization:'example-team'}],capabilities:{}})}]){
  moduleFS.exec=()=>Promise.resolve(response);const v=await c.load();assert.ok(v.load_error);assert.ok(!/Cannot read|TypeError|undefined/.test(v.load_error));assert.deepEqual(v.clients,[]);
 }
 const qrEnd=moduleSource.indexOf('return baseclass.extend({');assert.ok(qrEnd>50000);
 const qr=new Function(moduleSource.slice(0,qrEnd)+';return qrcode;')();
 const uri='masque://162.159.197.2:443?privateKey=SIMULATED-NOT-A-KEY&ip=100.96.0.88#SIMULATED';
 const code=qr(0,'M');code.addData(uri);code.make();const gif=Buffer.from(code.createDataURL(4,16).split(',')[1],'base64');
 assert.equal(gif.subarray(0,6).toString(),'GIF87a');assert.ok(code.getModuleCount()>=21);assert.ok(gif.length>100);
 const begin=source.indexOf('  function requestChecksum('),end=source.indexOf('  function backup(',begin);
 const bound=new Function('window','TextEncoder','_',source.slice(begin,end)+';return boundRequest;')({crypto:crypto.webcrypto},TextEncoder,x=>x);
 const request=bound({name:'中文客户端',token:'synthetic-only'});assert.match(request.id,/^[a-f0-9]{32}$/);assert.equal(request.token,request.id+':'+crypto.createHash('sha256').update(request.text).digest('hex'));
 const loadBegin=source.indexOf(' load: function()'),loadEnd=source.indexOf('\n render: function',loadBegin);const load=source.slice(loadBegin,loadEnd);
 assert.ok(load.indexOf("if(leaf==='clients')")<load.indexOf("uci.load('routepilot')"),'Client route must return before main UCI and runtime loading');
 assert.ok(moduleSource.includes("poll.add(function()"));assert.ok(moduleSource.includes('r.busy;'),'No idle client polling');
 assert.ok(!moduleSource.includes("fs.read('/opt/routepilot/runtime"));assert.ok(!moduleSource.includes("fs.exec('/usr/libexec/routepilot',"));

 // Regression: QA-004/005 — valid HTML v-pattern and stale-list isolation.
 // Found by /qa on 2026-11-09; report: docs/quality/system-audit-20261109.md
 // Value: protects=invalid team input and post-save list freshness; fails_when=browser ignores pattern or old reads replace new state;
 // why_new=previous tests only load lists and execute QR/request ABI; seam=none;
 // no_claim=provider writes (synthetic DOM/network-free function boundary).
 const attrs=[];new Function('E','button','field','modal','modalActions',moduleSource.slice(moduleSource.indexOf('  function newClient('),moduleSource.indexOf('  function registerClient('))+';return newClient;')((tag,a)=>{attrs.push(a);return {};},()=>({}),()=>null,()=>{},()=>null)();
 const org=attrs.find(a=>a.placeholder==='团队名称'),pattern=new RegExp('^(?:'+org.pattern+')$','v');
 for(const value of ['a','example-team','TEAM1','a'.repeat(63)])assert.ok(pattern.test(value),value);
 for(const value of ['-team','team-','invalid$$','a'.repeat(64),'a.b'])assert.ok(!pattern.test(value),value);
 let release,reads=0,draws=[];
 const fakeRoot={isConnected:true,setAttribute(){},removeAttribute(){},querySelectorAll(){return [];}};
 const make=new Function('root','fs','self','bindRequest','ensurePrepared','draw','message',
  'var inFlight=false,refreshing=false,refreshSerial=0,data={clients:[]};'+moduleSource.slice(moduleSource.indexOf('  function request(action,payload)'),moduleSource.indexOf('  function newClient('))+';return {request,refresh,data:()=>data};');
 const api=make(fakeRoot,{write:()=>Promise.resolve(),exec:()=>Promise.resolve({code:0,stdout:'{"request_id":"test"}'})},{load:()=>++reads===1?new Promise(resolve=>{release=resolve;}):Promise.resolve({clients:[{id:'new'}]})},()=>({id:'test',token:'test',text:'{}'}),()=>Promise.resolve(),()=>draws.push(true),()=>{});
 const oldRead=api.refresh(true);await api.request('edit',{});await api.refresh(false);release({clients:[{id:'old'}]});await oldRead;
 assert.equal(api.data().clients[0].id,'new','a list read started before edit must not replace post-edit state');assert.equal(reads,2);assert.equal(draws.length,1);
 const nullAPI=make(fakeRoot,{write:()=>Promise.resolve(),exec:()=>Promise.resolve({code:0,stdout:'null'})},{load:()=>Promise.resolve({clients:[]})},()=>({id:'test',token:'test',text:'{}'}),()=>Promise.resolve(),()=>{},()=>{});
 await assert.rejects(nullAPI.request('edit',{}),/客户端操作响应无效/);

 console.log('Client access: ABI, lazy load, read-only entry, failure states, offline QR, HTTP-safe request checksum and main-flow separation passed');
})().catch(e=>{console.error(e);process.exit(1);});

// Execute the exact target LuCI modal methods, not the permissive preview mock.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{test}=require('node:test');
const root=path.join(__dirname,'..'),menu=JSON.parse(fs.readFileSync(path.join(root,'root/usr/share/luci/menu.d/luci-app-routepilot.json'))),view=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/view',menu['admin/services/routepilot'].action.path+'.js'),'utf8');
const name=view.match(/L.require\('routepilot\.(clients_[a-f0-9]{12})'\)/)[1],source=fs.readFileSync(path.join(root,'htdocs/luci-static/resources/routepilot',name+'.js'),'utf8');
class Element{
 constructor(tag,attrs={},children=[]){this.tag=tag;this.attrs={...attrs};this.value=attrs.value||'';this.children=Array.isArray(children)?children:[children];this.classList={add:(...tokens)=>{for(const token of tokens){if(!token||/\s/.test(token))throw new DOMException('Invalid class token','InvalidCharacterError');this.attrs.class=((this.attrs.class||'')+' '+token).trim();}},remove:(...tokens)=>{this.attrs.class=(this.attrs.class||'').split(/\s+/).filter(v=>!tokens.includes(v)).join(' ');},contains:token=>(this.attrs.class||'').split(/\s+/).includes(token)};}
 setAttribute(k,v){this.attrs[k]=v;}replaceChildren(...children){this.children=children;}click(){this.attrs.click?.({detail:0});}addEventListener(type,listener){this.attrs[type]=listener;}
}
const E=(tag,attrs,children)=>new Element(tag,attrs,children);
function setup(){
 const dlg=E('div'),document={body:E('body'),removeEventListener(){}};
 const modalDiv={firstElementChild:dlg,focus(){},blur(){},querySelector(selector){
  assert.equal(selector,'.right > button, .right > .btn, .button-row > .btn');
  function find(node){if(!(node instanceof Element))return null;if(node.classList.contains('right')||node.classList.contains('button-row')){const b=node.children.find(c=>c instanceof Element&&(c.tag==='button'||c.classList.contains('btn')));if(b)return b;}for(const child of node.children){const b=find(child);if(b)return b;}return null;}
  return find(dlg);
 }};
 const dom={create:E,content(node,children){node.children=Array.isArray(children)?children:[children];},append(node,children){node.children.push(...(Array.isArray(children)?children:[children]));}};
 const ui=require('./fixtures/luci_modal_contract.cjs')(modalDiv,dom,document);return {ui,document,dlg,dom};
}
test('client modal passes distinct tokens to native LuCI classList.add',()=>{
 const env=setup(),match=source.match(/^([ ]*)function modal\(title,\s*children\)\s*\{[\s\S]*?^\1\}/m);assert.ok(match);
 const modal=new Function('ui','var exportCleanup=null;'+match[0]+';return modal;')(env.ui);modal('Client',[]);
 assert.ok(env.dlg.classList.contains('imok-dialog'));assert.ok(env.dlg.classList.contains('imok-client-dialog'));
});
test('native LuCI Escape activates the cancel button of all client modals',()=>{
 const phone={name:'Phone',id:'a'.repeat(32),organization:'example-team',service_auth_saved:true};
 for(const [name,next,args]of [['newClient','registerClient',[]],['registerClient','forgetServiceAuth',[phone]],['forgetServiceAuth','confirmAction',[phone]],['confirmAction','publishSubscription',[phone,'revoke']],['publishSubscription','exportClient',[phone]],['exportClient','draw',[phone]]]){
  const env=setup(),start=source.indexOf('  function '+name+'('),end=source.indexOf('  function '+next+'(',start);assert.ok(start>=0&&end>start);
  const footerMatch=source.match(/^([ ]*)function modalActions\(buttons\)\s*\{[\s\S]*?^\1\}/m),footer=footerMatch?new Function('E',footerMatch[0]+';return modalActions;')(E):undefined;
  const button=(label,click)=>E('button',{class:'btn',click},[label]),field=(label,input)=>E('label',{},[label,input]);
  const modal=(_,children)=>{env.dom.content(env.dlg,children);env.document.body.classList.add('modal-overlay-active');};
  const build=new Function('E','button','field','modal','ui','modalActions','document','dirty','currentModal','data','_','var exportCleanup=null;'+source.slice(start,end)+';return '+name+';');
  const open=build(E,button,field,modal,env.ui,footer,env.document,()=>false,()=>true,{team:{name:'example-team'},capabilities:{service_token:true}}, x=>x);
  open(...args);
  const credential=env.dlg.children.flatMap(e=>e.children||[]).flatMap(e=>e.children||[]).find(e=>e.attrs?.type==='password');
  if(name==='registerClient'){assert.ok(credential);credential.value='synthetic-credential';}
  env.ui.cancelModal({key:'Escape'});assert.ok(!env.document.body.classList.contains('modal-overlay-active'),name+' has no working native Escape cancel');
  if(credential)assert.equal(credential.value,'');
 }
});

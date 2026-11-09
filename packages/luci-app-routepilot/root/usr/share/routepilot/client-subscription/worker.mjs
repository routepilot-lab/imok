// iMOK private client subscriptions. No proxying, logging, cookies or admin API.
const headers = {'Content-Type':'text/plain; charset=utf-8','Cache-Control':'no-store, max-age=0','Pragma':'no-cache','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'};
export default {
 async fetch(request, env) {
  const url=new URL(request.url);
  if(!['GET','HEAD'].includes(request.method))return new Response('Method not allowed',{status:405,headers});
  const match=url.pathname.match(/^\/s\/([A-Za-z0-9_-]{43})$/);
  if(url.protocol!=='https:'||!match)return new Response('Not found',{status:404,headers});
  try {
   const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(match[1]));
   const key='S_'+Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
   const raw=env[key];
   if(typeof raw!=='string')return new Response('Not found',{status:404,headers});
   const value=JSON.parse(raw);
   if(value.schema!==1||!/^[a-f0-9]{64}$/.test(value.revision)||typeof value.body!=='string'||!/^[A-Za-z0-9+/=\r\n]{1,12000}$/.test(value.body))return new Response('Unavailable',{status:503,headers});
   return new Response(request.method==='HEAD'?null:value.body+'\n',{headers});
  }catch{return new Response('Unavailable',{status:503,headers});}
 }
};

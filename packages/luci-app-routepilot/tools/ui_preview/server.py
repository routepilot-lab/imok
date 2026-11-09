#!/usr/bin/env python3
"""Loopback-only preview of the real view with synthetic data; never proxies RPC."""
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import json,subprocess,urllib.parse,argparse
PROJECT=Path(__file__).resolve().parents[4];PACKAGE=PROJECT/'packages/luci-app-routepilot';HERE=Path(__file__).parent
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  url=urllib.parse.urlsplit(self.path);path=url.path
  try:
   if path=='/meta.json':data=json.dumps({'evidence_directory':str(PROJECT/'.local/ui-v2-all'),'client_access_evidence_directory':str(PROJECT/'.local/client-access-20261108')}).encode();kind='application/json'
   elif path=='/source.json':
    baseline=urllib.parse.parse_qs(url.query).get('baseline',[''])[0];ref='pre-client-access-20261108-7186c53' if baseline=='clients' else 'pre-imok-ui-v2-all-pages-20261108' if baseline=='overview' else 'pre-imok-ui-v2-20261108'
    def read(rel):
     return subprocess.check_output(['git','show',ref+':'+rel],cwd=PROJECT).decode() if baseline else (PROJECT/rel).read_text()
    prefix='packages/luci-app-routepilot/';menu=json.loads(read(prefix+'root/usr/share/luci/menu.d/luci-app-routepilot.json'));name=menu['admin/services/routepilot']['action']['path'];view=read(prefix+'htdocs/luci-static/resources/view/'+name+'.js');import re
    helper=re.search(r'require routepilot\.(feedback_[a-f0-9]+)',view)[1];css=re.search(r"routepilot/(style_[a-f0-9]+\.css)",view)[1]
    client=re.search(r"L.require\('routepilot\.(clients_[a-f0-9]{12})'\)",view)
    data=json.dumps(dict(view=view,helper=read(prefix+'htdocs/luci-static/resources/routepilot/'+helper+'.js'),clients=read(prefix+'htdocs/luci-static/resources/routepilot/'+client[1]+'.js') if client else None,css=read(prefix+'htdocs/luci-static/resources/routepilot/'+css),version=json.loads(read(prefix+'root/usr/share/routepilot/version.json')),baseline=baseline)).encode();kind='application/json'
   elif path=='/gallery':data=(PROJECT/'docs/ui-v2-all/gallery.html').read_bytes();kind='text/html'
   elif path.startswith('/evidence/') and __import__('re').fullmatch(r'[a-z0-9-]+\.png',path.removeprefix('/evidence/')):data=(PROJECT/'docs/ui-v2-all/screenshots'/path.removeprefix('/evidence/')).read_bytes();kind='image/png'
   elif path=='/theme.css':data=(PROJECT/'.local/fixtures/argon-cascade.css').read_bytes();kind='text/css'
   elif path=='/client.js':data=(HERE/'client.js').read_bytes();kind='text/javascript'
   elif path=='/' or path.startswith('/cgi-bin/luci/admin/services/routepilot'):
    data=(HERE/'index.html').read_bytes();kind='text/html'
   elif path.startswith('/assets/'):data=b'';kind='text/css'
   else:self.send_error(404);return
   self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self' data:; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; frame-src 'none'");self.end_headers();self.wfile.write(data)
  except Exception:self.send_error(500,'Preview asset unavailable')
 def do_POST(self):self.send_error(405,'No backend endpoints in preview')
 def log_message(self,*args):pass
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8767);a=p.parse_args();print(f'Isolated synthetic preview: http://127.0.0.1:{a.port}',flush=True);ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()

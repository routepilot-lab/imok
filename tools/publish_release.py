#!/usr/bin/env python3
"""Build the reviewed public artifacts; publish only with an ephemeral Actions token."""
import argparse,hashlib,importlib.util,json,os,re,stat,subprocess,sys,urllib.error,urllib.parse,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPOSITORY='routepilot-lab/imok'
API='https://api.github.com/repos/'+REPOSITORY

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare():
    package=ROOT/'packages/luci-app-routepilot'
    sys.path.insert(0,str(package/'tools'))
    spec=importlib.util.spec_from_file_location('release_build',package/'tools/build.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    checks=json.loads((ROOT/'docs/PUBLICATION_CHECKS.json').read_text())
    assert checks['version']==builder.VERSION and checks['all_passed'],'Unreviewed package version'
    out=ROOT/'.local/release';out.mkdir(parents=True,exist_ok=True)
    ipk=builder.build(out)
    assert digest(ipk)==checks['integrity']['package_sha256'],'IPK differs from the reviewed router package'
    version=builder.VERSION;archive=out/('imok-'+version+'-public-source.zip')
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name in sorted(n for n in names if n):
            path=Path(name)
            assert not any(p in ['.git','.local','dist','runtime','__pycache__','node_modules'] for p in path.parts),name
            assert not any(p.startswith('.env') for p in path.parts),name
            f=ROOT/path;assert f.is_file() and not f.is_symlink(),name
            item=zipfile.ZipInfo('imok-'+version+'/'+name,date_time=(2026,11,8,0,0,0))
            item.create_system=3;item.compress_type=zipfile.ZIP_DEFLATED
            item.external_attr=(stat.S_IFREG|stat.S_IMODE(f.stat().st_mode))<<16
            z.writestr(item,f.read_bytes())
    sums=out/'SHA256SUMS';sums.write_text(''.join(digest(p)+'  '+p.name+'\n' for p in [ipk,archive]))
    return version,out,[ipk,archive,sums]

def api(method,path,payload=None,data=None,content_type='application/json'):
    assert os.environ.get('GITHUB_REPOSITORY')==REPOSITORY,'Publishing is limited to the upstream repository'
    token=os.environ.get('GITHUB_TOKEN');assert token,'No Actions token available'
    url=path if path.startswith('https://uploads.github.com/') else API+path
    assert urllib.parse.urlsplit(url).hostname in ['api.github.com','uploads.github.com']
    raw=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else data
    req=urllib.request.Request(url,data=raw,method=method,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':content_type,'User-Agent':'imok-release'})
    try:
        with urllib.request.urlopen(req,timeout=90) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        if method=='GET' and error.code==404:return None
        raise RuntimeError('GitHub request failed: '+method+' '+str(error.code)) from None

def verify_assets(release,files):
    assets={a['name']:a for a in release.get('assets',[])}
    assert set(assets)=={p.name for p in files},'Unexpected release assets'
    for p in files:
        a=assets[p.name]
        assert a.get('state')=='uploaded' and a.get('size')==p.stat().st_size and a.get('digest')=='sha256:'+digest(p),'Release asset mismatch: '+p.name

def publish(version,files,head,body):
    assert re.fullmatch('[0-9a-f]{40}',head),'Invalid source commit'
    tag='v'+version;ref=api('GET','/git/ref/tags/'+tag)
    assert not ref or ref['object']['type']=='commit' and ref['object']['sha']==head,'Refuse to replace an existing tag'
    release=api('GET','/releases/tags/'+tag)
    if release and not release.get('draft'):
        assert ref and not release.get('prerelease'),'Existing public release differs'
        verify_assets(release,files)
        return release
    if release:
        assert release['target_commitish']==head and not release.get('prerelease'),'Unrelated draft release'
    else:
        release=api('POST','/releases',{'tag_name':tag,'target_commitish':head,'name':'iMOK 2.0 · 完整界面版（'+tag+'）','body':body,'draft':True,'prerelease':False,'make_latest':'false'})
    existing={a['name']:a for a in release.get('assets',[])}
    assert set(existing)<={p.name for p in files},'Unexpected draft attachments'
    for p in files:
        if p.name in existing:
            verify_assets({'assets':[existing[p.name]]},[p]);continue
        url=release['upload_url'].split('{')[0]+'?'+urllib.parse.urlencode({'name':p.name})
        uploaded=api('POST',url,data=p.read_bytes(),content_type='application/zip' if p.suffix=='.zip' else 'application/octet-stream')
        verify_assets({'assets':[uploaded]},[p])
    staged=api('GET','/releases/'+str(release['id']));verify_assets(staged,files)
    final=api('PATCH','/releases/'+str(release['id']),{'draft':False,'prerelease':False,'make_latest':'true'})
    verify_assets(final,files)
    final_ref=api('GET','/git/ref/tags/'+tag)
    assert final_ref['object']['sha']==head and not final['draft'] and not final['prerelease'],'Publication state mismatch'
    return final

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    version,out,files=prepare()
    report={'version':version,'assets':[{ 'name':p.name,'sha256':digest(p),'size':p.stat().st_size} for p in files]}
    if args.publish:
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        assert head==os.environ.get('GITHUB_SHA'),'Checkout differs from the triggering commit'
        release=publish(version,files,head,(ROOT/'docs/releases'/('v'+version+'.md')).read_text())
        report.update(url=release['html_url'],published=True,source_commit=head)
    (out/'receipt.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()

#!/usr/bin/env python3
"""Build the reviewed public artifacts; publish only with an ephemeral Actions token."""
import argparse,hashlib,importlib.util,json,os,re,stat,subprocess,sys,urllib.error,urllib.parse,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPOSITORY='routepilot-lab/imok'
API='https://api.github.com/repos/'+REPOSITORY

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

PUBLIC_DOCS = {'docs/CLIENT_ACCESS.md', 'docs/FAQ.md', 'docs/INSTALL_KIT.md', 'docs/PUBLICATION_CHECKS.json',
               'docs/USER_FEEDBACK.md', 'docs/WARP_RUNTIME.md', 'docs/WORKERS_SETUP.md', 'docs/quality/system-audit-20261109.md',
               'docs/quality/warp-maintenance-0.2.37-1.md'}
PUBLIC_TOP = {'README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.txt', '.gitignore',
              'tools/publish_release.py', 'tools/test_publish_release.py'}
PUBLIC_IMAGES = {'docs/images/imok-overview.png', 'docs/images/imok-clients.png', 'docs/images/imok-workers.png'}


def public_paths(names):
    """Explicit public scope; never infer safety from .gitignore alone."""
    result = []
    for name in sorted(set(names)):
        p = Path(name)
        if p.is_absolute() or any(part in {'.git', '.local', 'dist', 'runtime', '__pycache__', 'node_modules', '..'} or part.startswith('.env') for part in p.parts):
            continue
        if p.suffix.lower() in {'.key', '.pem', '.db', '.sqlite', '.sqlite3', '.pyc', '.ipk', '.tgz'}:
            continue
        if name in PUBLIC_TOP | PUBLIC_DOCS | PUBLIC_IMAGES or name.startswith(('packages/luci-app-routepilot/', '.github/')) or re.fullmatch(r'docs/releases/v[0-9.-]+\.md', name):
            result.append(name)
    return result


def write_zip(target, entries):
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in sorted(entries):
            item = zipfile.ZipInfo(name, date_time=(2026, 11, 9, 0, 0, 0))
            item.create_system = 3; item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(item, data)


def install_kit(out, version, ipk, source):
    prefix = 'imOK-' + version + '/'
    entries = [(prefix + ipk.name, ipk.read_bytes(), 0o644), (prefix + source.name, source.read_bytes(), 0o644)]
    for name, origin in [('开始阅读.md', ROOT/'docs/INSTALL_KIT.md'), ('本版更新.md', ROOT/'docs/releases'/('v'+version+'.md')),
                         ('LICENSE', ROOT/'LICENSE'), ('THIRD_PARTY_NOTICES.txt', ROOT/'THIRD_PARTY_NOTICES.txt')]:
        entries.append((prefix + name, origin.read_bytes(), 0o644))
    sums = ''.join(hashlib.sha256(data).hexdigest()+'  '+name.removeprefix(prefix)+'\n' for name, data, _ in entries)
    entries.append((prefix+'SHA256SUMS', sums.encode(), 0o644))
    target = out/('imOK-'+version+'-install-kit.zip'); write_zip(target, entries); return target


def prepare():
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip(), 'Commit reviewed source before preparing a public release'
    package=ROOT/'packages/luci-app-routepilot'
    sys.path.insert(0,str(package/'tools'))
    spec=importlib.util.spec_from_file_location('release_build',package/'tools/build.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    checks=json.loads((ROOT/'docs/PUBLICATION_CHECKS.json').read_text())
    assert checks['version']==builder.VERSION and checks['all_passed'],'Unreviewed package version'
    version=builder.VERSION;out=ROOT/'.local/release'/version;out.mkdir(parents=True,exist_ok=True)
    ipk=builder.build(out)
    assert digest(ipk)==checks['integrity']['package_sha256'],'IPK differs from the reviewed package'
    archive=out/('imOK-'+version+'-public-source.zip')
    records=subprocess.check_output(['git','ls-files','--stage','-z'],cwd=ROOT).decode().split('\0')
    modes={}
    for record in records:
        if not record:continue
        info,name=record.split('\t',1);mode,_,stage=info.split();assert stage=='0','Unresolved index'
        modes[name]=int(mode,8)
    entries=[]
    for name in public_paths(modes):
        f=ROOT/name;assert f.is_file() and not f.is_symlink(),name
        entries.append(('imOK-'+version+'/'+name,f.read_bytes(),modes[name]&0o777))
    write_zip(archive,entries)
    kit=install_kit(out,version,ipk,archive)
    sums=out/'SHA256SUMS';sums.write_text(''.join(digest(p)+'  '+p.name+'\n' for p in [ipk,archive,kit]))
    return version,out,[ipk,archive,kit,sums]

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

def release_options(version, checks):
    value = checks.get('release', {'name': 'imOK 2.0 · 客户端接入与稳定性版（v'+version+'）', 'prerelease': False})
    assert set(value) == {'name', 'prerelease'}, 'Unsupported publication metadata'
    assert type(value['prerelease']) is bool, 'Release kind must be a boolean'
    assert isinstance(value['name'], str) and value['name'].startswith('imOK ') and 1 <= len(value['name']) <= 120 and '\n' not in value['name'], 'Invalid release title'
    return dict(value)


def publish(version,files,head,body,*,name=None,prerelease=False):
    assert re.fullmatch('[0-9a-f]{40}',head),'Invalid source commit'
    assert type(prerelease) is bool, 'Release kind must be explicit'
    name=name or 'imOK 2.0 · 客户端接入与稳定性版（v'+version+'）'
    tag='v'+version;ref=api('GET','/git/ref/tags/'+tag)
    assert not ref or ref['object']['type']=='commit' and ref['object']['sha']==head,'Refuse to replace an existing tag'
    release=api('GET','/releases/tags/'+tag)
    if release and not release.get('draft'):
        assert ref and release.get('prerelease') is prerelease,'Existing public release differs'
        verify_assets(release,files)
        return release
    if release:
        assert release['target_commitish']==head and release.get('prerelease') is prerelease,'Unrelated draft release'
    else:
        release=api('POST','/releases',{'tag_name':tag,'target_commitish':head,'name':name,'body':body,'draft':True,'prerelease':prerelease,'make_latest':'false'})
    existing={a['name']:a for a in release.get('assets',[])}
    assert set(existing)<={p.name for p in files},'Unexpected draft attachments'
    for p in files:
        if p.name in existing:
            verify_assets({'assets':[existing[p.name]]},[p]);continue
        url=release['upload_url'].split('{')[0]+'?'+urllib.parse.urlencode({'name':p.name})
        uploaded=api('POST',url,data=p.read_bytes(),content_type='application/zip' if p.suffix=='.zip' else 'application/octet-stream')
        verify_assets({'assets':[uploaded]},[p])
    staged=api('GET','/releases/'+str(release['id']));verify_assets(staged,files)
    final=api('PATCH','/releases/'+str(release['id']),{'draft':False,'prerelease':prerelease,'make_latest':'false' if prerelease else 'true'})
    verify_assets(final,files)
    final_ref=api('GET','/git/ref/tags/'+tag)
    assert final_ref['object']['sha']==head and not final['draft'] and final['prerelease'] is prerelease,'Publication state mismatch'
    return final

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    version,out,files=prepare()
    options=release_options(version,json.loads((ROOT/'docs/PUBLICATION_CHECKS.json').read_text()))
    report={'version':version,'release':options,'assets':[{ 'name':p.name,'sha256':digest(p),'size':p.stat().st_size} for p in files]}
    if args.publish:
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        assert head==os.environ.get('GITHUB_SHA'),'Checkout differs from the triggering commit'
        release=publish(version,files,head,(ROOT/'docs/releases'/('v'+version+'.md')).read_text(),**options)
        report.update(url=release['html_url'],published=True,source_commit=head)
    (out/'receipt.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()

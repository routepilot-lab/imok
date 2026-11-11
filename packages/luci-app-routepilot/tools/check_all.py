#!/usr/bin/env python3
"""Run all local checks; never contacts a router, cloud API, or production DB."""
import argparse,ast,concurrent.futures,hashlib,importlib.util,io,json,os,re,subprocess,sys,tarfile,tempfile,time
from pathlib import Path
PACKAGE=Path(__file__).resolve().parents[1]
PROJECT=PACKAGE.parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--luci-core',type=Path,default=os.environ.get('ROUTEPILOT_LUCI_CORE'))
    parser.add_argument('--report',type=Path,default=PROJECT/'.local/check-all/report.json')
    args=parser.parse_args()
    if not args.luci_core or not Path(args.luci_core).is_file():parser.error('--luci-core must reference the locally obtained target LuCI core; no fake fallback')
    out=args.report.parent;out.mkdir(parents=True,exist_ok=True)
    jobs=[('python-main',[sys.executable,'-m','unittest','discover','-s',str(PACKAGE/'tests'),'-v']),('python-server',[sys.executable,'-m','unittest','discover','-s',str(PACKAGE/'server'),'-v']),('luci-contract',['node',str(PACKAGE/'tests/check_luci_module_contract.cjs'),str(args.luci_core)]),('assets',[sys.executable,str(PACKAGE/'tools/asset_versions.py'),'--check'])]
    jobs += [(f.stem,['node',str(f)]) for f in sorted((PACKAGE/'tests').glob('test_*.cjs'))]
    def run(job):
        name,cmd=job
        try:r=subprocess.run(cmd,capture_output=True,text=True,timeout=180);code=r.returncode;log=r.stdout+r.stderr
        except Exception as error:code=1;log=type(error).__name__
        (out/(name+'.log')).write_text(log)
        count=re.search(r'Ran (\d+) tests',log)
        return dict(check=name,exit=code,tests=int(count[1]) if count else None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,jobs))
    extra={}
    try:
        count=0
        for folder in [PACKAGE/'root/usr/lib/routepilot',PACKAGE/'server',PACKAGE/'tools',PROJECT/'scripts']:
            for f in folder.glob('*.py'):ast.parse(f.read_text(),filename=str(f));count+=1
        for f in (PACKAGE/'root/usr/libexec').iterdir():
            if f.is_file():ast.parse(f.read_text(),filename=str(f));count+=1
        for f in list((PACKAGE/'tools').glob('*.sh'))+list((PROJECT/'scripts').glob('*.sh'))+[PACKAGE/'root/etc/init.d/routepilot']:
            subprocess.run(['sh','-n',str(f)],check=True,capture_output=True);count+=1
        for f in list((PACKAGE/'htdocs').rglob('*.js'))+[PACKAGE/'root/usr/share/routepilot/workers/worker.mjs']:
            subprocess.run(['node','--check',str(f)],check=True,capture_output=True);count+=1
        bundle=PACKAGE/'root/usr/share/routepilot/workers';meta=json.loads((bundle/'SOURCE.json').read_text())
        assert hashlib.sha256((bundle/'worker.mjs').read_bytes()).hexdigest()==meta['template_sha256']
        spec=importlib.util.spec_from_file_location('check_builder',PACKAGE/'tools/build.py');builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        assert json.loads((PACKAGE/'root/usr/share/routepilot/version.json').read_text())['package']==builder.VERSION
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            a=builder.build(folder/'a');b=builder.build(folder/'b');assert a.read_bytes()==b.read_bytes()
            with tarfile.open(a,'r:gz') as outer:raw=outer.extractfile('./data.tar.gz').read()
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tf:
                files={m.name:m for m in tf if m.isfile()}
                assert not any('__pycache__' in name or name.endswith('.pyc') for name in files)
                for f in (PACKAGE/'root/usr/libexec').iterdir():assert files['./usr/libexec/'+f.name].mode==0o755
                assert files['./etc/config/routepilot'].mode==0o600
                manifest={name:hashlib.sha256(tf.extractfile(item).read()).hexdigest() for name,item in files.items()}
            extra=dict(version=builder.VERSION,syntax_files=count,worker_hash=True,reproducible=True,package_sha256=hashlib.sha256(a.read_bytes()).hexdigest(),package_files=len(files),manifest=manifest)
    except Exception as error:results.append(dict(check='syntax-build-integrity',exit=1,error=type(error).__name__+': '+str(error)))
    report=dict(time=time.time(),checks=results,integrity=extra,all_passed=all(x['exit']==0 for x in results),no_router_or_cloud_operations=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k!='integrity'},ensure_ascii=False,indent=2));return 0 if report['all_passed'] else 1

if __name__=='__main__':raise SystemExit(main())

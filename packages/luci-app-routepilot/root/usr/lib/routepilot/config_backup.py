"""Versioned non-credential settings migration; imported entries remain staged."""
import json
from pathlib import Path
import re
import tempfile
from runtime import RUN, atomic, command
from public_errors import ControlError
import endpoint_control as endpoint

INPUT=RUN/'settings-import-input'
UCI=Path('/etc/config/routepilot')


def validate(document):
    if not isinstance(document,dict) or document.get('format') not in ('routepilot-masque-v2','routepilot-masque-v3'):
        raise ControlError('备份格式无效')
    allowed={'format','sections'}|({'endpoint'} if document['format']=='routepilot-masque-v3' else set())
    if set(document)-allowed:raise ControlError('备份包含未知字段')
    rows=document.get('sections')
    if not isinstance(rows,list) or not 1<=len(rows)<=64:raise ControlError('备份配置节无效')
    names=set();main=0;clean=[]
    for row in rows:
        if not isinstance(row,dict) or len(row)>128:raise ControlError('备份配置节无效')
        name=row.get('.name','');kind=row.get('.type','')
        if not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9_]{1,32}',name) or name in names or kind not in ('main','policy'):
            raise ControlError('备份配置节无效')
        names.add(name)
        if kind=='main':
            if name!='main':raise ControlError('总配置名称无效')
            main+=1
        item={'.name':name,'.type':kind}
        for key,value in row.items():
            if key.startswith('.'):continue
            if not re.fullmatch('[a-z_][a-z0-9_]{0,63}',key):raise ControlError('备份字段无效')
            values=value if isinstance(value,list) else [value]
            limit=2000 if key in ('custom_direct_domains','custom_proxy_domains','custom_direct_ips','custom_proxy_ips') else 256
            if len(values)>limit or not all(isinstance(x,str) and len(x)<=2048 and not any(ord(c)<32 or ord(c)==127 for c in x) for x in values):
                raise ControlError('备份字段值无效')
            item[key]=value
        item['enabled']='0'
        if kind=='main':item.update(preparing='0',backend='masque')
        clean.append(item)
    if main!=1:raise ControlError('备份缺少总配置')
    # Old backups reset entry preference to preserve, never inherit a target machine's pending request.
    preferences=document.get('endpoint',{}) if document['format']=='routepilot-masque-v3' else {}
    if not isinstance(preferences,dict) or set(preferences)-{'mode','address','hours'}:raise ControlError('入口备份字段无效')
    cfg=endpoint.validate(preferences);cfg.update(revision='',staged=True)
    return clean,cfg


def export():
    with endpoint.locked():
        cfg=endpoint.settings()
        values=json.loads(command(['ubus','call','uci','get','{"config":"routepilot"}']).stdout)['values']
        rows=[]
        for name,value in values.items():
            row=dict(value);row['.name']=name
            if row.get('.type') in ('main','policy'):rows.append(row)
        document=dict(format='routepilot-masque-v3',sections=rows,endpoint={k:cfg[k] for k in ('mode','address','hours')})
        validate(document)
        # Match the existing UTF-8 file limit, including the CLI's final newline.
        if len(json.dumps(document,ensure_ascii=False).encode())+1>262144:
            raise ControlError('配置备份超过当前 256 KiB 导入上限，未生成无法恢复的文件；现有设置未改动')
        return document


def quote(value):
    return "'"+value.replace("'","'\\''")+"'"


def render(rows):
    lines=[]
    for row in rows:
        lines.append('config '+row['.type']+' '+quote(row['.name']))
        for key,value in row.items():
            if key.startswith('.'):continue
            if isinstance(value,list):lines.extend(' list '+key+' '+quote(x) for x in value)
            else:lines.append(' option '+key+' '+quote(value))
    return '\n'.join(lines)+'\n'


def import_settings(request_id):
    from worker_backend import guard
    with guard(),endpoint.locked():
        from request_input import read_request
        rows,preferences=validate(read_request(INPUT,request_id,264192))
        endpoint.reject_new_requests()
        # No real config is touched until the target UCI parser accepts the whole document.
        with tempfile.TemporaryDirectory(prefix='routepilot-import-',dir=RUN) as folder:
            directory=Path(folder);(directory/'delta').mkdir()
            command(['uci','-c',folder,'-t',str(directory/'delta'),'import','routepilot'],data=render(rows))
            command(['uci','-c',folder,'-t',str(directory/'delta'),'commit','routepilot'])
            content=(directory/'routepilot').read_text()
        if UCI.is_symlink():raise ControlError('配置路径无效')
        before=UCI.read_text();old_endpoint=endpoint.CONFIG.read_bytes() if endpoint.CONFIG.exists() else None
        try:
            # Stage first: a crash cannot accidentally apply a migrated endpoint to a live line.
            atomic(endpoint.CONFIG,json.dumps(preferences))
            atomic(UCI,content)
            command(['uci','revert','routepilot'],check=False)
        except Exception:
            atomic(UCI,before)
            if old_endpoint is None:endpoint.CONFIG.unlink(missing_ok=True)
            else:atomic(endpoint.CONFIG,old_endpoint.decode())
            raise
        return dict(imported=True,request_id=request_id.split(':')[0],enabled=False,endpoint_staged=True,message='配置已导入并保持关闭；入口偏好待核对后单独保存应用，身份与历史事务未迁移')

#!/usr/bin/env python3
"""Name plugin modules by content so LuCI's core version cannot pin old code."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MENU = 'root/usr/share/luci/menu.d/luci-app-routepilot.json'
RESOURCES = 'htdocs/luci-static/resources'
CSS = re.compile(r"routepilot/((?:v0262|style_[0-9a-f]{12})\.css)")
HELPER = re.compile(r"'require routepilot\.(feedback(?:_[0-9a-f]{12})?) as feedback';")


def digest(data):
    return hashlib.sha256(data).hexdigest()[:12]


def paths(root):
    menu = json.loads((root / MENU).read_text(encoding='utf-8'))
    names = {entry['action']['path'] for entry in menu.values() if entry.get('action', {}).get('type') == 'view'}
    if len(names) != 1:
        raise ValueError('imok menu must reference one view module')
    name = names.pop()
    if not re.fullmatch(r'routepilot_masque_[A-Za-z0-9]+', name):
        raise ValueError('Unexpected view module name')
    view = root / RESOURCES / 'view' / (name + '.js')
    text = view.read_text(encoding='utf-8')
    match = HELPER.search(text)
    if match is None:
        raise ValueError('Missing feedback module import')
    helper = root / RESOURCES / 'routepilot' / (match[1] + '.js')
    return menu, view, text, helper, match[1]


def stylesheet(root, text):
    matches = CSS.findall(text)
    if len(matches) != 1:
        raise ValueError('Expected one imok stylesheet')
    return root / RESOURCES / 'routepilot' / matches[0]


def client_module(root, text):
    names = re.findall(r"L.require\('routepilot\.(clients_[a-f0-9]{12})'\)", text)
    if not names:
        return None
    if len(names) != 1:
        raise ValueError('Expected one lazy client module')
    return root / RESOURCES / 'routepilot' / (names[0] + '.js')


def sync(root=ROOT):
    menu, view, text, helper, old_helper = paths(root)
    css = stylesheet(root, text)
    css_name = 'style_' + digest(css.read_bytes()) + '.css'
    text = text.replace('routepilot/' + css.name, 'routepilot/' + css_name)
    css.rename(css.with_name(css_name))
    helper_name = 'feedback_' + digest(helper.read_bytes())
    text = text.replace("'require routepilot." + old_helper + " as feedback';",
                        "'require routepilot." + helper_name + " as feedback';")
    client = client_module(root, text)
    if client:
        new_client = client.with_name('clients_' + digest(client.read_bytes()) + '.js')
        text = text.replace(client.stem, new_client.stem)
        client.rename(new_client)
    view_name = 'routepilot_masque_' + digest(text.encode('utf-8'))
    new_helper = helper.with_name(helper_name + '.js')
    new_view = view.with_name(view_name + '.js')
    helper.rename(new_helper)
    view.write_bytes(text.encode('utf-8'))
    view.rename(new_view)
    for entry in menu.values():
        if entry.get('action', {}).get('type') == 'view':
            entry['action']['path'] = view_name
    (root / MENU).write_bytes((json.dumps(menu, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return validate(root)


def validate(root=ROOT):
    _, view, text, helper, _ = paths(root)
    css = stylesheet(root, text)
    if css.name != 'style_' + digest(css.read_bytes()) + '.css':
        raise ValueError('Stylesheet changed: run tools/asset_versions.py before packaging')
    if set(css.parent.glob('*.css')) != {css}:
        raise ValueError('Obsolete imok stylesheets remain')
    if helper.stem != 'feedback_' + digest(helper.read_bytes()):
        raise ValueError('Feedback changed: run tools/asset_versions.py before packaging')
    if view.stem != 'routepilot_masque_' + digest(view.read_bytes()):
        raise ValueError('View changed: run tools/asset_versions.py before packaging')
    if set(view.parent.glob('routepilot_masque_*.js')) != {view}:
        raise ValueError('Obsolete imok view modules remain')
    if set(helper.parent.glob('feedback*.js')) != {helper}:
        raise ValueError('Obsolete feedback modules remain')
    result = {'view': view.stem, 'feedback': helper.stem, 'css': css.name}
    client = client_module(root, text)
    if client:
        if client.stem != 'clients_' + digest(client.read_bytes()):
            raise ValueError('Client module changed: run tools/asset_versions.py')
        if set(client.parent.glob('clients_*.js')) != {client}:
            raise ValueError('Obsolete client modules remain')
        result['clients'] = client.stem
    return result


if __name__ == '__main__':
    print(json.dumps(validate() if '--check' in sys.argv else sync()))

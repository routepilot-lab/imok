'use strict';
'require view';
'require form';
'require uci';
'require fs';
'require ui';
'require poll';
'require rpc';
'require dom';
'require routepilot.feedback_3a581c5b4c6b as feedback';
'require routepilot.page_workspace_8777f401a3ce as pageWorkspace';
'require routepilot.page_account_164b6748a5fc as pageAccount';
'require routepilot.page_endpoint_691c764fb4bc as pageEndpoint';
'require routepilot.page_routing_a051a736c281 as pageRouting';
'require routepilot.page_dns_c5839ecc6c42 as pageDns';
'require routepilot.page_workers_a6382e53f54e as pageWorkers';
'require routepilot.page_devices_b9598bb0ca64 as pageDevices';
'require routepilot.page_overview_12a5df5ec5fc as pageOverview';
'require routepilot.page_backup_0d3a260fdf9c as pageBackup';
'require routepilot.page_rules_288722c683fe as pageRules';
'require routepilot.page_shared_04a787115574 as pageShared';

var callHostHints = rpc.declare({ object: 'luci-rpc', method: 'getHostHints', expect: { '': {} } });
var commitConfig = rpc.declare({
  object: 'uci',
  method: 'commit',
  params: ['config'],
  reject: true
});

function refreshMenuIndex() {
  return ui.menu
    .load()
    .then(function (tree) {
      var node = tree;
      ['admin', 'services', 'routepilot'].forEach(function (k) {
        node = node && node.children && node.children[k];
      });
      if (
        node &&
        node.children &&
        (!node.children.cloudflare ||
          !node.children.runtime ||
          ['basic', 'devices', 'dns', 'advanced', 'workers'].some(function (k) {
            return node.children[k] && node.children[k].title;
          }))
      )
        ui.menu.flushCache();
    })
    .catch(function () {});
}
function readExitDirectory() {
  return fs
    .exec('/usr/libexec/routepilot-exits', [])
    .then(function (r) {
      if (r.code) throw Error('directory');
      var v = JSON.parse(r.stdout);
      if (!v.available || !Array.isArray(v.networks)) throw Error('directory');
      v.read_at = Date.now();
      v.last_success_at = v.read_at;
      return v;
    })
    .catch(function () {
      return { available: false, networks: [], read_at: Date.now() };
    });
}
function readRuntimeStatus() {
  return Promise.resolve()
    .then(function () {
      return fs.exec('/usr/libexec/routepilot-runtime-status', []);
    })
    .then(function (r) {
      var d = r.code ? null : JSON.parse(r.stdout);
      return d && d.schema === 1 && typeof d.installed === 'boolean' ? d : null;
    })
    .catch(function () {
      return null;
    });
}
function readHostHints() {
  return new Promise(function (resolve) {
    var timer = setTimeout(function () {
      resolve(null);
    }, 2000);
    Promise.resolve()
      .then(function () {
        return callHostHints();
      })
      .then(
        function (rows) {
          clearTimeout(timer);
          resolve(rows && typeof rows === 'object' && !Array.isArray(rows) ? rows : null);
        },
        function () {
          clearTimeout(timer);
          resolve(null);
        }
      );
  });
}
function readOverviewSnapshot() {
  var files = [];
  return Promise.all(
    [
      ['status.json', 0],
      ['connectivity.json', 2],
      ['exit-observation.json', 13],
      ['traffic.json', 14],
      ['connection-events.json', 15]
    ].map(function (item) {
      return fs
        .read('/var/run/routepilot/' + item[0])
        .then(JSON.parse)
        .catch(function () {
          return null;
        })
        .then(function (value) {
          files[item[1]] = value;
        });
    })
  ).then(function () {
    return files;
  });
}
function normalizedID(value) {
  return String(value || '').toLowerCase();
}
function configuredExit() {
  var value = uci.get('routepilot', 'main', 'exit');
  return value == null ? 'seoul' : value;
}
function savedExitID(key) {
  return ['default', 'seoul', 'usa'].indexOf(key) >= 0
    ? normalizedID(uci.get('routepilot', 'main', 'vnet_' + key))
    : normalizedID(key);
}
function directoryLabel(key, catalog) {
  if (key == null) key = configuredExit();
  var id = savedExitID(key),
    item = (catalog.networks || []).filter(function (n) {
      return n.id === id;
    })[0];
  if (item) return item.name + (item.is_default ? ' · 账户默认网络' : '');
  if (['default', 'seoul', 'usa'].indexOf(key) >= 0)
    return { default: '旧默认', seoul: '旧首尔', usa: '旧美国' }[key] + '（兼容配置）';
  if (/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id))
    return _('账户出口') + ' · ' + id.slice(0, 8);
  return String(key || '未选择');
}
var sectionGroups = {
  account: 'advanced',
  endpoint: 'endpoint',
  routing: 'basic',
  devices: 'devices',
  dns: 'dns',
  workers: 'workers'
};
var legacyPages = {
  basic: 'routing',
  devices: 'devices',
  dns: 'dns',
  diagnostics: 'dns',
  advanced: 'account',
  workers: 'workers'
};
var groupSections = {
  advanced: 'account',
  endpoint: 'endpoint',
  basic: 'routing',
  devices: 'devices',
  dns: 'dns',
  workers: 'workers'
};
var ruleSections = {
    'rules-overview': 'overview',
    'rules-sources': 'sources',
    'rules-manage': 'manage',
    'rules-custom': 'custom'
  },
  ruleSectionNames = {
    overview: '更新概况',
    sources: '来源与频率',
    manage: '规则管理',
    custom: '自定义分流'
  };
var imokStylesheet = 'routepilot/style_f98625e62fa2.css';
var refreshImokTheme;

function syncImokTheme() {
  if (!document.documentElement || !document.body || typeof window.getComputedStyle !== 'function')
    return;
  if (!refreshImokTheme) {
    var root = document.documentElement;
    var preference = window.matchMedia?.('(prefers-color-scheme: dark)');
    refreshImokTheme = function () {
      var mode = root.getAttribute('data-argon-mode');
      if (mode !== 'dark' && mode !== 'light') {
        // Older Argon inlines the effective stylesheet, including forced modes.
        var background = window.getComputedStyle(document.body).backgroundColor;
        var rgba = /^rgba?\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\s*\)$/.exec(
          background
        );
        var dark =
          mode !== 'normal' && rgba && (rgba[4] == null || Number(rgba[4]) === 1)
            ? 0.2126 * rgba[1] + 0.7152 * rgba[2] + 0.0722 * rgba[3] < 128
            : preference?.matches;
        mode = dark ? 'dark' : 'light';
      }
      root.setAttribute('data-imok-theme', mode);
    };
    if (preference?.addEventListener) preference.addEventListener('change', refreshImokTheme);
    else preference?.addListener?.(refreshImokTheme);
    if (typeof window.MutationObserver === 'function')
      new window.MutationObserver(refreshImokTheme).observe(root, {
        attributes: true,
        attributeFilter: ['data-argon-mode']
      });
  }
  refreshImokTheme();
}

function routeLeaf() {
  var parts = window.location.pathname.split('/').filter(Boolean),
    leaf = parts.pop();
  if (parts[parts.length - 1] === 'rules' && Object.keys(ruleSectionNames).indexOf(leaf) >= 0)
    return 'rules-' + leaf;
  if (leaf === 'rules') return 'rules-overview';
  if (leaf === 'routepilot') return 'overview';
  if (leaf === 'cloudflare')
    return groupSections[(window.location.hash || '').replace('#rp-center-', '')] || 'account';
  return parts[parts.length - 1] === 'cloudflare' && sectionGroups[leaf]
    ? leaf
    : legacyPages[leaf] || leaf;
}
return view.extend({
  exitOptions: function (current, mapping, catalog) {
    var id =
        ['default', 'seoul', 'usa'].indexOf(current) >= 0
          ? normalizedID(mapping[current])
          : normalizedID(current),
      found = false;
    var networks = catalog.available ? catalog.networks : [],
      names = {};
    networks.forEach(function (n) {
      names[n.name] = (names[n.name] || 0) + 1;
    });
    var options = networks.map(function (n) {
      var same = n.id === id;
      found = found || same;
      return {
        value: same && current ? current : n.id,
        label:
          n.name +
          (n.is_default ? ' · 账户默认网络' : '') +
          (names[n.name] > 1 ? ' · ' + n.id.slice(0, 8) : '')
      };
    });
    if (!found)
      options.unshift({
        value: current || '',
        label: current
          ? '保留当前选择：' +
            String(current) +
            '（' +
            (catalog.available
              ? '未在本次目录中'
              : !catalog.read_at
                ? '目录尚未读取，保留原值'
                : '目录读取失败，保留原值') +
            '）'
          : '请选择账户出口（不会自动选择）'
      });
    return options;
  },
  load: function () {
    var parts = window.location.pathname.split('/').filter(Boolean),
      raw = parts[parts.length - 1],
      leaf = routeLeaf();
    if (
      ((legacyPages[raw] && parts[parts.length - 2] !== 'cloudflare') ||
        (raw === 'cloudflare' && window.location.hash)) &&
      typeof window.location.replace === 'function'
    )
      window.location.replace(L.url('admin/services/routepilot/cloudflare/' + leaf));
    if (leaf === 'runtime')
      return L.require('routepilot.runtime_597c3c4ba717').then(function (runtimeView) {
        return runtimeView.load().then(function (runtimeData) {
          return { runtimeView: runtimeView, runtimeData: runtimeData };
        });
      });
    if (leaf === 'clients')
      return L.require('routepilot.clients_b27530227248').then(function (clientView) {
        return clientView.load().then(function (clientData) {
          return { clientsView: clientView, clientsData: clientData };
        });
      });
    var devices = leaf === 'devices',
      interfaces = devices || leaf === 'routing';
    var initialOverviewRead = leaf === 'overview' ? readOverviewSnapshot() : null;
    var runtimeRead = ['overview', 'account', 'routing'].includes(leaf)
      ? readRuntimeStatus()
      : null;
    return Promise.all([
      uci.load('routepilot'),
      devices ? readHostHints() : Promise.resolve(null),
      interfaces
        ? fs
            .exec('/usr/libexec/routepilot', ['interfaces'])
            .then(function (r) {
              return r.code ? [] : JSON.parse(r.stdout);
            })
            .catch(function () {
              return [];
            })
        : Promise.resolve([]),
      Promise.resolve({
        available: false,
        networks: [],
        skipped: !['overview', 'account', 'routing'].includes(leaf)
      }),
      refreshMenuIndex(),
      leaf === 'workers'
        ? fs
            .exec('/usr/libexec/routepilot-backup-list', [])
            .then(function (r) {
              return r.code ? { nodes: [] } : JSON.parse(r.stdout);
            })
            .catch(function () {
              return { nodes: [] };
            })
        : Promise.resolve({ nodes: [] }),
      Promise.resolve(null),
      fs
        .read('/usr/share/routepilot/version.json')
        .then(JSON.parse)
        .catch(function () {
          return null;
        })
    ]).then(function (data) {
      data.runtimeRead = runtimeRead;
      data.refreshRuntime = readRuntimeStatus;
      data.initialOverviewRead = initialOverviewRead;
      return data;
    });
  },
  render: function (data) {
    syncImokTheme();
    return pageWorkspace.render.call(this, data, {
      window,
      document,
      L,
      imokStylesheet,
      routeLeaf,
      ruleSections,
      sectionGroups,
      form,
      ui,
      fs,
      uci,
      configuredExit,
      savedExitID,
      feedback,
      readExitDirectory,
      directoryLabel,
      ruleSectionNames,
      commitConfig,
      poll,
      pageShared,
      pageBackup,
      pageAccount,
      pageEndpoint,
      pageRouting,
      pageDevices,
      pageDns,
      pageWorkers,
      pageRules,
      pageOverview
    });
  },
  handleSaveApply: function () {
    return this.applySettings();
  },
  handleReset: null
});

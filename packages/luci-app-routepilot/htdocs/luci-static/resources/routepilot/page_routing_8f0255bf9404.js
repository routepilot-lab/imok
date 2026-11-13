'use strict';
'require baseclass';

return baseclass.extend({
  render: function (centerGroup, panel, selectFields) {
    return centerGroup(
      'basic',
      _('账户出口、国家 / 地区与接管范围'),
      _('选择代理出口和设备接管范围。'),
      [
        E('div', { class: 'imok-route-layout' }, [
          panel(
            _('代理出口'),
            _('选择账户出口后保存并应用。'),
            selectFields(['exit', '_discover', '_exit_directory'])
          ),
          panel(
            _('接管范围与分流'),
            _('按当前范围显示适用设置；未启用的设备与网络不接管。'),
            selectFields([
              'scope',
              'lan_interfaces',
              'lan_mode',
              'bypass_clients',
              'ipv6_mode'
            ]).concat(
              E('details', { class: 'rp-help imok-routing-help' }, [
                E('summary', {}, _('说明')),
                ...selectFields(['_device_mode', '_info'])
              ])
            )
          )
        ])
      ]
    );
  },
  comparisonActions: function (networks, startComparison, stopComparison) {
    return {
      allExits: E(
        'button',
        {
          class: 'btn',
          click: function () {
            startComparison(
              'exit',
              networks()
                .filter(function (n) {
                  return !n.is_default;
                })
                .map(function (n) {
                  return n.id;
                })
            );
          }
        },
        _('全部出口测速')
      ),
      cancelExits: E('button', { class: 'btn', click: stopComparison }, _('取消测速'))
    };
  },
  directoryRows: function ({
    catalog,
    comparisonResult,
    exitHealth,
    comparisonCell,
    comparisonReady,
    startComparison
  }) {
    var measured = new Map(Object.entries((comparisonResult && comparisonResult.history) || {}));
    ((comparisonResult && comparisonResult.rows) || []).forEach(function (row) {
      measured.set(row.target, row);
    });
    var rows = (catalog.available ? catalog.networks : []).map(function (n) {
      return E('tr', {}, [
        E('td', {}, n.name + (n.is_default ? ' · 账户默认网络' : '')),
        E('td', {}, exitHealth(n.id)),
        E('td', {}, [
          comparisonCell(measured.get(n.id)),
          n.is_default
            ? ''
            : E(
                'button',
                {
                  class: 'btn',
                  disabled: !comparisonReady(),
                  click: function () {
                    startComparison('exit', [n.id]);
                  }
                },
                _('测一下')
              )
        ])
      ]);
    });
    if (!rows.length)
      rows.push(
        E(
          'tr',
          {},
          E(
            'td',
            { colspan: 3 },
            catalog.available
              ? _('账户目录中没有可选虚拟网络。')
              : _('本次未取得出口目录；不代表账户没有其他出口。')
          )
        )
      );
    return rows;
  },
  configure: function ({
    pageOption,
    form,
    uci,
    data,
    catalog,
    configuredExit,
    exitOptions,
    refreshDirectory,
    directoryStatus,
    allExits,
    cancelExits,
    comparisonStatus,
    directoryRows
  }) {
    var o = pageOption('basic', form.ListValue, 'scope', _('接管范围'));
    o.value('devices', _('按设备选择'));
    o.value('lan', _('所选局域网（无需绑定设备）'));
    o.default = 'devices';
    o = pageOption(
      'basic',
      form.MultiValue,
      'lan_interfaces',
      _('接管的 LAN 接口（可多选）'),
      _(
        '勾选设备所在的主网络、访客网络或 VLAN，可同时选择。Wi-Fi 与网线共用网桥时只勾选该网桥。每个接口需要一个独立 IPv4 私网网段；不会创建网络，也不会开放主网络与访客网络之间的互访。标记未启用的网络需先在路由器网络设置中开启，再启用本插件。'
      )
    );
    o.rmempty = false;
    var interfaces = data[2] || [],
      currentLAN = uci.get('routepilot', 'main', 'lan_interfaces') || [
        uci.get('routepilot', 'main', 'lan_interface') || 'br-lan'
      ];
    if (typeof currentLAN === 'string') currentLAN = currentLAN.split(/\s+/);
    o.cfgvalue = function () {
      return uci.get('routepilot', 'main', 'lan_interfaces') || currentLAN;
    };
    interfaces.forEach(function (i) {
      o.value(i.name, i.name + ' · ' + i.network + (i.up ? '' : ' · 尚未启用，请先在路由器启用'));
    });
    currentLAN.forEach(function (name) {
      if (
        !interfaces.some(function (i) {
          return i.name === name;
        })
      )
        o.value(name, name + ' · 未检测到有效网段，请核对');
    });
    o.validate = function (id, v) {
      return v && v.length && v.length <= 8 ? true : _('请选择 1–8 个 LAN 接口');
    };
    o = pageOption('basic', form.ListValue, 'lan_mode', _('局域网分流模式'));
    o.depends('scope', 'lan');
    [
      ['bypass_cn', _('国内 IP 直连，其余代理')],
      ['global', _('全部代理')],
      ['gfw', _('仅代理名单内域名')],
      ['whitelist', _('仅直连名单内域名')]
    ].forEach(function (v) {
      o.value(v[0], v[1]);
    });
    o.default = 'bypass_cn';
    o = pageOption(
      'basic',
      form.Flag,
      'bypass_clients',
      _('Cloudflare 客户端连接直连'),
      _('避免电脑尚未关闭 Cloudflare One 时叠加隧道；关闭电脑客户端后，普通网页仍按本插件分流。')
    );
    o.depends('scope', 'lan');
    o.default = '1';
    o.rmempty = false;
    o = pageOption(
      'basic',
      form.ListValue,
      'ipv6_mode',
      _('IPv6 处理方式（所选网络 / 设备默认）'),
      _(
        '阻断：禁止受管设备 IPv6 转发并过滤本插件 DNS 的 AAAA。允许直连：IPv6 不经过代理隧道，也不遵循本插件代理或域名规则，可能使用宽带真实出口；不创建 IPv6 网络，不覆盖其他防火墙。LAN 模式统一作用于所有勾选接口，忽略设备表覆盖；按设备模式可单独选择。未接管设备不受此选项影响。'
      )
    );
    o.value('block', _('阻断 IPv6（默认）'));
    o.value('direct', _('允许 IPv6 直连（绕过代理规则）'));
    o.default = 'block';
    o.rmempty = false;
    var exitOption = (o = pageOption(
      'basic',
      form.ListValue,
      'exit',
      _('主线路代理出口'),
      _(
        '这是 WARP 主线路的出口；启用下方备用线路时暂不使用此出口。按设备分流不会改变主线路出口。列表来自当前账户，已列出不代表可用，读取不会切换。'
      )
    ));
    exitOptions(
      configuredExit(),
      {
        default: uci.get('routepilot', 'main', 'vnet_default'),
        seoul: uci.get('routepilot', 'main', 'vnet_seoul'),
        usa: uci.get('routepilot', 'main', 'vnet_usa')
      },
      catalog
    ).forEach(function (item) {
      o.value(item.value, item.label);
    });
    o.cfgvalue = function () {
      return configuredExit();
    };
    o.rmempty = false;
    o.validate = function (id, value) {
      return value ? true : _('请明确选择一个账户出口');
    };
    o = pageOption('basic', form.Button, '_discover', _('账户出口'));
    o.inputtitle = _('刷新账户出口（只读）');
    o.onclick = function () {
      return refreshDirectory();
    };
    o = pageOption('basic', form.DummyValue, '_exit_directory', _('目录读取与检测状态'));
    o.renderWidget = function () {
      return E('div', {}, [
        directoryStatus,
        E('section', {}, [
          E('h4', {}, _('账户出口列表')),
          E('div', { class: 'rp-actions' }, [allExits, cancelExits]),
          comparisonStatus,
          E('table', { class: 'table imok-exit-table' }, [
            E(
              'thead',
              {},
              E('tr', {}, [
                E('th', {}, _('账户可选网络')),
                E('th', {}, _('网站检测')),
                E('th', {}, _('测速结果'))
              ])
            ),
            directoryRows
          ])
        ])
      ]);
    };
    o = pageOption('basic', form.DummyValue, '_device_mode', _('按设备的分流模式'));
    o.depends('scope', 'devices');
    o.cfgvalue = function () {
      return (
        _('到“设备”页面，') +
        _(
          '为每台设备分别选择分流模式：国内 IP 直连、全部代理、代理名单、直连名单或保持原样。不接管未启用的设备；局域网模式不覆盖设备表。'
        )
      );
    };
    o = pageOption('basic', form.DummyValue, '_info', _('使用说明'));
    o.rawhtml = false;
    o.cfgvalue = function () {
      return _(
        '国内直连流量使用本地宽带 IP；只有代理流量使用所选出口。关闭后撤回本插件的接管规则；其他软件按其自身配置运行。'
      );
    };
    return exitOption;
  }
});

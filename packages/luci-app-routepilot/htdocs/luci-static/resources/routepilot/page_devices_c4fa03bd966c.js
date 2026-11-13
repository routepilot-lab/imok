'use strict';
'require baseclass';

return baseclass.extend({
  render: function (panel, centerDeviceNode, deviceUsagePanel) {
    var scopeHint = E('p', { class: 'rp-help' });
    var deviceLeaseHint = E('p', { class: 'rp-help', role: 'status', hidden: '' });
    var devicePanel = panel(
      _('设备策略'),
      null,
      [
        deviceLeaseHint,
        E('div', { class: 'imok-device-grid' }, [centerDeviceNode]),
        E('details', { class: 'imok-device-note' }, [
          E('summary', {}, _('分流详情')),
          E(
            'p',
            {},
            _(
              '域名分流依赖普通 DNS，共用 CDN IP 的网站可能互相影响。使用加密 DNS 的设备可优先选「国内 IP 直连，其余代理」。'
            )
          )
        ])
      ],
      'imok-device-panel'
    );
    devicePanel.setAttribute('id', 'rp-center-devices');
    devicePanel.setAttribute('aria-label', _('设备分流'));
    var heading = devicePanel.querySelector('.imok-panel-heading');
    heading.appendChild(scopeHint);
    var addDevice = centerDeviceNode.querySelector('.cbi-section-create');
    if (addDevice) heading.appendChild(addDevice);
    var actionHeader = centerDeviceNode.querySelector(
      '.cbi-section-table-titles .cbi-section-actions'
    );
    if (actionHeader) actionHeader.textContent = _('操作');
    return {
      panel: devicePanel,
      usage: deviceUsagePanel(),
      scopeHint: scopeHint,
      leaseHint: deviceLeaseHint
    };
  },
  renderUsage: function ({ bytes, fs, helpDetails, isCurrentView, panel }) {
    var status = E('p', { class: 'rp-help', role: 'status', 'aria-live': 'polite' }),
      results = E('div', {});
    var refreshButton = E(
      'button',
      { type: 'button', class: 'btn', click: refreshUsage },
      _('刷新流量')
    );
    var card = panel(_('设备流量'), _('查看 GL.iNet 记录的累计用量。'), [
      status,
      results,
      helpDetails(_('计数范围和起点取决于固件。请在固件的流量页面核对统计范围。'))
    ]);
    card.querySelector('.imok-panel-heading').appendChild(refreshButton);
    function refreshUsage() {
      refreshButton.disabled = true;
      status.textContent = _('正在读取设备流量…');
      results.replaceChildren();
      return fs
        .exec('/usr/libexec/routepilot-device-usage', [])
        .then(function (r) {
          if (!isCurrentView()) return;
          if (r.code) throw Error();
          var data = JSON.parse(r.stdout);
          if (data.state === 'unsupported') {
            status.textContent = _('当前固件未提供按设备流量计数。');
            return;
          }
          if (
            data.state !== 'ready' ||
            !Array.isArray(data.devices) ||
            data.devices.length > 1024 ||
            !data.devices.every(function (row) {
              return (
                row &&
                typeof row.name === 'string' &&
                typeof row.mac === 'string' &&
                ['upload', 'download', 'total'].every(function (key) {
                  return row[key] === null || (Number.isSafeInteger(row[key]) && row[key] >= 0);
                })
              );
            })
          )
            throw Error();
          if (!data.devices.length) {
            status.textContent = _('当前没有可显示的设备。');
            return;
          }
          status.textContent = '';
          var columns = [_('设备'), _('上传'), _('下载'), _('合计')];
          var head = E(
            'thead',
            {},
            E(
              'tr',
              {},
              columns.map(function (label) {
                return E('th', { scope: 'col' }, label);
              })
            )
          );
          var rows = data.devices.map(function (row) {
            var label = row.name
              ? [E('strong', {}, row.name), E('small', { class: 'rp-help' }, row.mac)]
              : row.mac;
            return E('tr', {}, [
              E('th', { scope: 'row' }, label),
              E('td', {}, bytes(row.upload)),
              E('td', {}, bytes(row.download)),
              E('td', {}, bytes(row.total))
            ]);
          });
          results.replaceChildren(
            E(
              'div',
              {
                class: 'imok-device-usage-scroll',
                tabindex: '0',
                role: 'region',
                'aria-label': _('设备累计流量')
              },
              [
                E('table', { class: 'imok-device-usage-table', 'aria-label': _('设备累计流量') }, [
                  head,
                  E('tbody', {}, rows)
                ])
              ]
            )
          );
        })
        .catch(function () {
          if (isCurrentView()) status.textContent = _('设备流量读取失败，请刷新重试。');
        })
        .finally(function () {
          refreshButton.disabled = false;
        });
    }
    refreshUsage();
    return card;
  },
  configure: function ({
    m,
    form,
    uci,
    hints,
    ui,
    checkMapBeforeSave,
    checkGuardBeforeSave,
    guardDraft
  }) {
    var s = m.section(form.GridSection, 'policy', '', ''),
      o;
    s.addremove = true;
    s.anonymous = true;
    s.nodescriptions = true;
    s.addbtntitle = _('添加设备');
    s.modaltitle = function (id) {
      return this.map.addedSection === id ? _('添加设备策略') : _('编辑设备策略');
    };
    s.renderSectionPlaceholder = function () {
      return E(
        'span',
        { class: 'imok-device-empty' },
        _('还没有设备策略。点击「添加设备」开始设置。')
      );
    };
    var addDeviceModalOptions = s.addModalOptions;
    s.addModalOptions = function (section, id) {
      var result = addDeviceModalOptions.apply(this, arguments),
        notice = null;
      checkMapBeforeSave(
        section.map,
        function () {
          if (notice) {
            notice.remove();
            notice = null;
          }
          return checkGuardBeforeSave(
            null,
            id,
            guardDraft(section.children, id, ['enabled', 'mode', 'mac', 'egress_guard'])
          );
        },
        function (message) {
          notice = E('p', { class: 'imok-notice', 'data-tone': 'error', role: 'alert' }, message);
          section.map.root.prepend(notice);
        }
      );
      return result;
    };
    var renderDeviceModal = s.renderMoreOptionsModal;
    if (typeof renderDeviceModal === 'function') {
      s.renderMoreOptionsModal = function () {
        var section = this;
        return renderDeviceModal.apply(section, arguments).then(function (result) {
          var map = section.getActiveModalMap();
          if (map) map.parentElement.classList.add('imok-dialog', 'imok-device-dialog');
          return result;
        });
      };
    }
    o = s.option(form.Flag, 'enabled', _('启用'));
    o.default = '0';
    o.rmempty = false;
    o.textvalue = function (id) {
      var enabled = this.cfgvalue(id) === '1';
      return E(
        'span',
        { class: 'imok-device-state', 'data-enabled': enabled ? 'true' : 'false' },
        enabled ? _('已启用') : _('未启用')
      );
    };
    o = s.option(form.Value, 'name', _('设备名称'));
    var devices = {};
    o = s.option(
      form.ListValue,
      '_device',
      _('在线设备'),
      _(
        '选择设备后自动填入 MAC 地址和当前 IPv4。列表中找不到设备时，可按路由器的 DHCP 租约手动填写。'
      )
    );
    o.modalonly = true;
    o.optional = true;
    o.value('', _('请选择设备，或手动填写'));
    if (hints && typeof hints === 'object' && !Array.isArray(hints))
      Object.keys(hints)
        .sort()
        .forEach(function (mac) {
          var row = hints[mac];
          if (!/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac) || !row || typeof row !== 'object')
            return;
          var values = row.ipaddrs || row.ipv4,
            ips = Array.isArray(values) ? values : typeof values === 'string' ? [values] : [],
            ip = ips.filter(function (value) {
              return (
                typeof value === 'string' &&
                /^\d{1,3}(?:\.\d{1,3}){3}$/.test(value) &&
                value.split('.').every(function (part) {
                  return Number(part) <= 255;
                })
              );
            })[0];
          if (ip) {
            devices[mac] = ip;
            o.value(mac, (typeof row.name === 'string' && row.name ? row.name : mac) + ' · ' + ip);
          }
        });
    o.parse = function () {
      return Promise.resolve();
    };
    o.cfgvalue = function () {
      return '';
    };
    o.onchange = function (ev, id, value) {
      var address = devices[value];
      if (!address) return;
      // GridSection clones its options into a separate map for the edit dialog.
      var mac = this.section.getUIElement(id, 'mac'),
        ip = this.section.getUIElement(id, 'ip');
      if (!mac || !ip) return;
      mac.setValue(value);
      ip.setValue(address);
      mac.triggerValidation();
      ip.triggerValidation();
    };
    o = s.option(form.Value, 'mac', _('MAC 地址'));
    o.datatype = 'macaddr';
    o.rmempty = false;
    o = s.option(
      form.Value,
      'ip',
      _('固定 IPv4'),
      _(
        '填写设备在此局域网的 IPv4。可在路由器的 DHCP 设置中为该 MAC 保留地址，避免设备换 IP 后策略失效。'
      )
    );
    o.datatype = 'ip4addr';
    o.rmempty = false;
    function deviceChoiceText(id) {
      var value = this.cfgvalue(id) || this.default,
        index = this.keylist.indexOf(value);
      return index >= 0 ? this.vallist[index] : _('未知选项');
    }
    o = s.option(form.ListValue, 'mode', _('分流模式'));
    o.textvalue = deviceChoiceText;
    [
      ['bypass_cn', _('国内 IP 直连，其余代理')],
      ['global', _('全部代理')],
      ['gfw', _('仅代理名单内域名')],
      ['whitelist', _('仅直连名单内域名')],
      ['direct', _('保持原样，不接管')]
    ].forEach(function (v) {
      o.value(v[0], v[1]);
    });
    o.default = 'bypass_cn';
    o = s.option(
      form.Flag,
      'egress_guard',
      _('断网保护'),
      _(
        '仅在「按设备选择」模式下生效。此设备等待出口、转发和 DNS 就绪后按所选策略联网。检测到线路中断时重新阻断外网和普通 DNS，IPv6 外网始终阻断。启用前请关闭软、硬件流量卸载。'
      )
    );
    o.default = '0';
    o.rmempty = false;
    o.modalonly = true;
    o.retain = true;
    ['global', 'bypass_cn', 'gfw', 'whitelist'].forEach(function (mode) {
      o.depends('mode', mode);
    });
    o = s.option(
      form.ListValue,
      'ipv6_mode',
      _('IPv6 处理方式'),
      _(
        '仅按设备模式生效。允许直连会绕过代理隧道 和全部代理规则，可能使用宽带真实出口；不创建 IPv6。分流模式为“保持原样”时 IPv6 本就不受管理，此项不生效。'
      )
    );
    o.value('inherit', _('跟随默认'));
    o.value('block', _('阻断 IPv6'));
    o.value('direct', _('允许 IPv6 直连'));
    o.default = 'inherit';
    o.rmempty = false;
    o.textvalue = deviceChoiceText;
    ['global', 'bypass_cn', 'gfw', 'whitelist'].forEach(function (mode) {
      o.depends('mode', mode);
    });
    o.retain = true;
    return s;
  }
});

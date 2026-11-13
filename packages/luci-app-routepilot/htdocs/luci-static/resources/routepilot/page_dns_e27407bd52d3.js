'use strict';
'require baseclass';

return baseclass.extend({
  render: function (
    centerGroup,
    panel,
    fields,
    dnsCacheCard,
    dnsTestCard,
    bootstrapCard,
    lanDnsCard
  ) {
    return centerGroup('dns', _('DNS'), null, [
      E('div', { class: 'imok-settings-pair imok-dns-settings' }, [
        panel(_('DNS 配置'), null, [fields('dns'), lanDnsCard]),
        E('div', { class: 'imok-panel imok-dns-operations' }, [dnsCacheCard, dnsTestCard])
      ]),
      bootstrapCard
    ]);
  },
  create: function ({
    bootstrapCustom,
    bootstrapMode,
    bootstrapPinned,
    command,
    domesticDns,
    feedback,
    foreignDns,
    transportOption,
    notify,
    showImokModal,
    submitInput,
    uci,
    ui,
    when,
    getStatus
  }) {
    var liveDNS = null;
    function bootstrapPreferences(read) {
      var custom = read(bootstrapCustom);
      return {
        warp_dns_mode: read(bootstrapMode) || 'auto',
        warp_dns_pinned: read(bootstrapPinned) || '',
        warp_dns_custom: Array.isArray(custom) ? custom.filter(Boolean) : custom ? [custom] : []
      };
    }
    function checkBootstrapPreferences() {
      var saved = bootstrapPreferences(function (option) {
        return uci.get('routepilot', 'main', option.option);
      });
      var draft = bootstrapPreferences(function (option) {
        return option.formvalue('main');
      });
      if (JSON.stringify(saved) !== JSON.stringify(draft))
        return submitInput('warp-dns-input', 'warp-dns-validate', draft);
    }
    function renderBootstrap(panel, selectFields) {
      return panel(_('WARP 启动解析'), _('为 WARP 启动选 DNS，下次启动生效。'), [
        ...selectFields(['warp_dns_mode', 'warp_dns_pinned', 'warp_dns_custom']),
        bootstrapStatus,
        bootstrapDraft,
        E('div', { class: 'rp-actions' }, [bootstrapTest, bootstrapReselect]),
        E('details', { class: 'imok-result-details' }, [
          E('summary', {}, _('测试结果')),
          bootstrapResults
        ])
      ]);
    }
    function renderLanCandidates(panel) {
      return E('section', { class: 'imok-card-section' }, [
        E('h4', {}, _('局域网 DNS 候选')),
        E('p', { class: 'rp-help' }, _('国内候选含宽带下发地址。')),
        E('div', { class: 'rp-actions' }, [domesticDnsTest.nodes[0], foreignDnsTest.nodes[0]]),
        E('details', { class: 'imok-result-details' }, [
          E('summary', {}, _('候选结果')),
          domesticDnsTest.nodes[1],
          foreignDnsTest.nodes[1]
        ])
      ]);
    }
    function chooseBootstrap(row) {
      var ip = row.address;
      var mode = bootstrapMode.getUIElement('main');
      var pinned = bootstrapPinned.getUIElement('main');
      if (!mode || !pinned) return;
      mode.setValue('pinned');
      pinned.setValue(ip);
      bootstrapDraft.textContent = _('已选 ') + ip + _('，请保存。');
    }
    var bootstrapDraft = E('p', { class: 'rp-help', role: 'status' });
    var bootstrapStatus = E('p', { class: 'rp-help', role: 'status' }, _('尚未读取启动解析状态'));
    var bootstrapResults = E('div', { class: 'rp-help', 'aria-live': 'polite' });
    var bootstrapPending = 0;
    var bootstrapTest = E(
      'button',
      {
        class: 'btn',
        click: function () {
          var values = bootstrapCustom.formvalue('main') || [];
          if (!Array.isArray(values)) values = [values];
          bootstrapPending = Date.now();
          bootstrapTest.disabled = true;
          bootstrapResults.parentNode && (bootstrapResults.parentNode.open = true);
          bootstrapResults.textContent = _('正在测试候选服务器…');
          return submitInput('warp-dns-input', 'warp-dns-test', {
            addresses: values.filter(Boolean)
          }).catch(function (error) {
            bootstrapPending = 0;
            bootstrapTest.disabled = false;
            bootstrapResults.textContent = error.message;
          });
        }
      },
      _('测试 WARP 启动解析')
    );
    var bootstrapReselect = E(
      'button',
      {
        class: 'btn',
        click: function () {
          showImokModal(_('重新选择启动解析'), [
            E('p', {}, _('将按已保存设置重建 WARP 实例，主线路会短暂断线。')),
            E(
              'button',
              {
                class: 'btn',
                click: function () {
                  ui.hideModal();
                }
              },
              _('取消')
            ),
            E(
              'button',
              {
                class: 'btn cbi-button-action',
                click: function () {
                  ui.hideModal();
                  return command('warp-dns-reselect')
                    .then(function () {
                      bootstrapStatus.textContent = _('已提交重建，等待新实例启动。');
                    })
                    .catch(function (error) {
                      notify(error.message, true);
                    });
                }
              },
              _('确认重建')
            )
          ]);
        }
      },
      _('现在重新选择')
    );
    function updateBootstrapDNS(status, result) {
      var chosen = status && status.warp_dns;
      var labels = {
        wan: _('宽带下发'),
        candidate: _('候选表'),
        pinned: _('指定'),
        custom: _('自定义'),
        fallback: _('兜底')
      };
      bootstrapStatus.textContent = chosen
        ? _('当前实例使用 ') +
          chosen.address +
          ' · ' +
          (labels[chosen.source] || _('未知来源')) +
          (chosen.fallback ? _(' · 配置的服务器不可用，已临时改用该地址') : '')
        : _('等待 WARP 实例启动');
      bootstrapReselect.disabled = !chosen;
      var timeout = bootstrapPending && Date.now() - bootstrapPending > 17000;
      var freshResult =
        result &&
        (!bootstrapPending || Number(result.finished_at) * 1000 >= bootstrapPending - 500);
      if (!freshResult) {
        if (timeout) {
          bootstrapPending = 0;
          bootstrapTest.disabled = false;
          bootstrapResults.textContent = _('测试未完成，请重试。');
        }
        return;
      }
      if (result.state === 'running') return;
      bootstrapPending = 0;
      bootstrapTest.disabled = false;
      if (result.state !== 'done') {
        bootstrapResults.textContent = result.message || _('测试未完成，请重试。');
        return;
      }
      renderResolverResults(bootstrapResults, result, chooseBootstrap);
    }
    function renderResolverResults(output, result, choose) {
      var dual = result.transport === 'udp' || result.transport === 'tcp';
      var reasons = {
        timeout: _('超时'),
        dns_error: _('DNS 返回错误'),
        empty_answer: _('空答案'),
        unexpected_answer: _('答案异常'),
        unavailable: _('无法连接')
      };
      var wanNotice =
        result.wan_dns === 'unavailable'
          ? _('当前宽带接口没有可比较的运营商下发 DNS。')
          : result.wan_dns === 'error'
            ? _('暂时无法读取宽带下发 DNS，请重试。')
            : '';
      function formatProbe(probe) {
        if (!probe) return '—';
        return probe.ms == null ? reasons[probe.reason] || _('未通过') : String(probe.ms) + ' ms';
      }
      output.replaceChildren(
        ...(wanNotice ? [E('p', { class: 'rp-help' }, wanNotice)] : []),
        E('table', { class: 'table' }, [
          E(
            'tr',
            { class: 'tr' },
            [
              _('分组'),
              _('名称'),
              _('地址'),
              ...(dual ? ['UDP', 'TCP'] : [_('结果')]),
              _('选择')
            ].map(function (name) {
              return E('th', { class: 'th' }, name);
            })
          ),
          ...(Array.isArray(result.rows) ? result.rows : []).map(function (row) {
            var selectable = dual
              ? [row.udp, row.tcp].some(function (probe) {
                  return probe && probe.ms != null;
                })
              : row.ms != null;
            return E(
              'tr',
              { class: 'tr' },
              [
                row.group,
                row.name,
                row.address,
                ...(dual ? [formatProbe(row.udp), formatProbe(row.tcp)] : [formatProbe(row)]),
                !selectable
                  ? '—'
                  : E(
                      'button',
                      {
                        class: 'btn',
                        click: function () {
                          choose(row);
                        }
                      },
                      _('使用这个')
                    )
              ].map(function (item) {
                return E('td', { class: 'td' }, item);
              })
            );
          })
        ])
      );
    }
    function chooseLanResolver(option, row, testedTransport) {
      var widget = option.getUIElement && option.getUIElement('main');
      if (!widget || !widget.setValue) return;
      var transport =
        transportOption.formvalue('main') ||
        uci.get('routepilot', 'main', 'dns_transport') ||
        'tcp';
      function selectAddress() {
        widget.setValue(row.address);
        notify(_('已选 ') + row.address + _('，请保存。'));
      }
      if (!testedTransport) {
        notify(_('请重新测试候选，确认所选传输方式。'), true);
        return;
      }
      if (row[transport] && row[transport].ms != null) {
        selectAddress();
        return;
      }
      var alternate = transport === 'tcp' ? 'udp' : 'tcp';
      if (!row[alternate] || row[alternate].ms == null) return;
      var transportWidget = transportOption.getUIElement && transportOption.getUIElement('main');
      if (!transportWidget || !transportWidget.setValue) return;
      var protocol = alternate.toUpperCase();
      showImokModal(_('更改上游 DNS 传输'), [
        E(
          'p',
          {},
          _(
            '此地址仅通过 %s 测试。选用时会同时修改国内和代理 DNS 共用的传输设置，保存并应用后生效。'
          ).replace('%s', protocol)
        ),
        E('div', { class: 'rp-actions' }, [
          E('button', { class: 'btn', click: ui.hideModal }, _('取消')),
          E(
            'button',
            {
              class: 'btn cbi-button-action',
              click: function () {
                transportWidget.setValue(alternate);
                selectAddress();
                ui.hideModal();
              }
            },
            _('改为 %s 并选用').replace('%s', protocol)
          )
        ])
      ]);
    }
    function lanDnsTest(kind, label, option) {
      var output = E('div', { class: 'rp-help', 'aria-live': 'polite' });
      var pending = 0;
      var button = E(
        'button',
        {
          class: 'btn',
          click: function () {
            pending = Date.now();
            button.disabled = true;
            if (output.parentNode) output.parentNode.open = true;
            output.textContent = _('正在测试候选服务器…');
            return command('lan-dns-' + kind).catch(function (error) {
              pending = 0;
              button.disabled = false;
              output.textContent = error.message;
            });
          }
        },
        label
      );
      return {
        nodes: [button, output],
        update: function (status, result) {
          button.disabled =
            Boolean(pending) ||
            (kind === 'foreign' &&
              !(
                feedback.fresh(status, Date.now()) &&
                status.state === 'running' &&
                status.backend === 'masque'
              ));
          if (pending && Date.now() - pending > 17000) {
            pending = 0;
            output.textContent = _('测试未完成，请重试。');
            return;
          }
          if (
            !result ||
            result.state === 'running' ||
            (pending && Number(result.finished_at) * 1000 < pending - 500)
          )
            return;
          pending = 0;
          if (result.state !== 'done') {
            output.textContent = result.message || _('测试未完成，请重试。');
            return;
          }
          renderResolverResults(output, result, function (row) {
            chooseLanResolver(option, row, result.transport);
          });
        }
      };
    }
    var domesticDnsTest = lanDnsTest('domestic', _('测试国内 DNS 候选'), domesticDns);
    var foreignDnsTest = lanDnsTest('foreign', _('测试代理 DNS 候选'), foreignDns);
    var dnsInfo = E('p', { role: 'status', 'aria-live': 'polite' }, _('正在读取独立 DNS 状态…')),
      dnsCounts = E('p'),
      dnsEffective = E('p'),
      dnsResults = E('div'),
      dnsTestInfo = E('p', { role: 'status', 'aria-live': 'polite' }, _('尚未执行 DNS 检测'));
    var clearPending = false,
      dnsSubmitting = false,
      dnsJobPending = null;
    var dnsClearButton = E(
      'button',
      {
        class: 'btn',
        disabled: true,
        click: function () {
          if (clearPending || feedback.dnsState(liveDNS, getStatus(), Date.now()) !== 'running')
            return;
          clearPending = true;
          dnsClearButton.disabled = true;
          command('dns-clear')
            .then(JSON.parse)
            .then(function (result) {
              if (
                !result.ok ||
                !liveDNS ||
                ['runtime_id', 'generation', 'process_instance'].some(function (k) {
                  return result[k] !== liveDNS[k];
                })
              )
                throw Error(_('连接已改变，请刷新后确认缓存状态'));
              notify(
                _('已清理 imOK 独立 DNS 缓存，移除 ') +
                  result.removed +
                  _(' 条；未清理系统、SSR 缓存或动态分流规则。')
              );
            })
            .catch(function (e) {
              notify(e.message, true);
            })
            .finally(function () {
              clearPending = false;
              dnsClearButton.disabled =
                feedback.dnsState(liveDNS, getStatus(), Date.now()) !== 'running' ||
                !liveDNS.control_available;
            });
        }
      },
      _('清理本插件 DNS 缓存')
    );
    var dnsProbeButton = E(
      'button',
      {
        class: 'btn',
        disabled: true,
        click: function () {
          if (feedback.dnsState(liveDNS, getStatus(), Date.now()) !== 'running') return;
          if (dnsSubmitting) return;
          dnsSubmitting = true;
          dnsProbeButton.disabled = true;
          if (dnsResults.parentNode) dnsResults.parentNode.open = true;
          dnsTestInfo.textContent = _('正在提交 DNS 检测…');
          command('dns-test')
            .then(JSON.parse)
            .then(function (result) {
              dnsTestInfo.textContent = result.message;
              if (result.state === 'submitted' || result.state === 'running')
                dnsJobPending = { id: result.request_id || null, time: Date.now() };
            })
            .catch(function (e) {
              dnsTestInfo.textContent = e.message;
              dnsProbeButton.disabled = false;
            })
            .finally(function () {
              dnsSubmitting = false;
            });
        }
      },
      _('检测当前 DNS 解析')
    );
    var dnsCacheCard = E('section', { class: 'imok-card-section' }, [
      E('h3', {}, _('独立 DNS 状态与缓存')),
      dnsInfo,
      dnsEffective,
      dnsCounts,
      dnsClearButton,
      E(
        'p',
        { class: 'rp-help' },
        _(
          '显示已生效的 DNS，表单未保存的修改不会用于检测。清理只作用于本插件缓存，不重启隧道，也不清除动态分流规则。'
        )
      )
    ]);
    var dnsTestCard = E('section', { class: 'imok-card-section' }, [
      E('h3', {}, _('DNS 解析检测')),
      E('p', { class: 'rp-help' }, _('从路由器测试生效的国内和代理 DNS。')),
      dnsProbeButton,
      dnsTestInfo,
      E('details', { class: 'imok-result-details' }, [E('summary', {}, _('解析明细')), dnsResults])
    ]);
    function updateDNS(data, test, status) {
      liveDNS = data;
      var now = Date.now(),
        state = feedback.dnsState(data, status, now);
      dnsInfo.textContent = {
        running: _('独立 DNS 正在运行'),
        inactive: _('分流未运行，独立 DNS 当前不可用'),
        stale: _('DNS 状态已过期或连接已改变'),
        unavailable: _('暂未取得独立 DNS 状态')
      }[state];
      dnsCounts.textContent = '';
      dnsEffective.textContent = '';
      var valid = state === 'running';
      dnsClearButton.disabled = clearPending || !valid || !data.control_available;
      if (valid) {
        var entries = 0,
          hits = 0,
          requests = 0,
          errors = 0,
          classificationBusy = 0;
        data.policies.forEach(function (p) {
          entries += p.cache_entries;
          hits += p.cache_hits;
          requests += p.requests;
          errors += p.errors;
          classificationBusy += Number(p.classification_busy || 0);
        });
        dnsCounts.textContent =
          _('缓存条目：') +
          entries +
          ' · ' +
          _('命中：') +
          hits +
          ' / ' +
          _('查询：') +
          requests +
          ' · ' +
          _('转发异常：') +
          errors +
          ' · ' +
          _('分类等待超时：') +
          classificationBusy;
        dnsEffective.textContent =
          _('生效国内上游：') +
          data.upstreams.dns_domestic +
          ' · ' +
          String(data.upstreams.transport || '').toUpperCase() +
          ' · ' +
          _('代理上游：') +
          (data.upstreams.proxy_dns_override
            ? data.upstreams.proxy_dns_override + _(' · DoH，经备用线路加密')
            : data.upstreams.dns_foreign + _(' · 普通 DNS，非加密上游'));
        if (!data.control_available) dnsInfo.textContent += _('；缓存控制暂不可用，转发仍独立运行');
      }
      var ts = feedback.dnsTestState(test, status, data, now);
      if (dnsJobPending && now - dnsJobPending.time > 50000) dnsJobPending = null;
      var awaiting =
        dnsJobPending && (!test || (dnsJobPending.id && test.request_id !== dnsJobPending.id));
      if (dnsJobPending && !awaiting && ts !== 'busy') dnsJobPending = null;
      dnsProbeButton.disabled = dnsSubmitting || !valid || ts === 'busy' || !!dnsJobPending;
      dnsTestInfo.textContent = awaiting
        ? _('等待本次 DNS 检测作业，最长约 40 秒…')
        : {
            busy: _('正在检测，最长约 40 秒…'),
            success: _('本机受管 DNS 入口解析通过'),
            error: _('部分 DNS 解析检测失败'),
            warning: _('部分 DNS 入口未就绪或没有完整解析结果'),
            stale: _('历史 DNS 结果已过期或检测环境已改变，请重新检测'),
            pending: _('暂未取得有效的 DNS 检测结果'),
            unavailable: _('当前 DNS 未运行，尚不能检测')
          }[ts];
      if (test && test.finished_at)
        dnsTestInfo.textContent += ' · ' + _('检测时间：') + when(test.finished_at);
      var rows = (test && Array.isArray(test.results) ? test.results : []).map(function (r) {
        return E('tr', {}, [
          E('td', {}, r.label || _('受管范围 ') + r.policy),
          E('td', {}, r.domain + ' · ' + String(r.protocol || '').toUpperCase()),
          E(
            'td',
            {},
            (r.configured_route === 'domestic' ? _('国内上游：') : _('代理上游：')) + r.upstream
          ),
          E(
            'td',
            {},
            (ts === 'stale' || awaiting ? _('历史：') : '') +
              (r.message || r.state) +
              (r.addresses && r.addresses.length ? ' · ' + r.addresses.join(', ') : '')
          ),
          E('td', {}, r.ms != null ? r.ms + ' ms' : '—')
        ]);
      });
      dnsResults.replaceChildren(
        E('table', { class: 'table' }, [
          E(
            'thead',
            {},
            E(
              'tr',
              {},
              ['受管范围', '测试域名 / 接入', '按规则选择的上游', '解析结果', '耗时'].map(
                function (t) {
                  return E('th', {}, _(t));
                }
              )
            )
          ),
          E('tbody', {}, rows)
        ])
      );
    }
    return {
      checkPreferences: checkBootstrapPreferences,
      clearDraft: function () {
        bootstrapDraft.textContent = '';
      },
      running: function (status) {
        return liveDNS && feedback.dnsState(liveDNS, status, Date.now()) === 'running';
      },
      update: function (status, state, test, bootstrap, domestic, foreign) {
        updateDNS(state, test, status);
        updateBootstrapDNS(status, bootstrap);
        domesticDnsTest.update(status, domestic);
        foreignDnsTest.update(status, foreign);
      },
      cacheCard: dnsCacheCard,
      testCard: dnsTestCard,
      renderBootstrap,
      renderLanCandidates
    };
  },
  createSpeed: function ({ bytes, command, feedback, showImokModal, ui, when }) {
    function speedCpu(row) {
      if (!Number.isFinite(row.cpu_busy_percent)) return '';
      var text = _('路由器 CPU 平均 ') + row.cpu_busy_percent + '%';
      if (Number.isFinite(row.cpu_busiest_core_percent))
        text += _(' · 最忙核心 ') + row.cpu_busiest_core_percent + '%';
      return text;
    }
    var speedOutput = E('div', { class: 'rp-help', 'aria-live': 'polite' });
    var speedPending = 0;
    var speedButton = E(
      'button',
      {
        class: 'btn',
        click: function () {
          showImokModal(_('开始线路测速'), [
            E('p', {}, _('直连和代理各最多下载 200 MB。测速从路由器发起。')),
            E(
              'button',
              {
                class: 'btn',
                click: function () {
                  ui.hideModal();
                }
              },
              _('取消')
            ),
            E(
              'button',
              {
                class: 'btn cbi-button-action',
                click: function () {
                  ui.hideModal();
                  speedPending = Date.now();
                  speedButton.disabled = true;
                  speedOutput.textContent = _('正在测速…');
                  return command('speed-test').catch(function (error) {
                    speedPending = 0;
                    speedButton.disabled = false;
                    speedOutput.textContent = error.message;
                  });
                }
              },
              _('确认测速')
            )
          ]);
        }
      },
      _('测速')
    );
    function speedRate(row) {
      var bound = ['budget', 'sample_limit'].includes(row.stop_reason) ? '≥ ' : '';
      return row.measurement_version === 2
        ? (row.stable_mbps == null
            ? _('全程平均 ') + bound + row.average_mbps
            : _('后 5 秒平均 ') + bound + row.stable_mbps) + ' Mbps'
        : _('旧测速结果已作废');
    }
    function speedRows(result) {
      if (result.state === 'stale') return [];
      var rows = Array.isArray(result.rows) ? result.rows : [];
      return rows.map(function (row) {
        var name = row.path === 'proxy' ? _('代理') : _('宽带直连');
        return E(
          'p',
          {},
          name +
            ' · ' +
            (row.state !== 'done'
              ? row.message || _('无有效结果')
              : row.measurement_version !== 2
                ? speedRate(row)
                : speedRate(row) +
                  (row.stop_reason === 'budget'
                    ? _('（已达测速上限）')
                    : row.stop_reason === 'sample_limit'
                      ? _('（下载样本已用完）')
                      : '') +
                  (row.stable_mbps != null ? _(' · 全程平均 ') + row.average_mbps + ' Mbps' : '') +
                  (row.peak_mbps != null ? _(' · 单秒峰值 ') + row.peak_mbps + ' Mbps' : '') +
                  _(' · HTTPS 建连中位数 ') +
                  row.https_connect_median_ms +
                  ' ms' +
                  _(' · 最慢 ') +
                  row.https_connect_worst_ms +
                  ' ms' +
                  (speedCpu(row) ? ' · ' + speedCpu(row) : ''))
        );
      });
    }
    function updateSpeed(status, result) {
      var running =
        result && result.state === 'running' && Date.now() / 1000 - result.started_at <= 65;
      if (speedPending && Date.now() - speedPending > 65000) speedPending = 0;
      if (result && result.finished_at * 1000 >= speedPending) speedPending = 0;
      speedButton.disabled = !!(
        speedPending ||
        running ||
        !feedback.fresh(status, Date.now()) ||
        status.state !== 'running'
      );
      if (!result || (speedPending && !running)) return;
      speedOutput.replaceChildren(
        E(
          'p',
          {},
          running
            ? _('正在测速…')
            : result.state === 'stale'
              ? _('线路已变化，本次结果失效。')
              : [result.protocol, when(result.finished_at), _('下载 ') + bytes(result.bytes)]
                  .filter(Boolean)
                  .join(' · ')
        ),
        ...speedRows(result)
      );
      if (result.history && result.history.length > 1) {
        speedOutput.appendChild(
          E('details', {}, [
            E('summary', {}, _('历史测速')),
            ...result.history
              .slice(0, -1)
              .reverse()
              .map(function (entry) {
                return E('div', {}, [
                  E(
                    'p',
                    {},
                    when(entry.finished_at) + (entry.state === 'stale' ? _(' · 已失效') : '')
                  ),
                  ...speedRows(entry)
                ]);
              })
          ])
        );
      }
    }
    return {
      rate: speedRate,
      cpu: speedCpu,
      update: updateSpeed,
      render: function (panel) {
        return panel(_('线路测速'), _('测量路由器到出口的速度，局域网设备体验需另测。'), [
          speedButton,
          speedOutput
        ]);
      }
    };
  },
  configure: function ({ pageOption, form, center }) {
    var o = pageOption(
      'dns',
      form.Value,
      'dns_domestic',
      _('国内 DNS'),
      _('中国域名与自定义直连域名使用本地宽带解析。')
    );
    o.datatype = 'ip4addr';
    o.default = '223.5.5.5';
    var domesticDns = o;
    o = pageOption(
      'dns',
      form.Value,
      'dns_foreign',
      _('代理 DNS'),
      _('主线路的其他普通 DNS 经 WARP 隧道发送。应用自带的 DoH/DoT 不能按普通 DNS 域名规则分类。')
    );
    o.datatype = 'ip4addr';
    o.default = '1.1.1.1';
    var foreignDns = o;
    o = pageOption('dns', form.ListValue, 'dns_transport', _('上游 DNS 传输'));
    o.value('tcp', 'TCP');
    o.value('udp', 'UDP');
    o.default = 'tcp';
    var transportOption = o;
    o = pageOption('dns', form.Flag, 'dns_cache', _('启用独立 DNS 缓存'));
    o.default = '1';
    o.rmempty = false;
    var bootstrapMode = pageOption('dns', form.ListValue, 'warp_dns_mode', _('启动解析方式'));
    bootstrapMode.value('auto', _('自动优选'));
    bootstrapMode.value('pinned', _('指定一个'));
    bootstrapMode.value('custom', _('自己填写'));
    bootstrapMode.default = 'auto';
    var bootstrapPinned = pageOption('dns', form.Value, 'warp_dns_pinned', _('指定服务器'));
    bootstrapPinned.datatype = 'ip4addr';
    bootstrapPinned.depends('warp_dns_mode', 'pinned');
    var bootstrapCustom = pageOption(
      'dns',
      form.DynamicList,
      'warp_dns_custom',
      _('自定义 IPv4，最多 4 个')
    );
    bootstrapCustom.datatype = 'ip4addr';
    bootstrapCustom.depends('warp_dns_mode', 'custom');
    o = pageOption('dns', form.DummyValue, '_ipv6_info', _('IPv6 与 AAAA'));
    o.cfgvalue = function () {
      return center
        ? _(
            'AAAA 过滤由“出口与分流”或“设备”页面中的 IPv6 处理方式决定。本插件只代理 IPv4；允许 IPv6 直连会绕过代理规则。'
          )
        : _(
            'AAAA 是否过滤由“分流设置 / 设备管理”的 IPv6 处理方式决定；本插件仅代理 IPv4，允许 IPv6 直连会绕过所有代理规则。'
          );
    };
    return {
      domesticDns,
      foreignDns,
      transportOption,
      bootstrapMode,
      bootstrapPinned,
      bootstrapCustom
    };
  }
});

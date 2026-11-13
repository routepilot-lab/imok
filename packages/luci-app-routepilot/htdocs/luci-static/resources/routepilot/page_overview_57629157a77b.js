'use strict';
'require baseclass';

return baseclass.extend({
  render: function (
    overviewHero,
    trafficCard,
    nodeCard,
    quickCard,
    protectionHistory,
    connectionHistory,
    versionInfo,
    diagnostics
  ) {
    return [
      E('div', { class: 'imok-overview-main' }, [
        E('div', { class: 'imok-overview-left' }, [overviewHero, nodeCard]),
        trafficCard
      ]),
      E('div', { class: 'imok-overview-secondary' }, [quickCard]),
      E('section', { class: 'cbi-section imok-technical', 'aria-label': _('技术详情') }, [
        E('h3', {}, _('详情与诊断')),
        protectionHistory,
        connectionHistory,
        diagnostics,
        versionInfo
      ])
    ];
  },
  create: function ({
    base,
    buildInfo,
    bytes,
    getCatalog,
    connectionDirectory,
    data,
    directoryLabel,
    feedback,
    isCurrentView,
    page,
    powerButton,
    refreshDirectory,
    switchFeedback,
    when,
    wizardConfig,
    enabled,
    getStatus
  }) {
    var overviewState = E('strong', { class: 'rp-overview-state' }, _('读取中'));
    var overviewBadge = E('span', { class: 'rp-status-badge' }, _('读取中'));
    var overviewMeta = E('p', { class: 'rp-help' }),
      overviewLine = E('p', { class: 'rp-overview-line', role: 'status', 'aria-live': 'polite' });
    var overviewFacts = {};
    var overviewFactsNode = E(
      'dl',
      { class: 'imok-overview-facts' },
      [
        ['enabled', _('独立开关')],
        ['applied', _('配置应用')],
        ['tunnel', _('隧道状态')],
        ['website', _('网站检测')],
        ['protocol', _('连接协议')],
        ['exit', _('当前出口')],
        ['scope', _('接管范围')],
        ['guard', _('设备断网保护')]
      ].map(function (item) {
        var value = (overviewFacts[item[0]] = E('dd', {}, '—'));
        if (item[0] === 'tunnel') value.replaceChildren(overviewBadge);
        return E('div', {}, [E('dt', {}, item[1]), value]);
      })
    );
    var overviewSummary = E('p', { class: 'rp-help', role: 'status' });
    var overviewAction = E('a', { class: 'btn' }, _('查看详情'));
    var overviewSetup = E('section', { class: 'cbi-section', hidden: '' });
    var runtimeInfo = null;
    function updateSetup(v) {
      var setup = wizardConfig();
      var active = v && v.state === 'running';
      var steps = [
        [_('安装运行环境'), 'runtime', active || (runtimeInfo && runtimeInfo.installed)],
        [_('登录团队'), 'cloudflare/account', active || getCatalog().available],
        [_('选择出口'), 'cloudflare/routing', active || !!setup.exit_id],
        setup.scope === 'lan'
          ? [_('选择局域网'), 'cloudflare/routing', active || setup.lans > 0]
          : [_('选择设备'), 'cloudflare/devices', active || setup.devices > 0],
        [_('启用'), 'overview#imok-power', enabled()]
      ];
      overviewSetup.hidden = !!steps.every(function (step) {
        return step[2];
      });
      if (overviewSetup.hidden) return;
      overviewSetup.replaceChildren(
        E('h3', {}, _('首次使用')),
        E(
          'ol',
          {},
          steps.map(function (step) {
            return E('li', {}, [
              E('a', { href: base + '/' + step[1] }, step[0]),
              step[2] ? _(' · 已完成') : _(' · 待完成')
            ]);
          })
        )
      );
    }
    var overviewHero = E('section', { class: 'cbi-section rp-overview-hero' }, [
      E('div', { class: 'rp-hero-heading' }, [
        E('div', { class: 'imok-connection-heading' }, [E('h3', {}, _('连接状态')), overviewState]),
        powerButton
      ]),
      E('div', { class: 'rp-actions' }, [overviewSummary, overviewAction]),
      overviewFactsNode,
      overviewSetup,
      overviewLine,
      E('details', { class: 'rp-stat-details' }, [E('summary', {}, _('连接详情')), overviewMeta]),
      switchFeedback
    ]);
    var trafficRange = 'today',
      latestTraffic = null,
      trafficNote = E('p', { class: 'rp-data-note', role: 'status' }, _('正在读取流量观测…'));
    var trafficMeta = E('p', { class: 'rp-help' }),
      metricFields = {};
    var metricCards = ['proxy', 'direct'].map(function (key) {
      var value = E('strong', { class: 'rp-metric-number' }, '—'),
        detail = E('p', { class: 'rp-help' }),
        speed = E('p', { class: 'rp-help' });
      var today = E('strong', {}, '—'),
        total = E('strong', {}, '—');
      metricFields[key] = {
        value: value,
        detail: detail,
        speed: speed,
        today: today,
        total: total
      };
      return E('section', { class: 'cbi-section rp-metric-card rp-' + key }, [
        E('div', { class: 'rp-metric-heading' }, [
          E(
            'span',
            { class: 'rp-metric-icon', 'aria-hidden': 'true' },
            key === 'proxy' ? '↗' : '⇄'
          ),
          E('h3', {}, key === 'proxy' ? _('代理出口 · 已观测') : _('直连线路 · 已观测')),
          value
        ]),
        E('dl', { class: 'imok-traffic-context' }, [
          E('div', {}, [E('dt', {}, _('今日')), E('dd', {}, today)]),
          E('div', {}, [E('dt', {}, _('累计')), E('dd', {}, total)])
        ]),
        detail,
        speed
      ]);
    });
    var rangeButtons = {};
    var rangeControls = E(
      'div',
      { class: 'rp-segments', role: 'group', 'aria-label': _('流量统计时段') },
      [
        ['today', _('今日')],
        ['total', _('累计')]
      ].map(function (item) {
        var b = (rangeButtons[item[0]] = E(
          'button',
          {
            class: 'btn',
            'aria-pressed': item[0] === trafficRange ? 'true' : 'false',
            click: function () {
              trafficRange = item[0];
              Object.keys(rangeButtons).forEach(function (k) {
                rangeButtons[k].setAttribute('aria-pressed', k === trafficRange ? 'true' : 'false');
              });
              updateTraffic(latestTraffic);
            }
          },
          item[1]
        ));
        return b;
      })
    );
    var weekChart = E('div', {
        class: 'rp-week-chart',
        'aria-label': _('近七天已观测流量，蓝色代理，青色直连')
      }),
      weekLegend = E('p', { class: 'rp-help' }, [
        E('span', { class: 'rp-legend-proxy' }, _('● 代理')),
        E('span', { class: 'rp-legend-direct' }, _('● 直连')),
        _(' · 缺失日期显示 —，不补零')
      ]);
    var trafficCard = E('section', { class: 'cbi-section rp-traffic-section' }, [
      E('div', { class: 'rp-hero-heading' }, [E('h3', {}, _('流量观测')), rangeControls]),
      trafficNote,
      E('div', { class: 'rp-metric-grid' }, metricCards),
      E('div', { class: 'rp-trend-heading' }, [E('strong', {}, _('近 7 天')), weekLegend]),
      weekChart,
      E('details', { class: 'rp-stat-details' }, [
        E('summary', {}, _('统计口径与采样详情')),
        trafficMeta
      ])
    ]);
    var nodeCount = E('span', { class: 'rp-group-state' }, '—'),
      nodeSummary = E('p'),
      nodeTime = E('p', { class: 'rp-help' });
    var nodeRefresh = E(
      'button',
      {
        class: 'btn',
        click: function () {
          nodeRefresh.disabled = true;
          return refreshDirectory().finally(function () {
            nodeRefresh.disabled = false;
          });
        }
      },
      _('刷新出口目录')
    );
    var nodeCard = E('section', { class: 'cbi-section imok-node-card' }, [
      E('div', { class: 'rp-hero-heading' }, [
        E('h3', {}, [_('账户出口'), nodeCount]),
        E('a', { href: base + '/cloudflare/routing' }, _('管理全部出口 →'))
      ]),
      nodeSummary,
      nodeTime,
      E('div', { class: 'rp-summary-actions' }, [nodeRefresh])
    ]);
    if (page !== 'overview')
      connectionDirectory.appendChild(
        E('details', { class: 'rp-stat-details' }, [
          E('summary', {}, _('账户目录时间与刷新')),
          nodeTime,
          nodeRefresh
        ])
      );
    var versionInfo = E('p', { class: 'rp-help' }, _('客户端版本正在读取…'));
    var quickCard = E('section', { class: 'cbi-section', 'aria-label': _('快捷操作') }, [
      E(
        'div',
        { class: 'rp-quick-grid' },
        [
          ['cloudflare/routing', '↗', _('出口与分流'), _('查看当前出口和接管范围')],
          [
            'overview#imok-network-diagnostics',
            '◎',
            _('检测与测速'),
            _('测量线路速度和网站连通性')
          ],
          ['cloudflare/dns', '◇', _('DNS'), _('解析、缓存与候选测试')],
          ['cloudflare/workers', '⌁', _('Workers 备用'), _('部署、优选和独立检测')]
        ].map(function (q) {
          return E('a', { class: 'rp-quick-tile', href: base + '/' + q[0] }, [
            E('span', { class: 'rp-quick-icon', 'aria-hidden': 'true' }, q[1]),
            E('span', {}, [E('strong', {}, q[2]), E('small', {}, q[3])])
          ]);
        })
      )
    ]);
    if (page === 'overview' && data.runtimeRead)
      data.runtimeRead.then(function (info) {
        if (!isCurrentView()) return;
        runtimeInfo = info;
        updateSetup(getStatus());
        if (
          !isCurrentView() ||
          !info ||
          !(
            info.update_available ||
            !info.installed ||
            (info.check && info.check.state === 'failed')
          )
        )
          return;
        quickCard.appendChild(
          E('p', { class: 'rp-help' }, [
            info.update_available
              ? _('WARP 有新版本可供检查与手动升级。')
              : !info.installed
                ? _('首次使用请先安装路由器的 WARP 运行环境。')
                : _('WARP 版本检查失败，最新版本暂未确认。'),
            E('a', { href: base + '/runtime' }, _(' 前往 WARP 安装与更新'))
          ])
        );
      });
    function validTrafficCounts(v) {
      return (
        v &&
        ['proxy_up', 'proxy_down', 'direct_up', 'direct_down'].every(function (k) {
          return Number.isSafeInteger(v[k]) && v[k] >= 0;
        })
      );
    }
    var weekChartKey = null;
    function updateWeekChart(traffic) {
      var today = traffic && traffic.today;
      var end = /^\d{4}-\d{2}-\d{2}$/.test(today || '') ? Date.parse(today + 'T12:00:00Z') : NaN;
      var days = [];
      if (Number.isFinite(end) && new Date(end).toISOString().slice(0, 10) === today) {
        for (var offset = 6; offset >= 0; offset--) {
          var day = new Date(end - offset * 86400000).toISOString().slice(0, 10);
          var bucket = traffic.days && traffic.days[day],
            counts = bucket && bucket.bytes;
          days.push([
            day,
            validTrafficCounts(counts)
              ? [counts.proxy_up, counts.proxy_down, counts.direct_up, counts.direct_down]
              : null
          ]);
        }
      }
      var key = JSON.stringify(days);
      if (key === weekChartKey) return;
      var max = days.reduce(function (value, day) {
        return day[1]
          ? Math.max(
              value,
              day[1].reduce(function (a, b) {
                return a + b;
              }, 0)
            )
          : value;
      }, 1);
      var nodes = days.map(function (entry) {
        var day = entry[0],
          counts = entry[1];
        var proxy = counts ? counts[0] + counts[1] : 0,
          direct = counts ? counts[2] + counts[3] : 0;
        var bars = counts
          ? [
              E('span', { class: 'rp-bar-direct', style: 'height:' + (direct / max) * 100 + '%' }),
              E('span', { class: 'rp-bar-proxy', style: 'height:' + (proxy / max) * 100 + '%' })
            ]
          : [E('span', { class: 'rp-no-data' }, '—')];
        return E(
          'div',
          {
            class: 'rp-day',
            role: 'img',
            tabindex: '0',
            title:
              day +
              ': ' +
              (counts
                ? _('代理上传 ') +
                  bytes(counts[0]) +
                  _(' / 下载 ') +
                  bytes(counts[1]) +
                  _('；直连上传 ') +
                  bytes(counts[2]) +
                  _(' / 下载 ') +
                  bytes(counts[3])
                : _('没有采集数据')),
            'aria-label':
              day +
              ': ' +
              (counts
                ? _('代理 ') + bytes(proxy) + _('，直连 ') + bytes(direct)
                : _('没有采集数据'))
          },
          [
            E('span', { class: 'rp-day-number' }, counts ? bytes(proxy + direct) : '—'),
            E('div', { class: 'rp-day-bars', 'aria-hidden': 'true' }, bars),
            E('span', { class: 'rp-day-label' }, day.slice(5))
          ]
        );
      });
      if (!nodes.length) nodes.push(E('p', { class: 'rp-help' }, _('采集开始后显示每日趋势。')));
      weekChart.replaceChildren(...nodes);
      weekChartKey = key;
    }
    function updateTraffic(v) {
      latestTraffic = v;
      var age = v ? Date.now() / 1000 - Number(v.time) : Infinity,
        live = v && v.schema === 1 && age >= -5 && age <= 20 && v.state === 'observing';
      var limited = !v || v.offload !== 'not_detected';
      trafficNote.textContent = !live
        ? v && v.message
          ? v.message
          : !v
            ? _('尚无采集数据，不能确认当前用量。')
            : _('采集已暂停或过期；以下仅为历史观测，不是当前用量。')
        : limited
          ? _('仅统计未加速部分，可能远低于实际用量；图表与累计均为观测下限。')
          : _('观测下限，不作账单；历史可能存在采集中断。');
      trafficCard.setAttribute('data-stale', live ? 'false' : 'true');
      if (v && v.clock_synced === false)
        trafficNote.textContent += _(' 路由器时间尚未同步，新增流量暂不分配到日期。');
      var bucket = v && v.days && v.days[v.today],
        counts = trafficRange === 'total' ? v && v.total : bucket && bucket.bytes,
        valid = validTrafficCounts(counts);
      ['proxy', 'direct'].forEach(function (k) {
        var f = metricFields[k];
        var todayCounts = bucket && bucket.bytes,
          totalCounts = v && v.total;
        f.today.textContent = validTrafficCounts(todayCounts)
          ? bytes(todayCounts[k + '_up'] + todayCounts[k + '_down'])
          : '—';
        f.total.textContent = validTrafficCounts(totalCounts)
          ? bytes(totalCounts[k + '_up'] + totalCounts[k + '_down'])
          : '—';
        f.value.textContent = valid ? bytes(counts[k + '_up'] + counts[k + '_down']) : '—';
        f.detail.textContent = valid
          ? (live ? '' : _('历史观测，停止于 ') + when(v.time) + ' · ') +
            _('↑ 上传 ') +
            bytes(counts[k + '_up']) +
            _(' · ↓ 下载 ') +
            bytes(counts[k + '_down'])
          : _('此时段没有采集数据');
        var last = v && Array.isArray(v.history) && v.history[v.history.length - 1],
          rate = live && last && last.rate;
        f.speed.textContent =
          rate && Number.isFinite(rate[k + '_down']) && Number.isFinite(rate[k + '_up'])
            ? _('已观测速率 ↑ ') +
              bytes(rate[k + '_up']) +
              '/s ↓ ' +
              bytes(rate[k + '_down']) +
              '/s'
            : _('当前速率：未知');
      });
      updateWeekChart(v);
      trafficMeta.textContent = v
        ? _('累计为自此时间起的观测下限：') +
          when(v.started_at) +
          _(' · 最后采样：') +
          when(v.time) +
          _(' · 路由器时区：') +
          String(v.timezone || _('未知')) +
          _(
            '。每 5 分钟保存一次，掉电可能丢失未保存部分。仅含受管 IPv4 公网转发；不含 IPv6、局域网、路由器本机和隧道封装开销。'
          ) +
          (v.gaps ? _(' 已记录不完整采样区间或边界：') + v.gaps + _(' 次。') : '') +
          (validTrafficCounts(v.unallocated) &&
          Object.keys(v.unallocated).some(function (k) {
            return v.unallocated[k] > 0;
          })
            ? _(' 有无法归属到日期的流量，仅纳入累计。')
            : '')
        : _('升级前的流量无法补算。');
      versionInfo.textContent =
        'imOK ' +
        (buildInfo.package || _('版本待读取')) +
        ' · WARP ' +
        (v && v.client_version ? v.client_version : _('版本未知')) +
        _(' · 仅显示本机读到的版本；是否有更新请核对官方发布记录。');
    }
    function updateOverview(v, t) {
      var now = Date.now(),
        config = wizardConfig(),
        catalog = getCatalog(),
        fresh = feedback.fresh(v, now),
        running = fresh && v.state === 'running',
        activeExit = running
          ? v.backend === 'worker'
            ? v.backup_name || _('Workers 备用')
            : directoryLabel(v.exit, catalog)
          : _('当前出口未确认'),
        tone = !fresh
          ? 'unknown'
          : running && v.tunnel_status !== 'unknown'
            ? 'good'
            : v.state === 'error'
              ? 'bad'
              : 'warn';
      overviewBadge.setAttribute('data-tone', tone);
      overviewBadge.textContent = !fresh
        ? _('？ 状态未知')
        : running
          ? v.backend === 'worker'
            ? _('✓ 备用线路运行中（TCP）')
            : v.tunnel_status === 'unknown'
              ? _('？ 客户端状态无法识别')
              : _('✓ 隧道已连接')
          : v.state === 'error'
            ? _('! 连接故障，请查看详情')
            : v.state === 'disabled'
              ? _('○ 已关闭')
              : v.state === 'preparing'
                ? _('◷ 登录准备中')
                : _('◷ 等待连接');
      overviewLine.textContent = !fresh
        ? _('状态已过期，当前连接未确认')
        : running
          ? (v.backend === 'worker' ? _('Workers 备用') : _('WARP 主线路')) +
            ' · ' +
            _('实际协议：') +
            (v.protocol || _('待确认')) +
            ' · ' +
            activeExit +
            ' · IP ' +
            (v.exit_ip || '—')
          : v.message || _('等待连接');
      overviewMeta.textContent =
        _('状态更新时间：') +
        when(v && v.time) +
        _(' · 接管范围：') +
        (v && v.scope === 'lan' ? _('所选局域网') : _('所选设备')) +
        (v && v.tunnel_warning ? ' ' + v.tunnel_warning : '') +
        (v && v.protocol_warning ? ' ' + v.protocol_warning : '') +
        (running && v.device_wait_message ? ' · ' + v.device_wait_message : '');
      var recent = catalog.available && now - catalog.read_at <= 60000,
        count = catalog.available ? catalog.networks.length : null;
      overviewFacts.enabled.textContent = enabled() ? _('已启用') : _('已关闭');
      overviewFacts.protocol.textContent = fresh && v && v.protocol ? v.protocol : _('未确认');
      overviewFacts.exit.textContent = activeExit;
      overviewFacts.scope.textContent = fresh
        ? (v.scope === 'lan' ? _('所选局域网') : _('所选设备')) +
          (Array.isArray(v.devices) ? ' · ' + v.devices.length + _(' 条接管策略') : '')
        : _('未确认');
      overviewFacts.applied.textContent =
        fresh &&
        ['running', 'disabled', 'preparing'].indexOf(v.state) >= 0 &&
        config.nonce &&
        v.applied_nonce === config.nonce
          ? _('当前配置已确认应用')
          : _('尚未确认当前配置已应用');
      var site = feedback.wizardVerification(t, v, config, now);
      var summary = feedback.overviewSummary(
        { enabled: enabled(), nonce: config.nonce },
        v,
        site,
        now
      );
      var titles = {
        normal: _('正常'),
        partial: _('部分可用'),
        unverified: _('待确认'),
        unavailable: _('不可用'),
        off: _('已关闭')
      };
      var reasons = {
        disabled: _('分流开关已关闭'),
        stale: _('运行状态尚未确认'),
        tunnel: _('线路等待恢复'),
        devices: _('等待核对设备地址'),
        guard: _('受保护设备等待放行'),
        apply: _('新设置等待应用'),
        unknown_tunnel: _('客户端状态待确认'),
        website: _('部分网站检测失败'),
        restricted: _('所测网站访问受限'),
        testing: _('检测中'),
        verified: _('路由器网站检测已通过'),
        not_tested: _('等待后台联网检测')
      };
      overviewState.textContent = titles[summary.state];
      overviewState.setAttribute('data-state', summary.state);
      overviewSummary.textContent = reasons[summary.reason];
      overviewAction.setAttribute('href', base + '/' + summary.page);
      overviewAction.textContent =
        {
          disabled: _('前往启用'),
          stale: _('检查 DNS'),
          tunnel: _('检查 DNS'),
          devices: _('核对设备'),
          guard: _('查看设备'),
          apply: _('查看分流'),
          unknown_tunnel: _('检查 WARP')
        }[summary.reason] || _('联网检测');
      updateSetup(fresh ? v : null);
      var guardLabel = {
        off: _('未启用'),
        blocking: _('受保护设备正在等待放行'),
        ready: _('已核验设备按策略联网'),
        no_targets: _('未选择受保护设备'),
        unverified: _('保护未确认，请查看连接状态')
      };
      overviewFacts.guard.textContent =
        fresh && guardLabel[v.egress_guard] ? guardLabel[v.egress_guard] : _('状态待确认');
      nodeCount.textContent =
        count == null ? _('目录待确认') : _('共 ') + String(count) + _(' 个可选网络');
      var verified =
          v && v.backend === 'worker'
            ? 'pending'
            : feedback.wizardProxyVerification(t, v, config, now),
        proof = verified === 'success',
        proofText = proof
          ? _('1 个最近检测通过')
          : verified === 'error'
            ? _('当前出口检测失败')
            : _('当前出口尚无有效检测');
      overviewFacts.website.textContent =
        site === 'success'
          ? _('近期所测网站通过')
          : site === 'error'
            ? _('所测网站存在失败')
            : site === 'warning'
              ? _('所测网站访问受限')
              : site === 'busy'
                ? _('检测中')
                : _('尚无有效检测');
      nodeSummary.textContent =
        (v && v.backend !== 'worker' ? _('当前出口：') + activeExit + ' · ' : '') +
        (v && v.backend === 'worker'
          ? _('当前运行备用线路；账户出口未接管。')
          : (fresh ? (running ? _('1 个当前连接') : _('0 个当前连接')) : _('当前连接数未知')) +
            ' · ' +
            proofText);

      nodeTime.textContent = catalog.available
        ? (recent ? _('目录读取时间：') : _('目录可能已变化，上次读取：')) +
          when(catalog.read_at / 1000) +
          (proof ? _(' · 网页检测通过时间：') + when(t.finished_at) + _('（5 分钟内有效）') : '')
        : _('目录暂不可用，请刷新；不代表账户没有出口。');
    }
    return {
      hero: overviewHero,
      traffic: trafficCard,
      nodes: nodeCard,
      quick: quickCard,
      version: versionInfo,
      update: updateOverview,
      updateTraffic
    };
  }
});

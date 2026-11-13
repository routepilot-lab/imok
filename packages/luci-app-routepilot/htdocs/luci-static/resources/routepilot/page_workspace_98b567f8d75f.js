'use strict';
'require baseclass';

// The workspace owns native LuCI form state and one status poll across settings pages.
var activeRender = 0;

return baseclass.extend({
  render: function (data, context) {
    var {
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
    } = context;

    if (data && data.runtimeView) {
      this.handleSaveApply = null;
      this.handleSave = null;
      return data.runtimeView.render(
        data.runtimeData,
        L.resource(imokStylesheet),
        boundRequest,
        pageShared.when
      );
    }
    if (data && data.clientsView) {
      this.handleSaveApply = null;
      this.handleSave = null;
      return data.clientsView.render(
        data.clientsData,
        L.resource(imokStylesheet),
        boundRequest,
        pageShared.when
      );
    }
    var pages = [
      ['account', _('账户与协议'), _('登录身份与主线路连接协议')],
      ['endpoint', _('主线路入口'), _('入口选择、候选读取与定期同步')],
      ['routing', _('出口与分流'), _('直接选择账户出口与接管范围')],
      ['devices', _('设备'), _('独立设备策略')],
      ['dns', 'DNS', _('解析、缓存与候选测试')],
      ['workers', 'Workers', _('手动备用的部署、转发与检测')],
      ['overview', _('总览'), _('开关与当前运行状态')],
      ['rules-overview', _('更新概况'), _('更新状态、运行版本与订阅变化')],
      ['rules-sources', _('来源与频率'), _('选择或编辑订阅地址，设置自动更新间隔')],
      ['rules-manage', _('规则管理'), _('浏览规则，维护长期保留的个人新增和排除')],
      ['rules-custom', _('自定义分流'), _('独立维护始终直连和始终代理的规则')],
      ['backup', _('备份迁移'), _('导入或导出本插件配置')]
    ];
    var leaf = routeLeaf(),
      page = pages.some(function (p) {
        return p[0] === leaf;
      })
        ? leaf
        : 'overview';
    var ruleSection = ruleSections[page],
      ruleDataPage = !!ruleSection && ruleSection !== 'custom';
    var pageInfo = pages.filter(function (p) {
        return p[0] === page;
      })[0],
      base = L.url('admin/services/routepilot'),
      renderToken = ++activeRender;
    function isCurrentView() {
      return renderToken === activeRender && routeLeaf() === page;
    }
    var pageGroup = sectionGroups[page],
      center = !!pageGroup,
      centerDeviceNode = null,
      deviceLeaseHint = null,
      centerFields = { basic: [], dns: [] },
      warpState = data[6] || null,
      exitObservation = null,
      saveInFlight = null,
      organizationOption = null,
      centerComposed = null,
      buildInfo = data[7] || {};
    var formPage = [
      'account',
      'routing',
      'devices',
      'dns',
      'rules-sources',
      'rules-custom'
    ].includes(page);
    if (!formPage) {
      this.handleSaveApply = null;
      this.handleSave = null;
    }
    var m = (this.map = new form.Map('routepilot', '', ''));
    var sourcePickers = {};
    var s,
      o,
      self = this,
      hints = data[1],
      catalog = data[3] || { available: false, networks: [] };
    var panel = pageShared.panel,
      helpDetails = pageShared.helpDetails,
      bytes = pageShared.bytes,
      when = pageShared.when;
    function command(action, requestId) {
      return pageShared.command(fs, action, requestId);
    }
    function finishPage(root) {
      if (
        !formPage ||
        !root ||
        root.nodeType !== 1 ||
        typeof requestAnimationFrame !== 'function' ||
        typeof MutationObserver !== 'function'
      )
        return root;
      requestAnimationFrame(function () {
        if (!root.isConnected) return;
        var host = root.parentElement,
          owned = null;
        function mark() {
          if (!root.isConnected || !isCurrentView()) {
            if (owned && owned._imokRoot === root) {
              owned.classList.remove('imok-page-actions');
              delete owned._imokRoot;
            }
            observer.disconnect();
            return;
          }
          var actions = host.querySelector('.cbi-page-actions');
          if (actions) {
            actions.classList.add('imok-page-actions');
            actions._imokRoot = root;
            owned = actions;
          }
        }
        var observer = new MutationObserver(mark);
        observer.observe(host, { childList: true });
        mark();
      });
      return root;
    }
    function showImokModal(title, children) {
      return ui.showModal(title, children, 'imok-dialog');
    }
    function notify(msg, err) {
      ui.addNotification(
        null,
        E('p', { class: 'imok-notice', 'data-tone': err ? 'error' : 'info' }, msg),
        err ? 'error' : 'info'
      );
    }
    function guardSelections(main, policies) {
      var selected = new Set();
      if ((main.scope || 'devices') !== 'devices') return selected;
      policies.forEach(function (policy) {
        if (policy.enabled === '1' && policy.mode !== 'direct' && policy.egress_guard === '1')
          selected.add(policy['.name'] + ':' + String(policy.mac || '').toLowerCase());
      });
      return selected;
    }
    function checkGuardBeforeSave(mainEdits, id, policyEdits) {
      var main = uci.get('routepilot', 'main') || {},
        policies = uci.sections('routepilot', 'policy');
      var current = guardSelections(main, policies);
      var saved = policies.find(function (row) {
        return row['.name'] === id;
      });
      var draftPolicies = policies.map(function (row) {
        return row['.name'] === id ? Object.assign({}, row, policyEdits) : row;
      });
      if (id && !saved) draftPolicies.push(Object.assign({ '.name': id }, policyEdits));
      var draft = guardSelections(Object.assign({}, main, mainEdits), draftPolicies);
      var optingIn =
        policyEdits && policyEdits.egress_guard === '1' && (!saved || saved.egress_guard !== '1');
      var removalOnly =
        draft.size < current.size &&
        Array.from(draft).every(function (key) {
          return current.has(key);
        });
      return optingIn || (draft.size && !removalOnly)
        ? command('guard-preflight')
        : Promise.resolve();
    }
    function guardDraft(options, id, keys) {
      var draft = {};
      keys.forEach(function (key) {
        var option = options.find(function (field) {
          return field.option === key;
        });
        var value = option && option.formvalue(id);
        if (value != null) draft[key] = value;
      });
      return draft;
    }
    function checkMapBeforeSave(map, check, onError) {
      var save = map.save;
      map.save = function () {
        var args = arguments;
        return Promise.resolve()
          .then(check)
          .catch(function (error) {
            if (onError) onError(error.message);
            throw error;
          })
          .then(function () {
            return save.apply(map, args);
          });
      };
    }
    function submitInput(name, action, payload) {
      var request = boundRequest(payload);
      return fs
        .write(
          '/var/run/routepilot/requests/' + name + '-' + request.id + '.json',
          request.text,
          384
        )
        .then(function () {
          return command(action, request.token);
        })
        .then(function (raw) {
          var result = JSON.parse(raw);
          if (result.request_id !== request.id)
            throw Error(_('未收到本次请求确认，请读取当前状态后重试'));
          return result;
        });
    }
    function save() {
      return (center ? saveOrganizationOnly() : self.applySettings()).then(function (ok) {
        if (ok === false) throw Error(_('配置未保存，后续操作未执行'));
        return ok;
      });
    }
    var wizardActions = {},
      wizardCards = {},
      wizard = { active: null, pinned: false, initialized: false, advance: null };
    var wizardStatus = null,
      wizardTest = null,
      exitOption = null,
      updateWizard = function () {};
    var committedOrganization = uci.get('routepilot', 'main', 'organization') || '';
    function wizardConfig() {
      return {
        organization: committedOrganization,
        exit: configuredExit(),
        exit_id: savedExitID(configuredExit()),
        nonce: switchNonce || uci.get('routepilot', 'main', 'apply_nonce'),
        backend: uci.get('routepilot', 'main', 'backend') || 'masque',
        scope: uci.get('routepilot', 'main', 'scope'),
        lans: (
          uci.get('routepilot', 'main', 'lan_interfaces') ||
          uci.get('routepilot', 'main', 'lan_interface') ||
          ''
        ).length,
        devices: uci.sections('routepilot', 'policy').filter(function (p) {
          return p.enabled === '1' && p.mode !== 'direct';
        }).length
      };
    }
    function openWizardStep(id) {
      if (!wizardCards[id]) return;
      Object.keys(wizardCards).forEach(function (key) {
        wizardCards[key].setAttribute('data-current', key === id ? 'true' : 'false');
      });
      wizard.active = id;
    }
    function beginWizardStep(id) {
      wizard.pinned = false;
      wizard.active = id;
      wizard.advance = id;
      wizardActions[id] = {
        state: 'busy',
        started: Date.now(),
        baseline: id === 'verify' ? feedback.wizardTestToken(wizardTest) : null
      };
      updateWizard();
    }
    function failWizardStep(id) {
      var previous = wizardActions[id];
      wizardActions[id] = {
        state: 'error',
        started: Date.now(),
        baseline: previous && previous.baseline
      };
      updateWizard();
    }

    var directoryStatus = E('p', { role: 'status', 'aria-live': 'polite' }),
      directoryRows = E('tbody'),
      directoryPending = null,
      directoryGeneration = 0,
      registeredHere = false,
      lastDirectoryRows = null,
      comparisonResult = null,
      comparisonStarting = false;
    function comparisonBusy() {
      return (
        comparisonStarting ||
        (comparisonResult && ['queued', 'testing', 'restoring'].includes(comparisonResult.state))
      );
    }
    function comparisonReady() {
      return (
        feedback.fresh(wizardStatus, Date.now()) &&
        wizardStatus.state === 'running' &&
        wizardStatus.backend === 'masque' &&
        !comparisonBusy()
      );
    }
    function startComparison(kind, targets) {
      if (!comparisonReady() || !targets.length || targets.length > 16) return;
      var maximum = Math.min(1000, 200 * targets.length);
      var seconds = Math.min(300, 60 * targets.length);
      showImokModal(_('确认线路对比测速'), [
        E(
          'p',
          {},
          _(
            '测速期间暂停受管设备的代理流量，受保护设备保持断网。测速最长 %s 秒，随后恢复已保存配置。'
          ).replace('%s', String(seconds))
        ),
        E(
          'p',
          {},
          _('最多下载 %s MB。').replace('%s', String(maximum)) +
            (targets.length > 5 ? ' ' + _('已降低每个出口的流量预算。') : '')
        ),
        E('div', { class: 'rp-actions' }, [
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
                comparisonStarting = true;
                refreshComparison();
                return submitInput('speed-compare-input', 'speed-compare', {
                  kind: kind,
                  targets: targets
                })
                  .then(function (response) {
                    comparisonResult = {
                      id: response.id,
                      kind: kind,
                      state: 'queued',
                      rows: [],
                      targets: targets
                    };
                  })
                  .catch(function (error) {
                    notify(error.message, true);
                  })
                  .finally(function () {
                    comparisonStarting = false;
                    refreshComparison();
                  });
              }
            },
            _('开始测速')
          )
        ])
      ]);
    }
    function comparisonCell(row) {
      if (!row) return '—';
      if (row.state !== 'done') return row.message || _('测量未完成');
      var rate = speed.rate(row);
      if (row.measurement_version === 2 && row.stop_reason === 'budget')
        rate += ' (' + _('已达测速上限') + ')';
      if (row.measurement_version === 2 && row.stop_reason === 'sample_limit')
        rate += ' (' + _('下载样本已用完') + ')';
      return [
        row.https_connect_median_ms != null
          ? _('HTTPS 建连中位数 ') + row.https_connect_median_ms + ' ms'
          : '',
        rate,
        row.region,
        row.exit_ip,
        speed.cpu(row),
        row.measured_at && when(row.measured_at),
        row.batch_state === 'config_changed' ? _('本批因设置变化中断') : ''
      ]
        .filter(Boolean)
        .join(' · ');
    }
    var comparisonStatus = E('p', { role: 'status', 'aria-live': 'polite' });
    function stopComparison() {
      return command('speed-compare-cancel').catch(function (error) {
        notify(error.message, true);
      });
    }
    var protocolComparison =
      page === 'account'
        ? pageAccount.protocolComparison({
            feedback,
            startComparison,
            stopComparison,
            comparisonBusy,
            comparisonCell
          })
        : null;
    function refreshComparison() {
      renderDirectory();
      if (protocolComparison)
        protocolComparison.update(wizardStatus, comparisonResult, comparisonStatus.textContent);
    }
    var { allExits, cancelExits } = pageRouting.comparisonActions(
      function () {
        return catalog.networks;
      },
      startComparison,
      stopComparison
    );
    function renderDirectory() {
      var age = Date.now() - Number(catalog.read_at),
        recent = catalog.available && age >= -5000 && age <= 60000;
      directoryStatus.textContent = directoryPending
        ? _('正在读取账户出口，请稍候（客户端查询最长约 8 秒）…')
        : catalog.available
          ? (!recent ? _('上次读取时间已失效，请刷新确认；') : '') +
            _('已读取 ') +
            catalog.networks.length +
            _(' 个账户网络；未切换出口，也未验证连通性。')
          : !catalog.read_at
            ? _('尚未读取账户目录；可点击下方刷新按钮。')
            : registeredHere
              ? _('已注册，目录暂未就绪，请稍后刷新；当前出口与配置未改动。')
              : _('读取失败，请重试；当前出口与配置未改动。若持续失败，请检查登录环境。');
      var targets = catalog.networks.filter(function (n) {
        return !n.is_default;
      });
      allExits.disabled = !comparisonReady() || !targets.length || targets.length > 16;
      cancelExits.hidden =
        !comparisonResult || !['queued', 'testing'].includes(comparisonResult.state);
      var comparisonStates = {
        queued: _('等待主线路开始测速'),
        testing: _('线路对比测速中'),
        restoring: _('正在恢复已保存的设置'),
        done: _('测速已完成，已恢复保存的设置'),
        config_changed: _('设置已变化，已按当前设置恢复'),
        cancelled: _('已取消，已恢复保存的设置'),
        timeout: _('测速超时'),
        interrupted: _('服务重启，测速已作废'),
        error: _('测速或恢复未完成，请查看总览')
      };
      comparisonStatus.textContent = comparisonStarting
        ? _('正在提交测速')
        : comparisonResult
          ? [
              comparisonStates[comparisonResult.state],
              comparisonResult.index
                ? comparisonResult.index + '/' + comparisonResult.targets.length
                : '',
              comparisonResult.finished_at ? when(comparisonResult.finished_at) : ''
            ]
              .filter(Boolean)
              .join(' · ')
          : targets.length > 16
            ? _('目录超过 16 个出口，请选择单个测试。')
            : !comparisonReady()
              ? _('出口测速需要 WARP 主线路已应用并运行。')
              : '';
      var renderKey = [
        catalog.read_at,
        catalog.available,
        recent,
        wizardStatus && wizardStatus.state,
        wizardStatus && wizardStatus.runtime_id,
        feedback.wizardTestToken(wizardTest),
        comparisonReady(),
        JSON.stringify(comparisonResult)
      ].join('|');
      if (renderKey === lastDirectoryRows) {
        updateWizard();
        return;
      }
      lastDirectoryRows = renderKey;
      directoryRows.replaceChildren.apply(
        directoryRows,
        pageRouting.directoryRows({
          catalog,
          comparisonResult,
          exitHealth,
          comparisonCell,
          comparisonReady,
          startComparison
        })
      );
      updateWizard();
    }
    function refreshDirectory(force) {
      if (force) {
        directoryGeneration++;
        catalog = { available: false, networks: [], last_success_at: catalog.last_success_at };
      }
      if (directoryPending)
        return force
          ? directoryPending.then(function () {
              return refreshDirectory();
            })
          : directoryPending;
      directoryStatus.textContent = _('正在读取账户出口，请稍候（客户端查询最长约 8 秒）…');
      var generation = directoryGeneration;
      directoryPending = Promise.resolve()
        .then(readExitDirectory)
        .catch(function () {
          return { available: false, networks: [], read_at: Date.now() };
        })
        .then(function (next) {
          if (!isCurrentView() || generation !== directoryGeneration) return next;
          next.last_success_at = next.available ? next.read_at : catalog.last_success_at;
          catalog = next;
          return updateExitChoices().then(function () {
            return next;
          });
        })
        .finally(function () {
          directoryPending = null;
          renderDirectory();
        });
      updateWizard();
      return directoryPending;
    }
    function updateExitChoices() {
      if (!isCurrentView() || page !== 'routing' || !exitOption) return Promise.resolve();
      var frame = document.getElementById('cbi-routepilot-main-exit'),
        field = frame && frame.querySelector('.cbi-value-field');
      if (!field) return Promise.resolve();
      var current = exitOption.formvalue('main');
      if (current == null) current = configuredExit();
      exitOption.keylist = [];
      exitOption.vallist = [];
      self
        .exitOptions(
          current,
          {
            default: uci.get('routepilot', 'main', 'vnet_default'),
            seoul: uci.get('routepilot', 'main', 'vnet_seoul'),
            usa: uci.get('routepilot', 'main', 'vnet_usa')
          },
          catalog
        )
        .forEach(function (item) {
          exitOption.value(item.value, item.label);
        });
      var description = field.querySelector('.cbi-value-description');
      return Promise.resolve(exitOption.renderWidget('main', 0, current)).then(function (widget) {
        if (description) field.replaceChildren(widget, description);
        else field.replaceChildren(widget);
        if (typeof exitOption.triggerValidation === 'function')
          exitOption.triggerValidation('main');
      });
    }
    function exitHealth(id) {
      var action = wizardActions.verify;
      if (action && action.state === 'error') return _('本次检测未完成，请重试');
      if (
        action &&
        action.state === 'busy' &&
        action.baseline === feedback.wizardTestToken(wizardTest)
      )
        return _('正在请求检测，等待新结果');
      var result = feedback.wizardProxyVerification(
        wizardTest,
        wizardStatus,
        wizardConfig(),
        Date.now()
      );
      if (
        !wizardStatus ||
        wizardStatus.exit !== configuredExit() ||
        id !== savedExitID(wizardStatus.exit)
      )
        return _('未检测');
      return (
        {
          success: _('✓ 当前连接检测通过'),
          error: _('✕ 当前连接检测失败'),
          warning: _('! 当前连接访问受限'),
          busy: _('… 当前连接检测中')
        }[result] || _('未检测 / 旧结果已过期')
      );
    }
    if (page === 'account' || page === 'routing') refreshComparison();
    s = m.section(form.NamedSection, 'main', 'main', pageInfo[1]);
    var mainSection = s;
    checkMapBeforeSave(m, function () {
      var draft = page === 'routing' ? guardDraft(centerFields.basic, 'main', ['scope']) : null;
      return checkGuardBeforeSave(draft).then(function () {
        if (page === 'dns') return dns.checkPreferences();
      });
    });
    function pageOption(tab) {
      var option = s.option.apply(s, Array.prototype.slice.call(arguments, 1));
      option.routepilotPage = tab;
      if (center) option.retain = true;
      if (centerFields[tab] && !String(option.option || '').startsWith('warp_dns_'))
        centerFields[tab].push(option);
      return option;
    }
    function sourcePresetField(option, kind) {
      var render = option.renderWidget.bind(option);
      option.renderWidget = function (section, index, value) {
        return Promise.resolve(render(section, index, value)).then(function (widget) {
          var select = E(
              'select',
              {
                class: 'cbi-input-select',
                'aria-label':
                  {
                    china: '中国 IP',
                    china_domains: '中国域名',
                    gfw: '代理域名',
                    whitelist: '直连域名'
                  }[kind] + _('订阅预设')
              },
              [E('option', { value: 'custom' }, _('自定义地址 / 保留当前地址'))]
            ),
            downloaded = E('p', { class: 'rp-help' }, _('正在读取已下载版本的来源…'));
          var saved = E(
              'p',
              { class: 'rp-help' },
              _('当前已保存地址：') + String(value || _('未设置'))
            ),
            picker = (sourcePickers[kind] = {
              option: option,
              widget: widget,
              select: select,
              downloaded: downloaded,
              saved: saved,
              initial: value,
              presets: []
            });
          select.value = 'custom';
          select.addEventListener('change', function () {
            picker.touched = true;
            picker.manual = select.value === 'custom';
            select.setAttribute('data-selected', picker.manual ? 'custom' : 'preset');
            var preset = picker.presets.filter(function (p) {
              return p.id === select.value;
            })[0];
            if (!preset) return;
            var element =
              typeof option.getUIElement === 'function' ? option.getUIElement(section) : null;
            if (element && typeof element.setValue === 'function') element.setValue(preset.url);
            else widget.value = preset.url;
            if (typeof option.triggerValidation === 'function') option.triggerValidation(section);
          });
          widget.addEventListener('input', function () {
            picker.touched = true;
            picker.manual = true;
            select.value = 'custom';
            select.setAttribute('data-selected', 'custom');
          });
          rules.refreshSourcePickers();
          return E('div', { class: 'rp-rule-source-field' }, [
            E('label', { class: 'rp-cloud-field' }, [E('span', {}, _('可选来源')), select]),
            widget,
            saved,
            downloaded
          ]);
        });
      };
    }
    // The top-level switch commits only its own power state, never unsaved form edits.
    organizationOption = pageAccount.configure({
      pageOption,
      form,
      uci,
      ui,
      fs,
      feedback,
      command,
      notify,
      save,
      submitInput,
      beginWizardStep,
      failWizardStep,
      openWizardStep,
      refreshDirectory,
      showImokModal,
      wizardActions,
      getCatalog: function () {
        return catalog;
      },
      invalidateDirectory: function () {
        directoryGeneration++;
      },
      refreshWizard: function () {
        updateWizard();
      },
      markRegistered: function () {
        registeredHere = true;
      }
    });
    exitOption = pageRouting.configure({
      pageOption,
      form,
      uci,
      data,
      catalog,
      configuredExit,
      exitOptions: self.exitOptions.bind(self),
      refreshDirectory,
      directoryStatus,
      allExits,
      cancelExits,
      comparisonStatus,
      directoryRows
    });
    var dnsFields = pageDns.configure({ pageOption, form, center });
    var dns = pageDns.create({
      ...dnsFields,
      command,
      feedback,
      notify,
      showImokModal,
      submitInput,
      uci,
      ui,
      when,
      getStatus: function () {
        return wizardStatus;
      }
    });
    var speed = pageDns.createSpeed({ bytes, command, feedback, showImokModal, ui, when });
    [
      ['custom_direct_domains', _('始终直连域名'), 'hostname'],
      ['custom_proxy_domains', _('始终代理域名'), 'hostname'],
      ['custom_direct_ips', _('始终直连 IPv4/CIDR'), 'or(ip4addr,cidr4)'],
      ['custom_proxy_ips', _('始终代理 IPv4/CIDR'), 'or(ip4addr,cidr4)']
    ].forEach(function (v) {
      o = pageOption('rules-custom', form.DynamicList, v[0], v[1]);
      o.datatype = v[2];
    });
    [
      ['china_url', _('中国 IP 规则')],
      ['china_domains_url', _('中国域名规则')],
      ['gfw_url', _('代理域名名单')],
      ['whitelist_url', _('直连域名名单')]
    ].forEach(function (v) {
      o = pageOption(
        'rules-sources',
        form.Value,
        v[0],
        v[1],
        _(
          '可信 HTTPS 名单；支持普通域名、domain:、full:。regexp 条目跳过并记录，不执行脚本或编码订阅。'
        )
      );
      o.validate = function (id, v) {
        return !v || /^https:\/\/[^\s]+$/.test(v) ? true : _('必须使用 HTTPS');
      };
      sourcePresetField(o, v[0].replace('_url', ''));
    });
    o = pageOption('rules-sources', form.Value, 'update_hours', _('自动更新间隔（小时）'));
    o.datatype = 'range(1,720)';
    o.default = '168';
    var policySection = pageDevices.configure({
      m,
      form,
      uci,
      hints,
      ui,
      checkMapBeforeSave,
      checkGuardBeforeSave,
      guardDraft
    });
    if (page === 'account') {
      var originalSectionRender = mainSection.render.bind(mainSection);
      mainSection.render = function () {
        return originalSectionRender.apply(null, arguments).then(function (node) {
          var guide = pageAccount.setupGuide(node, {
            base,
            document,
            uci,
            feedback,
            pageShared,
            wizard,
            wizardActions,
            wizardCards,
            wizardConfig,
            openWizardStep,
            beginWizardStep,
            failWizardStep,
            notify,
            save,
            refreshDirectory,
            readState: function () {
              return {
                catalog,
                directoryPending,
                registeredHere,
                committedOrganization,
                wizardStatus,
                wizardTest
              };
            }
          });
          updateWizard = guide.update;
          updateWizard();
          node.appendChild(guide.element);
          return node;
        });
      };
    }
    mainSection.children = mainSection.children.filter(function (o) {
      return center ? o.routepilotPage === pageGroup : o.routepilotPage === page;
    });
    if (page === 'devices') {
      var renderDevices = policySection.render.bind(policySection);
      policySection.render = function () {
        return renderDevices.apply(null, arguments).then(function (node) {
          centerDeviceNode = node;
          return node;
        });
      };
    }
    m.children = page === 'devices' ? [policySection] : formPage ? [mainSection] : [];
    var status = E('p', { role: 'status', 'aria-live': 'polite' }, _('正在读取实际运行状态…'));
    var rulesProgress = E('p', {}, _('正在读取规则状态…')),
      rulesTime = E('p'),
      rulesCounts = E('div'),
      lastRule = null;
    var testsInfo = E('p', {}, _('从路由器检测代理和直连网站。'));
    var testCells = {},
      testNames = {
        google: 'Google',
        youtube: 'YouTube',
        github: 'GitHub',
        taobao: '淘宝',
        baidu: '百度'
      };
    var testRows = Object.keys(testNames).map(function (k) {
      var result = E('td', {}, _('未检测')),
        delay = E('td', {}, '—');
      testCells[k] = { result: result, delay: delay };
      return E('tr', {}, [
        E('td', {}, testNames[k]),
        E('td', {}, k === 'taobao' || k === 'baidu' ? _('国内直连') : _('当前代理出口')),
        result,
        delay
      ]);
    });
    var testButton = E(
      'button',
      {
        class: 'btn',
        click: function () {
          if (
            center &&
            (!feedback.fresh(wizardStatus, Date.now()) || wizardStatus.state !== 'running')
          ) {
            notify(_('请先在总览手动开启，并等待主线路已连接后再检测。'), true);
            return;
          }
          testButton.disabled = true;
          testsInfo.textContent = _('正在检测，最长约 10 秒…');
          Object.keys(testCells).forEach(function (k) {
            testCells[k].result.textContent = _('检测中…');
            testCells[k].delay.textContent = '—';
          });
          command('test').catch(function (e) {
            testsInfo.textContent = e.message;
            testButton.disabled = false;
          });
        }
      },
      _('一键检测全部')
    );
    var switchEnabled = uci.get('routepilot', 'main', 'enabled') === '1',
      switchNonce = null,
      switchPending = null;
    var switchText = E(
      'p',
      {},
      _('新安装默认关闭。开关立即生效，无需点击页面底部保存；不会提交尚未保存的其他设置。')
    );
    var switchFeedback = E(
      'div',
      { hidden: true, class: 'imok-switch-feedback', role: 'status', 'aria-live': 'polite' },
      switchText
    );
    var powerButton = E(
      'button',
      {
        id: 'imok-power',
        class: 'btn cbi-button-action',
        click: function () {
          switchFeedback.hidden = false;
          var action = switchEnabled ? 'disable' : 'enable';
          powerButton.disabled = true;
          switchText.textContent = _('正在切换，等待后台确认…');
          command(action)
            .then(function (msg) {
              var result = JSON.parse(msg);
              if (!result || result.enabled !== (action === 'enable') || !result.nonce)
                throw Error(_('尚未收到切换确认，请查看实际运行状态。'));
              switchEnabled = result.enabled;
              uci.set('routepilot', 'main', 'enabled', switchEnabled ? '1' : '0');
              uci.set('routepilot', 'main', 'preparing', '0');
              switchNonce = result.nonce;
              switchPending = { nonce: switchNonce, started: Date.now() };
              powerButton.textContent = switchEnabled ? _('关闭 imOK') : _('开启 imOK');
            })
            .catch(function (e) {
              switchText.textContent = e.message;
              powerButton.disabled = false;
            });
        }
      },
      switchEnabled ? _('关闭 imOK') : _('开启 imOK')
    );
    var workers = pageWorkers.create({
      command,
      data,
      document,
      feedback,
      fs,
      helpDetails,
      notify,
      submitInput,
      uci,
      when,
      getStatus: function () {
        return wizardStatus;
      }
    });
    var connectionRows = E('div'),
      connectionDirectory = E('div'),
      connectionHistory = E('details', { class: 'rp-stat-details' }, [
        E('summary', {}, _('连接记录与开关说明')),
        connectionRows,
        connectionDirectory,
        E('p', { class: 'rp-help' }, _('开关立即生效，无需保存；不会提交尚未保存的其他设置。'))
      ]);
    var protectionRows = E('div', {}, E('p', { class: 'rp-help' }, _('本次启动尚无断网保护事件')));
    var protectionHistory = E('details', { class: 'rp-stat-details' }, [
      E('summary', {}, _('断网保护记录')),
      protectionRows,
      E('p', { class: 'rp-help' }, _('保留本次启动最近 40 条状态变化，路由器重启后重新记录。'))
    ]);
    var historyKeys = {};
    var protectionStates = {
      blocking: _('已阻断外网'),
      ready: _('已恢复放行'),
      off: _('保护已关闭'),
      no_targets: _('没有受保护设备'),
      unverified: _('保护未确认')
    };
    var protectionReasons = {
      startup: _('等待出口和 DNS 就绪'),
      settings: _('接管范围已更新，等待出口和 DNS 就绪'),
      firewall_restored: _('防火墙保护规则已重建'),
      capture_stopped: _('分流接管已停止'),
      reconfigure: _('正在应用分流配置'),
      reconnect: _('线路正在重连或重新应用入口'),
      capture_unavailable: _('本地分流暂不可用，正在恢复'),
      dns_not_ready: _('尚未确认接管 DNS，设备保持阻断'),
      disabled: _('imOK 已关闭，断网保护已撤回'),
      verified: _('出口、转发和 DNS 已确认'),
      no_targets: _('请检查代理设备及其断网保护设置'),
      opt_out: _('断网保护已取消'),
      invalid_config: _('请检查保护范围和设备设置'),
      boot_hook: _('路由器的开机保护入口未启用'),
      flow_offload: _('检测到防火墙流量卸载'),
      firewall_error: _('无法确认防火墙规则')
    };
    function renderHistory(key, node, lines) {
      var next = JSON.stringify(lines);
      if (next === historyKeys[key]) return;
      node.replaceChildren(
        ...lines.map(function (line) {
          return E('p', { class: 'rp-help' }, line);
        })
      );
      historyKeys[key] = next;
    }
    function updateConnectionHistory(data) {
      var rows =
        data && Array.isArray(data.events)
          ? data.events
              .filter(function (row) {
                return row && typeof row === 'object';
              })
              .slice(-6)
              .reverse()
          : [];
      var lines = rows.map(function (row) {
        var label =
          row.state === 'running'
            ? _('连接已恢复 / 已运行')
            : row.recovering
              ? _('短暂中断，等待原客户端恢复')
              : row.state === 'error'
                ? _('连接异常')
                : row.state === 'disabled'
                  ? _('已关闭')
                  : row.state === 'waiting_devices'
                    ? _('等待设备 DHCP 租约')
                    : _('状态变化');
        return (
          when(row.time) +
          ' · ' +
          label +
          (row.fallback === 'blocked'
            ? _(' · 断网保护已阻断外网')
            : row.fallback === 'direct'
              ? _(' · 已撤回接管，保持直连')
              : row.fallback === 'unknown'
                ? _(' · 网络路径未确认')
                : '')
        );
      });
      if (!lines.length) lines.push(_('尚无连接变化记录'));
      renderHistory('connection', connectionRows, lines);
      if (page !== 'overview') return;
      var records = data && Array.isArray(data.protection) ? data.protection : [];
      var protectionLines = records
        .slice(-40)
        .reverse()
        .flatMap(function (row) {
          if (
            !row ||
            !Number.isFinite(row.time) ||
            row.time <= 0 ||
            !Object.prototype.hasOwnProperty.call(protectionStates, row.state)
          )
            return [];
          var scope =
            row.scope === 'lan' ? _('所选局域网') : row.scope === 'devices' ? _('所选设备') : '';
          var reason = Object.prototype.hasOwnProperty.call(protectionReasons, row.reason)
            ? protectionReasons[row.reason]
            : '';
          return [
            [when(row.time), protectionStates[row.state], scope, reason].filter(Boolean).join(' · ')
          ];
        });
      if (!protectionLines.length) protectionLines.push(_('本次启动尚无断网保护事件'));
      renderHistory('protection', protectionRows, protectionLines);
    }
    var overview = pageOverview.create({
      base,
      buildInfo,
      bytes,
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
      getCatalog: function () {
        return catalog;
      },
      enabled: function () {
        return switchEnabled;
      },
      getStatus: function () {
        return wizardStatus;
      }
    });
    var rules = pageRules.create({
      base,
      command,
      isCurrentView,
      page,
      panel,
      ruleDataPage,
      ruleSection,
      ruleSectionNames,
      ruleSections,
      rulesCounts,
      rulesProgress,
      rulesTime,
      self,
      showImokModal,
      sourcePickers,
      submitInput,
      ui,
      when
    });
    var testCard = E('div', { class: 'imok-panel imok-web-tests' }, [
      E('h3', {}, _('网站连通性检测')),
      testsInfo,
      testButton,
      E(
        'table',
        { class: 'table' },
        [
          E('tr', { class: 'tr table-titles' }, [
            E('th', {}, _('网站')),
            E('th', {}, _('检测线路')),
            E('th', {}, _('结果')),
            E('th', {}, _('HTTPS 耗时'))
          ])
        ].concat(testRows)
      ),
      E(
        'p',
        { class: 'rp-help' },
        _('检测 HTTPS 能否建立连接；耗时不是 Ping，也不代表视频速度或网站登录一定可用。')
      )
    ]);
    var receiveState = E('p', { role: 'status', 'aria-live': 'polite' }, _('正在读取接收观测…')),
      receiveCounts = E('p'),
      receiveCoverage = E('p');
    var receiveCard = E('div', { class: 'cbi-section' }, [
      E('h3', {}, _('WARP 客户端 UDP 接收观测')),
      receiveState,
      receiveCounts,
      receiveCoverage,
      helpDetails(
        _(
          '约每 100 毫秒采样，每秒更新。队列表示内核接收内存，峰值取最近约 60 秒。新增丢弃从各连接建立观测基线后累计，包含已识别隧道和其他公网 UDP。'
        )
      )
    ]);
    function updateReceive(data, status) {
      if (status && status.backend === 'worker') {
        receiveState.textContent = _('当前使用 TCP 备用线路；此处的 WARP UDP 观测不适用。');
        receiveCounts.textContent = '';
        receiveCoverage.textContent = '';
        return;
      }
      var state = feedback.receiveState(data, status, Date.now()),
        names = {
          unavailable: _('接收观测不可用，不能确认队列或丢包情况。'),
          stale: _('接收观测已过期或连接已改变，旧数据不能代表当前网络。'),
          inactive: _('当前没有运行中的隧道观测。'),
          no_sockets: _('暂未观测到客户端公网 UDP 连接，不能据此认定没有丢包。'),
          observing: _('正在进行只读接收观测。'),
          partial: _('正在观测，但存在漏采或计数重置，数据不完整。')
        };
      receiveState.textContent = names[state];
      receiveCounts.textContent = '';
      receiveCoverage.textContent = '';
      if (state !== 'observing' && state !== 'partial') return;
      receiveCounts.textContent =
        _('当前接收积压：') +
        (data.queue_now / 1048576).toFixed(2) +
        ' MiB · ' +
        _('最近采样峰值：') +
        (data.peak_queue / 1048576).toFixed(2) +
        ' MiB · ' +
        _('本次观测新增丢弃：') +
        data.observed_drops;
      receiveCoverage.textContent =
        _('连接数：') +
        data.socket_count +
        ' · ' +
        _('已识别隧道：') +
        data.known_ingress_count +
        ' · ' +
        _('其他公网 UDP：') +
        data.other_public_udp_count +
        ' · ' +
        _('观测开始：') +
        when(data.started_at) +
        ' · ' +
        _('最近更新：') +
        when(data.time);
    }
    var pendingApply = null,
      pendingUpdate = null;
    function showCounts(parent, counts) {
      parent.replaceChildren();
      var names = {
        china: _('中国 IP'),
        china_domains: _('中国域名'),
        gfw: _('代理域名'),
        whitelist: _('直连域名')
      };
      Object.keys(names).forEach(function (k) {
        parent.appendChild(
          E(
            'span',
            { style: 'display:inline-block;margin-right:18px' },
            names[k] +
              '：' +
              (counts && counts[k] != null ? Number(counts[k]).toLocaleString() : '—')
          )
        );
      });
    }
    function updateFeedback(v) {
      if (v.state === 'running' && !feedback.updateRunning(v, Date.now()))
        v = Object.assign({}, v, {
          state: 'unknown',
          phase: _('更新状态已过期，结果未知；可重新更新')
        });
      lastRule = v;
      rulesProgress.textContent =
        (v.state === 'running' ? _('更新中 · ') + String(v.percent || 0) + '% · ' : '') +
        String(v.phase || _('尚未更新')) +
        (v.state === 'blocked' ? ' · ' + _('候选尚未应用，等待确认') : '');
      rulesProgress.style.color =
        v.state === 'failed' ? '#b42318' : v.state === 'success' ? '#087443' : '#2254d1';
      rulesTime.textContent = _('最后成功更新：') + when(v.last_success_at);
      showCounts(rulesCounts, v.counts);
      if (pendingUpdate && v.request_id === pendingUpdate.nonce) {
        pendingUpdate.box.replaceChildren(
          E('p', {}, rulesProgress.textContent),
          E('p', {}, rulesTime.textContent)
        );
        var counts = E('div');
        showCounts(counts, v.counts);
        pendingUpdate.box.appendChild(counts);
        if (['success', 'failed', 'blocked', 'unknown'].indexOf(v.state) >= 0) pendingUpdate = null;
      }
    }
    function submitSettings(update) {
      if (saveInFlight) return saveInFlight;
      if (
        update &&
        uci.get('routepilot', 'main', 'enabled') !== '1' &&
        uci.get('routepilot', 'main', 'preparing') !== '1'
      ) {
        notify(_('规则更新需要主线路已开启或登录环境已就绪；当前仅可浏览已有规则。'), true);
        return Promise.resolve(false);
      }
      if (update && feedback.updateRunning(lastRule, Date.now())) {
        var b = E('div', {}, lastRule.phase);
        pendingUpdate = { nonce: lastRule.request_id, box: b };
        showImokModal(_('规则正在更新'), [
          b,
          E('button', { class: 'btn', click: ui.hideModal }, _('关闭'))
        ]);
        return Promise.resolve();
      }
      var nonce = String(Date.now()),
        box = E('div', {}, E('p', {}, _('正在检查本插件配置…')));
      function setNonce() {
        uci.set('routepilot', 'main', update ? 'update_nonce' : 'apply_nonce', nonce);
      }
      pendingApply = update ? null : { nonce: nonce, box: box, started: Date.now(), update: false };
      if (update) pendingUpdate = { nonce: nonce, box: box };
      showImokModal(update ? _('规则更新进度') : _('应用配置'), [
        box,
        E(
          'button',
          {
            class: 'btn',
            click: function () {
              ui.hideModal();
            }
          },
          _('关闭')
        )
      ]);
      saveInFlight = Promise.resolve()
        .then(function () {
          return update ? checkGuardBeforeSave().then(setNonce) : m.save(setNonce, true);
        })
        .then(function () {
          return uci.save();
        })
        .then(function () {
          return commitConfig('routepilot');
        })
        .then(function () {
          committedOrganization = uci.get('routepilot', 'main', 'organization') || '';
          delete wizardActions.org;
          updateWizard();
          if (!update && ruleSection === 'sources') {
            Object.keys(sourcePickers).forEach(function (kind) {
              sourcePickers[kind].saved.textContent =
                _('当前已保存地址：') +
                String(uci.get('routepilot', 'main', kind + '_url') || _('未设置'));
            });
            rules.reload();
          }
          box.textContent = update
            ? _('请求已提交，等待后台开始更新…')
            : _('配置已提交，正在等待实际应用结果…');
          if (!update && page === 'dns') dns.clearDraft();
          return { nonce: nonce };
        })
        .catch(function (e) {
          box.textContent = [
            update ? _('规则更新请求未确认。') : _('配置提交未确认。'),
            e.message,
            _('请核对当前状态，再重试。')
          ].join(' ');
          pendingApply = null;
          pendingUpdate = null;
          if (center && wizardActions.org) failWizardStep('org');
          return false;
        })
        .finally(function () {
          saveInFlight = null;
        });
      return saveInFlight;
    }
    self.applySettings = function () {
      return submitSettings(false);
    };
    self.requestRulesUpdate = function () {
      return submitSettings(true);
    };
    var runtimeFiles = [
      'status.json',
      'update.json',
      'connectivity.json',
      'receive.json',
      'dns-state.json',
      'dns-tests.json',
      'backup-node.json',
      'backup-test.json',
      'cloud-state.json',
      'backup-scan.json',
      'relay-tests.json',
      'relay-observed.json',
      'warp-state.json',
      'exit-observation.json',
      'traffic.json',
      'connection-events.json',
      'cloud-directory-state.json',
      'warp-dns-tests.json',
      'speed-tests.json',
      'lan-dns-domestic.json',
      'lan-dns-foreign.json',
      'speed-compare.json',
      'endpoint-quality.json'
    ];
    var runtimeSlots =
      {
        overview: [0, 2, 3, 14, 15, 18],
        account: [0, 12, 13, 21],
        endpoint: [0, 13, 22],
        routing: [0, 2, 13, 21],
        devices: [0],
        dns: [0, 4, 5, 17, 19, 20],
        workers: [0, 6, 7, 8, 9, 10, 11, 13, 15, 16],
        'rules-overview': [0, 1],
        'rules-sources': [0],
        'rules-manage': [0],
        'rules-custom': [0],
        backup: []
      }[page] || [];
    function applyRuntimeFiles(files) {
      if (!isCurrentView()) return;
      if (!feedback.diagnosticValid(files[2])) files[2] = null;
      if (!feedback.dnsTestValid(files[5])) files[5] = null;
      var hasDirectory = page === 'workers';
      if (files[15]) updateConnectionHistory(files[15]);
      if (hasDirectory) workers.updateDirectory(files[16]);
      wizardStatus = files[0];
      wizardTest = files[2];
      if (
        (page === 'account' || page === 'routing') &&
        files[21] &&
        files[21].id &&
        Array.isArray(files[21].targets)
      )
        comparisonResult = files[21];
      if (page === 'workers') {
        workers.update(files[6], files[7], files[0], files[8], files[9], files[10], files[11]);
      }
      if (page === 'endpoint') endpoint.updateQuality(files[0], files[22]);
      if (page === 'dns') {
        dns.update(files[0], files[4], files[5], files[17], files[19], files[20]);
      }
      if (center) {
        warpState = files[12] || warpState;
        exitObservation = files[13] || exitObservation;
        updateCenter();
      }
      if (page === 'overview') {
        overview.update(files[0], files[2]);
        overview.updateTraffic(files[14]);
        updateReceive(files[3], files[0]);
        speed.update(files[0], files[18]);
      }
      if (page === 'devices' && deviceLeaseHint) {
        var deviceStatus = files[0],
          currentDeviceStatus = feedback.fresh(deviceStatus, Date.now());
        var leaseMessage = currentDeviceStatus
          ? deviceStatus.device_wait_message ||
            (deviceStatus.state === 'waiting_devices' ? deviceStatus.message : '')
          : '';
        deviceLeaseHint.textContent = leaseMessage;
        deviceLeaseHint.hidden = !leaseMessage;
      }
      if (page === 'workers') {
        workers.updateProgress();
      }
      if (hasDirectory) workers.refreshDirectoryIfNeeded();
      var v = files[0],
        fresh = feedback.fresh(v, Date.now());
      if (switchPending) {
        var confirmation;
        if (
          fresh &&
          v.applied_nonce === switchPending.nonce &&
          (v.state === (switchEnabled ? 'running' : 'disabled') ||
            (switchEnabled && v.state === 'waiting_devices'))
        ) {
          confirmation =
            v.state === 'waiting_devices'
              ? v.message || _('等待核对设备地址')
              : switchEnabled
                ? _('已开启，正在按保存的范围和规则接管网络。')
                : _('已关闭，本插件已退出接管；恢复此前的网络方式。');
        } else if (fresh && v.state === 'error' && v.attempted_nonce === switchPending.nonce) {
          confirmation = (switchEnabled ? _('开启失败：') : _('关闭失败：')) + v.message;
        } else if (Date.now() - switchPending.started > 45000) {
          confirmation = _('尚未收到切换确认，请查看实际运行状态。');
        }
        if (confirmation) {
          switchText.textContent = confirmation;
          powerButton.disabled = false;
          switchPending = null;
        }
      }
      if (v) {
        var exitName =
            v.backend === 'worker'
              ? v.backup_name || _('Workers 备用')
              : directoryLabel(v.exit, catalog),
          message = v.message || v.state;
        if (
          !catalog.skipped &&
          v.state === 'preparing' &&
          message === '登录环境已就绪；请完成邮箱登录，再自动读取出口'
        )
          message = catalog.available
            ? _('登录环境已就绪，分流尚未开启；已读到 ') +
              catalog.networks.length +
              _(' 个账户网络（未验证连通性）')
            : _('登录环境已就绪；出口目录暂未取得，可重新检查账户，或到分流设置刷新目录');
        status.textContent =
          String(message) +
          ' · ' +
          _('出口：') +
          exitName +
          ' · IP ' +
          String(v.exit_ip || '—') +
          ' · ' +
          (v.scope === 'lan'
            ? _('所选局域网')
            : _('接管设备：') + (Array.isArray(v.devices) ? v.devices.length : 0));
        status.style.color =
          fresh && v.state === 'running' ? '#087443' : v.state === 'error' ? '#b42318' : '#555';
        if (!fresh) {
          status.textContent = _('运行状态已过期，无法确认 WARP 或直连是否生效；请检查服务。');
          status.style.color = '#a15c00';
        }
        if (pendingApply) {
          var p = pendingApply;
          if (
            fresh &&
            v.applied_nonce === p.nonce &&
            ['running', 'disabled', 'preparing', 'waiting_devices'].indexOf(v.state) >= 0
          ) {
            p.box.textContent =
              v.state === 'waiting_devices'
                ? v.message
                : p.update
                  ? _('配置已应用，等待规则更新反馈…')
                  : _('已应用成功：') + status.textContent;
            pendingApply = null;
          } else if (fresh && v.state === 'error' && v.attempted_nonce === p.nonce) {
            p.box.textContent = _('应用失败：') + v.message;
            pendingApply = null;
            pendingUpdate = null;
          } else if (Date.now() - p.started > 45000) {
            p.box.textContent = _('已提交，但尚未收到应用确认。请检查页面顶部的实际运行状态。');
            pendingApply = null;
          }
        }
      } else status.textContent = _('尚无运行状态，未能确认服务是否生效。');
      if (ruleSection === 'overview' && files[1]) updateFeedback(files[1]);
      else
        rulesProgress.textContent = _(
          '尚无更新记录；点击“规则与更新 → 立即更新规则”查看完整反馈。'
        );
      var t = files[2];
      if (t) {
        var detecting = feedback.diagnosticRunning(t, Date.now()),
          staleTest =
            feedback.diagnosticStale(t, v, Date.now()) ||
            (center &&
              v &&
              (v.applied_nonce !== wizardConfig().nonce ||
                (v.backend !== 'worker' && v.exit !== configuredExit())));
        testButton.disabled =
          detecting || (center && (!feedback.fresh(v, Date.now()) || v.state !== 'running'));
        testsInfo.textContent = detecting
          ? _('正在检测，最长约 10 秒…')
          : (t.state === 'running'
              ? _('检测已中断或超时，结果未知；可以重新检测。')
              : (t.automatic ? _('后台检测时间：') : _('检测时间：')) + when(t.finished_at)) +
            ' · ' +
            _('代理出口：') +
            String(t.exit_ip || '—') +
            (staleTest ? ' · ' + _('历史结果已过期，不能代表当前网络，请重新检测') : '');
        (t.results || []).forEach(function (r) {
          var c = testCells[r.id];
          if (!c) return;
          c.result.textContent = (staleTest ? _('历史：') : '') + (r.message || _('等待检测'));
          c.result.style.color = staleTest
            ? '#777'
            : r.state === 'ok'
              ? '#087443'
              : r.state === 'pending'
                ? '#2254d1'
                : r.state === 'restricted'
                  ? '#a15c00'
                  : '#b42318';
          c.delay.textContent = r.ms != null ? r.ms + ' ms' : '—';
        });
      } else {
        testButton.disabled = !!(
          center &&
          (!feedback.fresh(v, Date.now()) || v.state !== 'running')
        );
        testsInfo.textContent = _('暂未取得有效的网站检测结果，请重新检测。');
        Object.keys(testCells).forEach(function (k) {
          testCells[k].result.textContent = _('未检测');
          testCells[k].result.style.color = '';
          testCells[k].delay.textContent = '—';
        });
      }
      if (page === 'account' || page === 'routing') {
        refreshComparison();
      }
      if (page === 'endpoint') return endpoint.poll();
      if (ruleDataPage) return rules.read(false);
    }
    function readRuntimeFiles() {
      return pageShared.readRuntimeFiles(runtimeFiles, runtimeSlots, applyRuntimeFiles, fs);
    }
    var initialOverviewAt = 0,
      initialOverviewRead =
        data.initialOverviewRead &&
        data.initialOverviewRead.then(function (files) {
          if (!isCurrentView()) return;
          if (feedback.fresh(files[0], Date.now())) initialOverviewAt = Date.now();
          return applyRuntimeFiles(files);
        });
    poll.add(function () {
      if (!isCurrentView()) return Promise.resolve();
      if (initialOverviewRead) {
        var first = initialOverviewRead;
        initialOverviewRead = null;
        return first.then(function () {
          if (!isCurrentView() || Date.now() - initialOverviewAt < 2000) return;
          return readRuntimeFiles();
        });
      }
      return readRuntimeFiles();
    }, 2);
    function requestChecksum(text) {
      var bytes = new TextEncoder().encode(text),
        size = (bytes.length + 72) & ~63,
        padded = new Uint8Array(size);
      padded.set(bytes);
      padded[bytes.length] = 128;
      new DataView(padded.buffer).setUint32(size - 4, bytes.length * 8);
      var h = [
          0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
          0x5be0cd19
        ],
        k = [
          0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
          0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
          0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
          0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
          0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
          0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
          0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
          0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
          0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
          0xc67178f2
        ];
      function r(x, n) {
        return (x >>> n) | (x << (32 - n));
      }
      var words = new Int32Array(64),
        view = new DataView(padded.buffer);
      for (var off = 0; off < size; off += 64) {
        for (var i = 0; i < 16; i++) words[i] = view.getInt32(off + i * 4);
        for (i = 16; i < 64; i++) {
          var x = words[i - 15],
            y = words[i - 2];
          words[i] =
            (words[i - 16] +
              (r(x, 7) ^ r(x, 18) ^ (x >>> 3)) +
              words[i - 7] +
              (r(y, 17) ^ r(y, 19) ^ (y >>> 10))) |
            0;
        }
        var a = h[0],
          b = h[1],
          c = h[2],
          d = h[3],
          e = h[4],
          f = h[5],
          g = h[6],
          v = h[7];
        for (i = 0; i < 64; i++) {
          var t =
              (v + (r(e, 6) ^ r(e, 11) ^ r(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + words[i]) | 0,
            u = ((r(a, 2) ^ r(a, 13) ^ r(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
          v = g;
          g = f;
          f = e;
          e = (d + t) | 0;
          d = c;
          c = b;
          b = a;
          a = (t + u) | 0;
        }
        [a, b, c, d, e, f, g, v].forEach(function (value, n) {
          h[n] = (h[n] + value) | 0;
        });
      }
      return h
        .map(function (value) {
          return (value >>> 0).toString(16).padStart(8, '0');
        })
        .join('');
    }
    function requestID() {
      if (!window.crypto || !window.crypto.getRandomValues)
        throw Error(_('浏览器不支持安全请求标识，请更新浏览器后重试'));
      var words = new Uint32Array(4);
      window.crypto.getRandomValues(words);
      return Array.prototype.map
        .call(words, function (v) {
          return v.toString(16).padStart(8, '0');
        })
        .join('');
    }
    function boundRequest(payload) {
      var id = requestID(),
        text = JSON.stringify({ request_id: id, payload: payload });
      return { id: id, text: text, token: id + ':' + requestChecksum(text) };
    }
    var backupCard = pageBackup.render(panel, command, notify, boundRequest, fs, uci);

    function saveOrganizationOnly() {
      var org = organizationOption.formvalue('main');
      if (org == null) org = uci.get('routepilot', 'main', 'organization') || '';
      return submitInput('organization-input', 'warp-org-save', {
        organization: String(org).trim()
      }).then(function (result) {
        uci.set('routepilot', 'main', 'organization', result.organization);
        uci.set('routepilot', 'main', 'apply_nonce', result.nonce);
        committedOrganization = result.organization;
        delete wizardActions.org;
        updateWizard();
        return result;
      });
    }
    function countryName(code) {
      return (
        {
          US: '美国',
          KR: '韩国',
          JP: '日本',
          SG: '新加坡',
          HK: '香港',
          TW: '台湾',
          DE: '德国',
          GB: '英国',
          CN: '中国',
          AU: '澳大利亚',
          CA: '加拿大'
        }[code] ||
        code ||
        _('未知')
      );
    }
    var centerState = E('div', { role: 'status', 'aria-live': 'polite' }),
      centerCountry = E('span', { class: 'rp-summary-tag', role: 'status', 'aria-live': 'polite' }),
      centerObservationHelp = E('span', {
        class: 'rp-help',
        id: 'imok-exit-check-help',
        role: 'status'
      }),
      centerWarpAuth = E('span', { class: 'rp-summary-tag' }),
      centerAPIAuth = E('span', { class: 'rp-summary-tag' }),
      centerAuth = E('div', { class: 'rp-auth-badges' }, [
        centerWarpAuth,
        centerAPIAuth,
        centerCountry
      ]),
      warpInfo = E('p', { role: 'status', 'aria-live': 'polite' });
    var centerReads = {};
    function observationCurrent(row, st) {
      return (
        row &&
        st &&
        row.state === 'done' &&
        feedback.fresh(st, Date.now()) &&
        st.state === 'running' &&
        [
          'runtime_id',
          'generation',
          'applied_nonce',
          'exit',
          'backend',
          'endpoint_generation'
        ].every(function (k) {
          return row[k] === st[k];
        }) &&
        Date.now() / 1000 - row.time >= -5 &&
        Date.now() / 1000 - row.time <= 300
      );
    }
    function updateCenter() {
      var st = wizardStatus,
        fresh = feedback.fresh(st, Date.now()),
        running = fresh && st && st.state === 'running';
      centerState.textContent = running
        ? (st.backend === 'worker' ? _('当前使用 Workers 备用') : _('当前使用 WARP 主线路')) +
          ' · ' +
          _('实际协议：') +
          (st.protocol || _('尚未确认')) +
          (st.protocol_warning ? _('（上次确认）') : '') +
          ' · ' +
          _('出口：') +
          (st.backend === 'worker'
            ? st.backup_name || _('Workers 备用')
            : catalog.available
              ? directoryLabel(st.exit, catalog)
              : String(st.exit || _('待确认'))) +
          ' · IP ' +
          (st.exit_ip || '—')
        : !fresh
          ? _('○ 状态过期，当前连接未确认')
          : st && st.state === 'disabled'
            ? _('○ 已关闭')
            : (st && st.message) || _('○ 尚无运行状态');
      centerState.className = running ? 'rp-center-ok' : 'rp-help';
      var summary = centerState.parentNode && centerState.parentNode.parentNode;
      if (summary && summary.setAttribute)
        summary.setAttribute('data-tone', running ? 'success' : fresh ? 'info' : 'warning');
      var valid = observationCurrent(exitObservation, st);
      centerCountry.textContent = valid
        ? _('本次观测出口地区：') +
          countryName(exitObservation.country) +
          ' (' +
          exitObservation.country +
          ')'
        : exitObservation
          ? _('○ 地区观测已过期或线路改变，可重新检测')
          : _('○ 出口地区尚未检测');
      var cfg = workers.authorization();
      centerWarpAuth.textContent =
        catalog.available || registeredHere || (running && st.backend !== 'worker')
          ? _('✓ WARP 已注册')
          : catalog.skipped
            ? _('○ 本页未读取 WARP 注册状态')
            : _('○ WARP 注册待确认');
      centerAPIAuth.textContent =
        cfg && cfg.configured ? _('✓ API 令牌已保存') : _('○ API 尚未读取');
      warpInfo.textContent =
        warpState && warpState.state === 'available'
          ? _('读取到的客户端协议：') +
            warpState.protocol +
            ' · ' +
            ({
              organization: _('组织策略'),
              local_policy: _('本机管理配置'),
              local: _('客户端设置'),
              mdm: _('本机管理文件'),
              routepilot: _('本路由器配置'),
              external_mdm: _('外部管理文件')
            }[warpState.source] || _('来源待确认')) +
            ' · ' +
            warpState.message
          : _('尚未读取到主线路协议信息；新安装先完成本组登录。');
      var labels = {
        advanced: catalog.available ? _('账户可读取') : _('账户待确认'),
        basic: running ? _('已生效') : _('待确认'),
        devices:
          uci.get('routepilot', 'main', 'scope') === 'lan'
            ? _('当前接管范围为「局域网」，此处策略仅在切换到「按设备选择」后生效。')
            : _('逐台选择分流模式，启用的设备共用所选代理出口。'),
        dns: dns.running(st) ? _('DNS 运行中') : _('DNS 待确认'),
        workers:
          running && st.backend === 'worker'
            ? _('备用运行中')
            : workers.hasConfiguredBackup()
              ? _('已配置，未接管')
              : _('未配置')
      };
      Object.keys(centerGroupBadges || {}).forEach(function (k) {
        centerGroupBadges[k].textContent = labels[k] || '';
      });
      centerRefresh.disabled = !!centerReads['warp-info'];
      centerRefresh.textContent = centerRefresh.disabled ? _('正在读取协议…') : _('读取当前协议');
      centerObserve.disabled = !!centerReads['exit-observe'] || !running;
      centerObserve.textContent = centerReads['exit-observe']
        ? _('正在检测地区…')
        : _('检测出口地区');
      centerObservationHelp.textContent = running ? '' : _('线路运行后可检测地区。');
    }
    function centerRead(action) {
      if (centerReads[action]) return centerReads[action];
      centerReads[action] = command(action)
        .then(function (raw) {
          var result = JSON.parse(raw);
          if (!isCurrentView()) return;
          if (action === 'warp-info') warpState = result;
          else exitObservation = result;
        })
        .catch(function (e) {
          notify(e.message, true);
        })
        .finally(function () {
          delete centerReads[action];
          updateCenter();
        });
      updateCenter();
      return centerReads[action];
    }
    var centerRefresh = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return centerRead('warp-info');
        }
      },
      _('读取当前协议')
    );
    var centerObserve = E(
      'button',
      {
        class: 'btn',
        'aria-describedby': 'imok-exit-check-help',
        click: function () {
          return centerRead('exit-observe');
        }
      },
      _('检测出口地区')
    );
    var centerGroups = {},
      centerGroupBadges = {};
    function centerGroup(id, title, description, children) {
      var state = (centerGroupBadges[id] = E('span', { class: 'rp-group-state' })),
        body = [
          pageShared.statusBar(
            'info',
            description ? [state, E('span', {}, description)] : [state],
            'rp-group-status'
          )
        ];
      var box = E(
        'section',
        {
          class: 'rp-center-group imok-settings-section',
          id: 'rp-center-' + id,
          'aria-label': title
        },
        E('div', { class: 'rp-center-body' }, body.concat(children.filter(Boolean)))
      );
      centerGroups[id] = box;
      return box;
    }
    var endpoint = pageEndpoint.create({
      boundRequest,
      command,
      feedback,
      fs,
      notify,
      showImokModal,
      ui,
      when,
      getStatus: function () {
        return wizardStatus;
      }
    });
    function buildCenter(node, force) {
      if (centerComposed === node && !force) return [node];
      function fields(group) {
        return E(
          'div',
          {},
          centerFields[group]
            .map(function (o) {
              return node.querySelector('[id="cbi-routepilot-main-' + o.option + '"]');
            })
            .filter(Boolean)
        );
      }
      function selectFields(keys) {
        return keys
          .map(function (key) {
            return node.querySelector('[id="cbi-routepilot-main-' + key + '"]');
          })
          .filter(Boolean);
      }
      var guide = node.querySelector('.rp-account-guide'),
        protocol = node.querySelector('[id="cbi-routepilot-main-warp_protocol"]');
      var top = pageShared.statusBar(
        'info',
        [
          E('div', { class: 'rp-summary-connection' }, [
            centerState,
            page === 'workers'
              ? centerAuth
              : E('div', { class: 'rp-auth-badges' }, [centerWarpAuth, centerCountry])
          ]),
          E('div', { class: 'rp-summary-actions', title: _('只读检测，不切换出口') }, [
            centerObserve,
            centerObservationHelp
          ])
        ],
        'rp-center-summary'
      );
      updateCenter();
      var groups = [
        E(
          'nav',
          { class: 'rp-section-nav', 'aria-label': _('Cloudflare 设置页面') },
          pages
            .filter(function (p) {
              return sectionGroups[p[0]];
            })
            .map(function (p) {
              return E(
                'a',
                {
                  class: 'btn',
                  href: base + '/cloudflare/' + p[0],
                  'aria-current': p[0] === page ? 'page' : null
                },
                p[1]
              );
            })
        ),
        top
      ];
      if (pageGroup === 'advanced')
        groups.push(
          pageAccount.render(centerGroup, panel, protocol, warpInfo, centerRefresh, guide),
          protocolComparison.render(panel)
        );
      if (pageGroup === 'endpoint')
        groups.push(pageEndpoint.render(centerGroup, panel, endpoint.details, endpoint.card));
      if (pageGroup === 'basic') groups.push(pageRouting.render(centerGroup, panel, selectFields));
      if (pageGroup === 'devices') {
        var devicePage = pageDevices.render(panel, centerDeviceNode, function () {
          return pageDevices.renderUsage({ bytes, fs, helpDetails, isCurrentView, panel });
        });
        centerGroupBadges.devices = devicePage.scopeHint;
        deviceLeaseHint = devicePage.leaseHint;
        groups.push(devicePage.panel, devicePage.usage);
      }
      if (pageGroup === 'dns')
        groups.push(
          pageDns.render(
            centerGroup,
            panel,
            fields,
            dns.cacheCard,
            dns.testCard,
            dns.renderBootstrap(panel, selectFields),
            dns.renderLanCandidates(panel)
          )
        );
      if (pageGroup === 'workers') groups.push(workers.render(centerGroup));
      groups.push(
        pageShared.statusBar(
          'info',
          [
            page === 'endpoint'
              ? _('本页使用各操作按钮单独提交；只保存同步间隔不会重连，应用入口可能重连。')
              : page === 'workers'
                ? _('完成独立检测后，点击「手动启用备用」切换线路。')
                : _('页面底部“保存并应用”只提交本页设置；读取、检测和清缓存不会提交编辑。')
          ],
          'rp-save-hint'
        )
      );
      var nativeTabs = document.querySelector && document.querySelector('#tabmenu');
      if (nativeTabs && groups[0].querySelectorAll) {
        var nativeLinks = Array.from(nativeTabs.querySelectorAll('a')).map(function (a) {
          return new URL(a.getAttribute('href'), window.location.href).pathname;
        });
        var pluginLinks = Array.from(groups[0].querySelectorAll('a'));
        if (
          pluginLinks.length &&
          pluginLinks.every(function (a) {
            return nativeLinks.includes(
              new URL(a.getAttribute('href'), window.location.href).pathname
            );
          })
        )
          groups.shift();
      }
      node.replaceChildren.apply(node, groups);
      pageShared.decorateFields(node);
      centerComposed = node;
      updateCenter();
      if (page === 'workers') {
        workers.updateProgress();
      }
      return [node];
    }
    if (
      (ruleSection === 'sources' || ruleSection === 'custom') &&
      typeof m.renderContents === 'function'
    ) {
      var renderRules = m.renderContents.bind(m);
      m.renderContents = function () {
        return renderRules.apply(null, arguments).then(function (node) {
          return pageShared.decorateFields(
            pageRules.renderForm(node, ruleSection, rules.names, panel)
          );
        });
      };
    }

    if (center && typeof m.renderContents === 'function') {
      var renderMap = m.renderContents.bind(m);
      m.renderContents = function () {
        return renderMap.apply(null, arguments).then(function (node) {
          buildCenter(node, true);
          return node;
        });
      };
    }

    var content =
      page === 'backup'
        ? [backupCard]
        : pageRules.render(
            ruleSection,
            rules.nav,
            rules.legacyCard,
            rules.statusCard,
            rules.sourcesIntro,
            rules.browser,
            rules.customIntro
          );
    return (center || formPage ? m.render() : Promise.resolve(null)).then(function (node) {
      if (center) {
        content = buildCenter(node);
      } else if (node) {
        var title = node.querySelector('h2');
        if (title) title.remove();
        if (typeof node.querySelectorAll === 'function')
          Array.prototype.forEach.call(node.querySelectorAll('h3'), function (heading) {
            if (heading.textContent.trim() === pageInfo[1]) heading.remove();
          });
        var intro = node.querySelector('.cbi-map-descr');
        if (intro) intro.remove();
        content.push(node);
      }
      if (['overview', 'account', 'routing'].includes(page))
        Promise.resolve()
          .then(function () {
            if (isCurrentView()) return refreshDirectory();
          })
          .catch(function () {});
      if (ruleDataPage)
        Promise.resolve().then(function () {
          return rules.read(true);
        });
      if (page === 'endpoint')
        Promise.resolve().then(function () {
          if (isCurrentView()) return endpoint.initialize();
        });
      if (page === 'account')
        Promise.resolve()
          .then(function () {
            if (isCurrentView()) return centerRead('warp-info');
          })
          .catch(function () {});
      var diagnostics =
        page === 'overview'
          ? E(
              'details',
              {
                class: 'rp-stat-details',
                id: 'imok-network-diagnostics',
                open: window.location.hash === '#imok-network-diagnostics' ? true : null
              },
              [
                E('summary', {}, _('检测与测速')),
                E('div', { class: 'imok-settings-pair imok-network-tests' }, [
                  speed.render(panel),
                  testCard
                ]),
                E('details', { class: 'rp-stat-details' }, [
                  E('summary', {}, _('UDP 接收观测与内部名称')),
                  receiveCard,
                  panel(_('内部名称对照'), null, [
                    E('dl', {}, [
                      E('dt', {}, _('包名')),
                      E('dd', {}, 'luci-app-routepilot'),
                      E('dt', {}, _('配置名')),
                      E('dd', {}, 'routepilot'),
                      E('dt', {}, _('规则与状态目录')),
                      E('dd', {}, '/etc/routepilot · /var/run/routepilot'),
                      E('dt', {}, _('策略路由表')),
                      E('dd', {}, '51990'),
                      E('dt', {}, _('nft 分流与保护')),
                      E('dd', {}, 'inet routepilot · inet routepilot_egress_guard')
                    ])
                  ])
                ])
              ]
            )
          : null;
      if (diagnostics && window.addEventListener)
        window.addEventListener('hashchange', function openDiagnostics() {
          if (!isCurrentView()) {
            window.removeEventListener('hashchange', openDiagnostics);
          } else if (window.location.hash === '#imok-network-diagnostics') {
            diagnostics.open = true;
          }
        });
      return finishPage(
        E('div', { class: 'rp-layout', 'data-page': page }, [
          E('link', { rel: 'stylesheet', href: L.resource(imokStylesheet) }),
          E('div', { class: 'rp-header' }, [
            E('span', { class: 'rp-brand' }, 'imOK'),
            E('h2', {}, pageInfo[1]),
            E('p', { class: 'rp-caption' }, pageInfo[2])
          ]),
          E(
            'div',
            { class: 'rp-page-content' },
            page === 'overview'
              ? pageOverview.render(
                  overview.hero,
                  overview.traffic,
                  overview.nodes,
                  overview.quick,
                  protectionHistory,
                  connectionHistory,
                  overview.version,
                  diagnostics
                )
              : content
          )
        ])
      );
    });
  }
});

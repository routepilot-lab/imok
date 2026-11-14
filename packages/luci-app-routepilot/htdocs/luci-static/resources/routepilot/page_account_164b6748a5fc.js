'use strict';
'require baseclass';

return baseclass.extend({
  render: function (centerGroup, panel, protocol, warpInfo, refreshProtocol, guide) {
    return centerGroup('advanced', _('主线路账户与连接协议'), null, [
      guide,
      panel(_('协议设置'), _('保存并应用后切换协议，主线路会短暂重连。'), [
        protocol,
        warpInfo,
        E('div', { class: 'rp-actions' }, [refreshProtocol])
      ])
    ]);
  },
  protocolComparison: function ({
    base,
    comparisonLineHint,
    startComparison,
    stopComparison,
    comparisonBusy,
    comparisonCell
  }) {
    var protocolComparisonStatus = E('p', { role: 'status', 'aria-live': 'polite' });
    var protocolComparisonHelp = E('p', { class: 'rp-help', id: 'imok-protocol-test-help' });
    var protocolOverview = E('a', { href: base + '/overview', hidden: true }, _('前往总览'));
    var protocolComparisonRows = E('div', { class: 'rp-help' });
    var compareProtocols = E(
      'button',
      {
        class: 'btn',
        'aria-describedby': 'imok-protocol-test-help',
        click: function () {
          startComparison('protocol', ['MASQUE', 'WireGuard']);
        }
      },
      _('对比 MASQUE 与 WireGuard')
    );
    var cancelProtocols = E('button', { class: 'btn', click: stopComparison }, _('取消测速'));
    return {
      render: function (panel) {
        return panel(_('协议对比测速'), _('依次测量两种协议，再恢复已保存的协议。'), [
          E('div', { class: 'rp-actions' }, [compareProtocols, cancelProtocols]),
          protocolComparisonHelp,
          protocolOverview,
          protocolComparisonStatus,
          protocolComparisonRows
        ]);
      },
      update: function (wizardStatus, comparisonResult, statusText) {
        var lineHint = comparisonLineHint();
        var reason =
          lineHint ||
          (wizardStatus.protocol_managed === true
            ? _('协议由 Cloudflare 组织管理，无法切换测速。')
            : wizardStatus.protocol_managed !== false
              ? _('还未确认协议是否由组织管理。')
              : comparisonBusy()
                ? _('已有测速正在进行。')
                : '');
        protocolOverview.hidden = !lineHint;
        compareProtocols.disabled = !!reason;
        protocolComparisonHelp.textContent = reason;
        var protocolResult = comparisonResult && comparisonResult.kind === 'protocol';
        cancelProtocols.hidden = !(
          protocolResult && ['queued', 'testing'].includes(comparisonResult.state)
        );
        protocolComparisonStatus.textContent = protocolResult ? statusText : '';
        var rows = protocolResult && comparisonResult.rows ? comparisonResult.rows : [];
        protocolComparisonRows.replaceChildren(
          E(
            'div',
            { class: 'rp-actions' },
            ['MASQUE', 'WireGuard'].map(function (name) {
              return E(
                'p',
                {},
                name +
                  ' · ' +
                  comparisonCell(
                    rows.find(function (row) {
                      return row.target === name;
                    })
                  )
              );
            })
          )
        );
      }
    };
  },
  setupGuide: function (
    node,
    {
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
      readState
    }
  ) {
    var wizardBadges = {};
    var steps = [];
    var wizardSummary = E('p', { role: 'status', 'aria-live': 'polite' });
    function para(text) {
      return E('p', {}, text);
    }
    function link(text, url) {
      return E('a', { href: url, target: '_blank', rel: 'noopener' }, text);
    }
    function step(id, title, items, fields) {
      var help = id === 'intro',
        content = items;
      if (!help) {
        var guidance = E('details', { class: 'rp-setting-help' }, [
          E('summary', {}, _('操作说明')),
          E('div', {}, items)
        ]);
        content = [guidance];
        if (id === 'prepare') content.unshift(para(_('只启动登录环境，不接管设备。')));
        if (id === 'auth')
          content.unshift(
            para(
              _('仅注册路由器；提示打开本机 Cloudflare 应用时取消。登录链接是临时凭据，请勿分享。')
            )
          );
      }
      var box = E('div', { class: 'rp-guide-body' }, content),
        badge = E('span', { class: 'rp-step-badge' }, help ? _('说明') : _('○ 未执行'));
      (fields || []).forEach(function (key) {
        var field = node.querySelector('[id="cbi-routepilot-main-' + key + '"]');
        if (field) box.appendChild(field);
      });
      var heading = E(help ? 'summary' : 'h3', help ? {} : { class: 'rp-guide-heading' }, [
          badge,
          E('span', {}, title)
        ]),
        card = E(help ? 'details' : 'section', { class: 'rp-guide-step', 'data-step': id }, [
          heading,
          box
        ]);
      wizardCards[id] = card;
      wizardBadges[id] = badge;
      steps.push(card);
      return card;
    }
    step('intro', _('开始前：新路由器需要准备什么？'), [
      para(
        _(
          '先在 iStore 安装 imOK 和系统依赖。到「WARP 安装与更新」检查本机环境，首次使用选择「自动下载并安装 WARP」。页面显示「WARP 已安装」后，回到这里登录。'
        )
      ),
      para(
        _(
          '在线准备支持 ARM64 OpenWrt 24.10，约需 1.6 GB 可用存储。安装页会检查本机条件。首次安装默认关闭 imOK。'
        )
      ),
      para(
        _(
          '换路由器但继续用同一 Cloudflare 账户：组织名称、现有虚拟网络及 VPS 出口可以沿用，不用重新创建；新路由器必须独立登录。先保持总览里的总开关关闭。'
        )
      ),
      para(
        _(
          '快捷迁移：在旧路由器“备份迁移”导出配置，到新路由器导入。导入后保持关闭；已导入组织名称时无需重复填写，仍需准备登录环境并注册新路由器。备份不包含登录凭据。'
        )
      ),
      E('a', { href: base + '/runtime' }, _('前往 WARP 安装与更新')),
      E('a', { href: base + '/backup' }, _('打开备份迁移 →'))
    ]);
    step(
      'org',
      _('组织名称'),
      [
        para(
          _(
            '到 Cloudflare 控制台进入 Zero Trust → Settings（设置），查看 Team name / Team domain（团队名称 / 团队域名）。也可查看已登录的 Cloudflare One 客户端账户页面。'
          )
        ),
        para(
          _(
            '例如显示 myteam.cloudflareaccess.com，这里只填 myteam；不填 https://，不填 .cloudflareaccess.com 后缀，也不填邮箱、个人域名或 Cloudflare 账户 ID。'
          )
        ),
        link(
          _('查看官方团队名称说明'),
          'https://developers.cloudflare.com/cloudflare-one/faq/getting-started-faq/'
        ),
        para(
          _(
            '如果是全新 Cloudflare 账户，需要先创建 Zero Trust 团队并允许你的邮箱注册设备。沿用现有账户则不要再创建。'
          )
        )
      ],
      ['organization']
    );
    wizardCards.org.querySelector('.rp-guide-body').appendChild(
      E(
        'button',
        {
          class: 'btn',
          click: function () {
            beginWizardStep('org');
            return save().catch(function (e) {
              failWizardStep('org');
              notify(e.message, true);
            });
          }
        },
        _('保存组织名称')
      )
    );
    step(
      'prepare',
      _('准备路由器登录环境'),
      [
        para(_('点击下方按钮。它只启动独立登录环境，不接管局域网，不需要先打开总开关。')),
        para(
          _(
            '看到「登录环境已就绪」后，再点击「邮箱登录」。如缺少运行环境，先去「WARP 安装与更新」完成安装。'
          )
        )
      ],
      ['_prepare']
    );
    step(
      'auth',
      _('邮箱验证与路由器注册'),
      [
        para(
          _(
            '点击“邮箱登录”，在弹窗打开团队登录地址。使用组织允许的邮箱收验证码，或按组织要求完成登录。'
          )
        ),
        para(
          _(
            '若浏览器询问是否打开电脑上的 Cloudflare 应用，选择取消。你要注册的是新路由器，不是这台电脑。'
          )
        ),
        para(
          _(
            '粘贴 token= 后的令牌、完整授权链接，或成功页源码里含 token= 的整行，再点击“注册此路由器”。imOK 会自动提取，请及时提交。'
          )
        ),
        para(
          _(
            '成功标志：提示路由器已注册。若邮箱无权限，在 Cloudflare 的设备注册权限中允许该邮箱；链接失效则重新登录获取，不要反复粘贴旧链接。'
          )
        )
      ],
      ['_login']
    );
    var loginAvailability = E('p', { class: 'rp-help', role: 'status' });
    var prepareButton = wizardCards.prepare.querySelector('button');
    var loginButton = wizardCards.auth.querySelector('button');
    var prepareBlocked, loginBlocked;
    wizardCards.auth.querySelector('.rp-guide-body').appendChild(loginAvailability);
    var setup = E('details', { class: 'imok-panel rp-account-setup', open: true }, [
      E('summary', {}, _('首次登录与重新注册')),
      E('div', { class: 'rp-guide' }, steps)
    ]);
    var accountCheck = E(
      'div',
      { class: 'rp-account-check rp-actions' },
      [node.querySelector('[id="cbi-routepilot-main-_account_check"]')].filter(Boolean)
    );
    var checkButton = accountCheck.querySelector('button');
    var accountNext = E(
      'a',
      { class: 'btn', hidden: true, href: base + '/cloudflare/routing' },
      _('前往出口与分流')
    );
    accountCheck.appendChild(accountNext);
    var setupInitialized = false;
    var guide = E('div', { class: 'rp-account-guide' }, [
      pageShared.statusBar('info', [wizardSummary, accountCheck], 'rp-account-status'),
      setup
    ]);
    function updateWizard() {
      var {
        catalog,
        directoryPending,
        registeredHere,
        setupRuntime,
        setupRuntimePending,
        setupRuntimeUnavailable,
        committedOrganization,
        wizardStatus,
        wizardTest
      } = readState();
      var now = Date.now(),
        config = wizardConfig();

      ['org', 'prepare'].forEach(function (id) {
        var a = wizardActions[id];
        if (a && a.state === 'busy' && now - a.started > 45000)
          wizardActions[id] = { state: 'error', started: now, baseline: a.baseline };
      });
      var prep = wizardActions.prepare;
      if (
        prep &&
        prep.state === 'busy' &&
        prep.nonce &&
        feedback.fresh(wizardStatus, now) &&
        wizardStatus.state === 'error' &&
        wizardStatus.attempted_nonce === prep.nonce
      ) {
        prep.state = 'error';
      }
      if (
        prep &&
        prep.state === 'busy' &&
        prep.nonce &&
        feedback.fresh(wizardStatus, now) &&
        ['preparing', 'running'].indexOf(wizardStatus.state) >= 0 &&
        wizardStatus.applied_nonce === prep.nonce
      ) {
        delete wizardActions.prepare;
        prep = null;
      }
      var states = feedback.wizardSteps(
        config,
        catalog,
        wizardStatus,
        wizardTest,
        wizardActions,
        now,
        registeredHere
      );
      if (
        prep &&
        prep.state === 'busy' &&
        (!prep.nonce || !wizardStatus || wizardStatus.applied_nonce !== prep.nonce)
      ) {
        var ps = states.filter(function (x) {
          return x.id === 'prepare';
        })[0];
        ps.state = 'busy';
      }
      var descriptions = {
        org: committedOrganization ? _('组织名称已保存') : _('可沿用已可访问的账户，无需重填'),
        prepare: _('登录环境已就绪'),
        auth: registeredHere ? _('路由器已注册') : _('账户目录可访问；无需重复注册')
      };
      var names = {
        pending: '○ ' + _('待确认'),
        busy: '… ' + _('处理中'),
        success: '✓ ' + _('已完成'),
        error: '✕ ' + _('失败'),
        warning: '! ' + _('访问受限'),
        stale: '○ ' + _('未检测 / 已过期')
      };
      states.forEach(function (st) {
        var a = wizardActions[st.id];
        if (st.state === 'success' && a && a.state === 'busy') delete wizardActions[st.id];
        var pendingText =
          st.reason === 'access_previous' ? _('○ 账户检查结果较旧，可重新检查') : names[st.state];
        wizardBadges[st.id].textContent =
          st.state === 'success'
            ? '✓ ' + descriptions[st.id]
            : st.state === 'pending'
              ? pendingText
              : names[st.state];
        wizardCards[st.id].setAttribute('data-state', st.state);
      });
      if (!wizard.initialized) {
        var first = states.filter(function (st) {
          return st.state !== 'success';
        })[0];
        if (first) openWizardStep(first.id);
        wizard.initialized = true;
      }
      var current = states.filter(function (st) {
        return st.id === wizard.active;
      })[0];
      if (
        !wizard.pinned &&
        current &&
        wizard.advance === current.id &&
        current.state === 'success'
      ) {
        var index = states.indexOf(current),
          next = states.slice(index + 1).filter(function (st) {
            return st.state !== 'success';
          })[0];
        if (next) openWizardStep(next.id);
        else {
          wizard.active = null;
        }
        wizard.advance = null;
      }
      var statusFresh = feedback.fresh(wizardStatus, now);
      var accountActive =
        statusFresh && wizardStatus.state === 'running' && wizardStatus.backend === 'masque';
      var readAge = now - Number(catalog.read_at);
      var catalogReady = catalog.available && readAge >= -5000 && readAge <= 60000;
      var enabled = uci.get('routepilot', 'main', 'enabled') === '1';
      var loginReady = statusFresh && wizardStatus.state === 'preparing';
      var runtimeBlocked =
        setupRuntimeUnavailable ||
        (setupRuntime && (setupRuntime.installed === false || setupRuntime.recovery_required));
      var blockPrepare = enabled || !!setupRuntimePending || !!runtimeBlocked;
      if (prepareButton && prepareBlocked !== blockPrepare) {
        prepareBlocked = blockPrepare;
        prepareButton.disabled = prepareBlocked;
      }
      var blockLogin = blockPrepare || !loginReady;
      if (loginButton && loginBlocked !== blockLogin) {
        loginBlocked = blockLogin;
        loginButton.disabled = loginBlocked;
      }
      var registrationPending =
        !registeredHere && setupRuntime?.registration_files_present === false;
      if (checkButton)
        checkButton.disabled = !!(
          directoryPending ||
          (!enabled &&
            (setupRuntimePending ||
              runtimeBlocked ||
              registrationPending ||
              (setupRuntime?.registration_files_present === true && !loginReady && !catalogReady)))
        );
      accountNext.hidden = !!directoryPending || !catalogReady || !catalog.networks.length;
      loginAvailability.textContent = enabled
        ? _('如需重新登录，请先在总览关闭 imOK。')
        : setupRuntimePending
          ? _('正在检查 WARP 安装状态。')
          : setupRuntimeUnavailable
            ? _('暂时无法确认 WARP 安装状态。请到「WARP 安装与更新」刷新本机状态。')
            : runtimeBlocked
              ? _('请先到「WARP 安装与更新」核对安装状态。')
              : !loginReady
                ? _('先准备登录环境，再进行邮箱登录。')
                : '';
      if (!setupInitialized && (accountActive || (catalogReady && catalog.networks.length))) {
        if (!document.activeElement || !setup.contains(document.activeElement)) setup.open = false;
        setupInitialized = true;
      }
      var summary = _('账户待确认。已有账户可直接检查，首次使用请按下方步骤登录。');
      if (!enabled && setupRuntimePending) summary = _('正在检查 WARP 安装状态。');
      else if (!enabled && setupRuntimeUnavailable)
        summary = _('暂时无法确认 WARP 安装状态，请刷新本机状态后再登录。');
      else if (!enabled && runtimeBlocked)
        summary = _('请先完成 WARP 安装或维护，再登录 Cloudflare。');
      else if (directoryPending)
        summary = registeredHere ? _('路由器已注册，正在读取账户出口。') : _('正在检查账户。');
      else if (catalogReady) {
        var catalogResult = catalog.networks.length
          ? _('已读取 %s 个账户网络，连通性待验证。').replace('%s', String(catalog.networks.length))
          : _('账户目录已读取，但没有返回可选网络。请核对 Cloudflare 团队及出口配置后重新读取。');
        summary = (registeredHere ? _('路由器已注册。') : '') + catalogResult;
      } else if (registeredHere || catalog.last_success_at || catalog.read_at) {
        if (catalog.available) summary = _('上次的出口目录已过期。请点击「检查账户」重新读取。');
        else
          summary =
            registeredHere || catalog.last_success_at
              ? _('已注册路由器，暂时无法读取出口目录。请重新检查账户。')
              : _('读取出口目录失败。请检查 WARP 登录环境，再点「检查账户」重试。');
      } else if (accountActive) summary = _('当前 WARP 隧道已连接，可重新读取账户目录。');
      else if (registrationPending)
        summary = loginReady
          ? _('登录环境已就绪。请完成邮箱验证和路由器注册，成功后会自动读取账户出口。')
          : _('本路由器尚未注册。请准备登录环境，再进行邮箱验证和注册。');
      else if (setupRuntime?.registration_files_present)
        summary = loginReady
          ? _('本机已有注册资料。请检查账户，确认能读取出口。')
          : _('本机已有注册资料。请先准备登录环境。');
      wizardSummary.textContent = summary;
    }
    return { element: guide, update: updateWizard };
  },
  configure: function ({
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
    getCatalog,
    invalidateDirectory,
    refreshWizard,
    markRegistered
  }) {
    var o = pageOption(
      'advanced',
      form.ListValue,
      'warp_protocol',
      _('主线路连接协议'),
      _(
        '默认跟随现有客户端设置。选择 MASQUE / WireGuard 后仅管理本路由器客户端，应用时重连；不自动换出口、不改其他设备或缓冲。WireGuard 尚未在本机切换验收。'
      )
    );
    o.value('auto', _('保持现状 / 跟随当前设置'));
    o.value('MASQUE', 'MASQUE');
    o.value('WireGuard', 'WireGuard');
    o.cfgvalue = function () {
      return uci.get('routepilot', 'main', 'warp_protocol') || 'auto';
    };
    o.write = function (id, value) {
      if (value === 'auto' && uci.get('routepilot', id, 'warp_protocol') == null) return;
      uci.set('routepilot', id, 'warp_protocol', value);
    };
    o.rmempty = false;
    o = pageOption(
      'advanced',
      form.Value,
      'organization',
      _('组织名称（只在首次登录时填写）'),
      _(
        '仅首次登录时填写 Cloudflare Zero Trust 团队名称，不填邮箱或个人网站域名；查找方法见操作说明。'
      )
    );
    o.datatype = 'hostname';
    var organizationOption = o;
    o = pageOption('advanced', form.Button, '_prepare', _('启动独立登录环境'));
    o.inputtitle = _('准备登录环境');
    o.onclick = function () {
      if (uci.get('routepilot', 'main', 'enabled') === '1') {
        notify(_('请先到“总览”关闭总开关；已有连接无需重新登录。'), true);
        return;
      }
      beginWizardStep('prepare');
      return save()
        .then(function (result) {
          if (!result || !result.nonce) throw Error(_('缺少本次保存确认，未启动登录环境'));
          wizardActions.prepare.nonce = result.nonce;
          return command('prepare');
        })
        .then(function (msg) {
          uci.set('routepilot', 'main', 'preparing', '1');
          notify(msg);
        })
        .catch(function (e) {
          failWizardStep('prepare');
          notify(e.message, true);
        });
    };
    o = pageOption('advanced', form.Button, '_account_check', _('检查账户'));
    o.inputtitle = _('检查账户');
    o.onclick = function () {
      beginWizardStep('auth');
      return refreshDirectory().finally(function () {
        delete wizardActions.auth;
        refreshWizard();
      });
    };
    o = pageOption('advanced', form.Button, '_login', _('验证账户'));
    o.inputtitle = _('邮箱登录');
    o.onclick = function () {
      if (uci.get('routepilot', 'main', 'enabled') === '1') {
        notify(_('请先到“总览”关闭总开关；已有连接无需重新登录。'), true);
        return;
      }
      function alreadyAccessible() {
        return getCatalog().available && Date.now() - getCatalog().read_at <= 60000;
      }
      function skipLogin() {
        openWizardStep('auth');
        notify(_('账户目录已可访问，无需重复邮箱注册。'));
      }
      if (alreadyAccessible()) {
        skipLogin();
        return;
      }
      return save()
        .then(function () {
          return fs.read('/var/run/routepilot/status.json').then(JSON.parse);
        })
        .then(function (st) {
          if (!feedback.fresh(st, Date.now()) || st.state !== 'preparing')
            throw Error(_('登录环境尚未确认就绪，请先准备路由器登录环境并等待状态更新。'));
          if (alreadyAccessible()) {
            skipLogin();
            return;
          }
          invalidateDirectory();
          beginWizardStep('auth');
          return command('login')
            .then(function (url) {
              var token = E('input', {
                  type: 'password',
                  class: 'cbi-input-text',
                  autocomplete: 'off',
                  spellcheck: false,
                  placeholder: _('粘贴令牌、授权链接或源码整行'),
                  'aria-label': _('Cloudflare 临时授权凭据'),
                  'aria-describedby': 'imok-router-login-help'
                }),
                registrationPending = false;
              var closeButton = E(
                'button',
                {
                  class: 'btn',
                  click: function () {
                    if (registrationPending) return;
                    delete wizardActions.auth;
                    ui.hideModal();
                    refreshWizard();
                  }
                },
                _('关闭')
              );
              var registerButton = E(
                'button',
                {
                  class: 'btn',
                  click: function () {
                    if (registrationPending) return;
                    if (!token.value.trim()) {
                      token.focus();
                      notify(_('请先粘贴 Cloudflare 登录成功页的授权凭据'), true);
                      return;
                    }
                    beginWizardStep('auth');
                    registrationPending = true;
                    registerButton.disabled = true;
                    closeButton.disabled = true;
                    var loginPayload = { token: token.value };
                    token.value = '';
                    return submitInput('auth-input', 'register', loginPayload)
                      .then(function (msg) {
                        markRegistered();
                        delete wizardActions.auth;
                        ui.hideModal();
                        notify(msg.message);
                        return refreshDirectory(true);
                      })
                      .catch(function (e) {
                        failWizardStep('auth');
                        notify(e.message, true);
                      })
                      .finally(function () {
                        registrationPending = false;
                        registerButton.disabled = false;
                        closeButton.disabled = false;
                      });
                  }
                },
                _('注册此路由器')
              );
              showImokModal(_('登录独立路由器客户端'), [
                E(
                  'p',
                  {},
                  _(
                    '打开下方地址完成邮箱验证。浏览器提示打开电脑上的 Cloudflare 应用时，选择取消。'
                  )
                ),
                E('a', { href: url.trim(), target: '_blank', rel: 'noopener' }, url.trim()),
                E(
                  'p',
                  { class: 'rp-help', id: 'imok-router-login-help' },
                  _(
                    '成功页右键选择“查看网页源代码”，搜索 token=，复制含授权链接的整行即可。也可直接粘贴完整链接或 token= 后的令牌。引号和反斜杠会自动处理。'
                  )
                ),
                token,
                registerButton,
                closeButton
              ]);
            })
            .catch(function (e) {
              failWizardStep('auth');
              notify(e.message, true);
            });
        })
        .catch(function (e) {
          notify(_('准备或状态读取未完成：') + e.message, true);
        });
    };
    return organizationOption;
  }
});

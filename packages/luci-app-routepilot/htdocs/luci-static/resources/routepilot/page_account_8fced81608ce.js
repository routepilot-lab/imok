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
    feedback,
    startComparison,
    stopComparison,
    comparisonBusy,
    comparisonCell
  }) {
    var protocolComparisonStatus = E('p', { role: 'status', 'aria-live': 'polite' });
    var protocolComparisonHelp = E('p', { class: 'rp-help', id: 'imok-protocol-test-help' });
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
          protocolComparisonStatus,
          protocolComparisonRows
        ]);
      },
      update: function (wizardStatus, comparisonResult, statusText) {
        var ready = feedback.fresh(wizardStatus, Date.now());
        var reason = !ready
          ? _('正在等待主线路状态。')
          : wizardStatus.state !== 'running' || wizardStatus.backend !== 'masque'
            ? _('请先在总览启用 WARP 主线路。')
            : wizardStatus.protocol_managed === true
              ? _('协议由 Cloudflare 组织管理，无法切换测速。')
              : wizardStatus.protocol_managed !== false
                ? _('还未确认协议是否由组织管理。')
                : comparisonBusy()
                  ? _('已有测速正在进行。')
                  : '';
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
          '下载 ZIP 不会自动安装。完整安装包包含 IPK 插件、WARP 运行环境压缩包、安装脚本和校验文件。将 ZIP 解压后的整个文件夹复制到兼容路由器，在该文件夹内以管理员运行 sh install-router.sh；脚本会校验文件、解压运行环境并安装插件。仅在网页上传 IPK，只安装管理插件，不会自动安装配套环境。'
        )
      ),
      para(
        _(
          '支持条件：目前只验证 GL-MT6000 / ARM64 OpenWrt 24.10，至少 500 MB 可用存储。不是在 Mac 或 Windows 上运行脚本。运行环境已存在时，安装脚本会停止以保护注册信息；更新已有插件只需安装新版 IPK，不要删除运行环境。安装结束默认关闭，之后再完成本向导登录。'
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
            '成功标志：本页上方实际状态显示“登录环境已就绪”。首次启动可能稍慢；未就绪先不要点击邮箱登录。若提示缺少运行环境，请先按安装包说明安装配套运行环境。'
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
            '复制登录成功页面里的完整 com.cloudflare.warp://… 登录链接，粘贴回插件弹窗，点击“注册此路由器”。这是临时登录凭据，不要分享或发到 GitHub。'
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
    var wizardNext = E(
      'div',
      { class: 'rp-layout-status rp-account-next', 'data-tone': 'success' },
      [
        E('div', {}, [E('h3', {}, _('选择代理出口')), para(_('选好出口后，点击「保存并应用」。'))]),
        E('a', { href: base + '/cloudflare/routing', class: 'btn' }, _('去选出口'))
      ]
    );
    wizardNext.hidden = true;
    var setup = E('details', { class: 'imok-panel rp-account-setup', open: true }, [
      E('summary', {}, _('首次登录与重新注册')),
      E('div', { class: 'rp-guide' }, steps)
    ]);
    var accountCheck = E(
      'div',
      { class: 'rp-account-check rp-actions' },
      [node.querySelector('[id="cbi-routepilot-main-_account_check"]')].filter(Boolean)
    );
    var setupInitialized = false;
    var guide = E('div', { class: 'rp-account-guide' }, [
      pageShared.statusBar('info', [wizardSummary, accountCheck], 'rp-account-status'),
      wizardNext,
      setup
    ]);
    function updateWizard() {
      var {
        catalog,
        directoryPending,
        registeredHere,
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
        now
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
        auth: _('账户目录可访问；无需重复注册'),
        dir: _('目录已读取（未验证联网）'),
        select: _('选择已保存（未验证运行）'),
        verify: _('当前连接所测网站通过')
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
          st.reason === 'access_previous'
            ? _('○ 账户检查结果较旧，可重新检查')
            : st.reason === 'directory_old'
              ? _('○ 上次已读取，待刷新')
              : st.id === 'select' && st.reason === 'saved'
                ? _('○ 已保存，目录待确认')
                : names[st.state];
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
        current.state === 'success' &&
        current.id !== 'verify'
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
      var accountReady =
        catalog.available ||
        (statusFresh && wizardStatus.state === 'running' && wizardStatus.backend === 'masque');
      var enabled = uci.get('routepilot', 'main', 'enabled') === '1';
      var loginReady = statusFresh && wizardStatus.state === 'preparing';
      if (prepareButton && prepareBlocked !== enabled) {
        prepareBlocked = enabled;
        prepareButton.disabled = prepareBlocked;
      }
      var blockLogin = enabled || !loginReady;
      if (loginButton && loginBlocked !== blockLogin) {
        loginBlocked = blockLogin;
        loginButton.disabled = loginBlocked;
      }
      loginAvailability.textContent = enabled
        ? _('如需重新登录，请先在总览关闭 imOK。')
        : !loginReady
          ? _('先准备登录环境，再进行邮箱登录。')
          : '';
      if (!setupInitialized && accountReady) {
        if (!document.activeElement || !setup.contains(document.activeElement)) setup.open = false;
        setupInitialized = true;
      }
      wizardNext.hidden = !accountReady || !!config.exit_id;
      wizardSummary.textContent = directoryPending
        ? _('正在检查账户。')
        : accountReady
          ? _('账户已就绪')
          : registeredHere || catalog.last_success_at
            ? _('暂时无法读取账户，可重新检查。')
            : _('账户待确认。已有账户可直接检查，首次使用请按下方步骤登录。');
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
                  placeholder: 'com.cloudflare.warp://…',
                  'aria-label': _('登录成功页的完整登录链接')
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
                    '打开下方地址，用组织允许的邮箱完成验证。浏览器提示打开本机 Cloudflare 应用时取消；复制成功页的 com.cloudflare.warp:// 登录链接，粘贴到此处。'
                  )
                ),
                E('a', { href: url.trim(), target: '_blank', rel: 'noopener' }, url.trim()),
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

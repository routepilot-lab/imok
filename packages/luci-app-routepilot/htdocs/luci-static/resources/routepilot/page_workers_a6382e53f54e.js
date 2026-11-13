'use strict';
'require baseclass';

return baseclass.extend({
  render: function (
    centerGroup,
    workerInventory,
    openExistingNode,
    workerWorkspaceView,
    workerStage,
    cloudMeta,
    cloudCard,
    relayCard,
    scanCard,
    workerBackupCard
  ) {
    return centerGroup(
      'workers',
      _('Workers 备用与 Cloudflare API 授权'),
      _('检测通过后手动启用备用。'),
      [
        E(
          'div',
          { class: 'rp-layout-status', 'data-tone': 'info' },
          E(
            'a',
            {
              href: '#',
              click: openExistingNode
            },
            _('导入或检测已有节点 →')
          )
        ),
        workerWorkspaceView([
          workerStage(
            'deploy',
            _('创建部署与 API 授权'),
            cloudCard,
            !(cloudMeta && cloudMeta.configured)
          ),
          workerStage('relay', _('PROXYIP 主用与备用'), relayCard, false),
          workerStage('scan', _('优选 Cloudflare 入口'), scanCard, false),
          workerStage(
            'use',
            _('导入、检测与启用'),
            workerBackupCard,
            !!(cloudMeta && cloudMeta.configured)
          )
        ]),
        workerInventory
      ]
    );
  },
  create: function ({
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
    getStatus
  }) {
    var thisPage = this;
    var workerStages = {},
      workerBadges = {},
      workerStageLinks = {},
      workerStageStates = {},
      workerWorkspace = null,
      workerPinned = false,
      workerFlow = {};
    var workerActions = {},
      workerDisplays = {},
      workerPanels = {},
      workerMessages = {},
      workerActivity = E('div', {
        class: 'rp-worker-activity',
        role: 'status',
        'aria-live': 'polite'
      }),
      workerProjectRows = (data[5] && data[5].cloud && data[5].cloud.projects) || [],
      workerProjectsAt = Number((data[5] && data[5].cloud && data[5].cloud.projects_read_at) || 0),
      cloudDirectoryJob = null;
    var backupNode = null,
      backupTest = null,
      backupBusy = false,
      backupRequested = 0,
      backupAutoEnable = null,
      backupSwitch = null;
    var backupInfo = E('p', { role: 'status' }, _('正在读取备用状态…')),
      backupResult = E('div'),
      backupProgress = E('p', { class: 'rp-help' });
    var backupSource = E('select', { 'aria-label': _('备用节点来源') }, [
      E('option', { value: 'current' }, _('保留已配置备用，只调整入口')),
      E('option', { value: 'saved' }, _('从原有节点读取（不修改原配置）')),
      E('option', { value: 'uri' }, _('粘贴 VLESS＋WS＋TLS 链接'))
    ]);
    var backupSaved = E(
      'select',
      { 'aria-label': _('已有备用节点') },
      [E('option', { value: '' }, _('请选择节点，不会自动选择'))].concat(
        ((data[5] && data[5].nodes) || []).map(function (n) {
          return E('option', { value: n.id }, n.name + ' · ' + n.address + ':' + n.port);
        })
      )
    );
    var backupURI = E('input', {
      type: 'password',
      autocomplete: 'off',
      placeholder: 'vless://…',
      'aria-label': _('私人备用节点链接')
    });
    var backupEntry = E('select', { 'aria-label': _('备用入口方式') }, [
      E('option', { value: 'saved' }, _('保留原有入口地址与端口')),
      E('option', { value: 'domain' }, _('直接连接节点域名 · 443')),
      E('option', { value: 'ip' }, _('指定优选 IPv4（保持域名证书验证）'))
    ]);
    var backupIP = E('input', {
      type: 'text',
      placeholder: _('公网 IPv4 地址'),
      'aria-label': _('优选入口 IPv4')
    });
    var backupURIBox = E('div', { hidden: true }, [
      E('p', { class: 'rp-help' }, _('链接只保存在路由器私有文件，不进入普通配置导出。')),
      backupURI
    ]);
    var backupSavedBox = E('div', { hidden: true }, backupSaved),
      backupIPBox = E('div', { hidden: true }, backupIP);
    backupSource.addEventListener('change', function () {
      backupURIBox.hidden = backupSource.value !== 'uri';
      backupSavedBox.hidden = backupSource.value !== 'saved';
    });
    backupEntry.addEventListener('change', function () {
      backupIPBox.hidden = backupEntry.value !== 'ip';
    });
    var backupImport = E(
      'button',
      {
        class: 'btn',
        click: function () {
          workerActionStage('use');
          startWorkerOperation('use', _('保存备用节点'), 'backup', null, backupTest);
          backupBusy = true;
          updateBackup(backupNode, backupTest, getStatus());
          var input = { source: backupSource.value, entry: backupEntry.value };
          if (input.source === 'uri') input.uri = backupURI.value;
          else if (input.source === 'saved') input.id = backupSaved.value;
          if (input.entry === 'ip') input.ip = backupIP.value;
          backupURI.value = '';
          return submitInput('backup-input', 'backup-import', input)
            .then(function () {
              backupInfo.textContent = _('备用节点已保存；尚未切换，下一步请独立检测。');
              backupTest = null;
              acceptWorkerOperation(
                'use',
                { message: _('✓ 节点已保存，尚未启用。可在本组顶部“当前保存的备用节点”查看。') },
                false
              );
              return refreshWorkerMetadata();
            })
            .catch(function (e) {
              failWorkerOperation('use', e.message);
              notify(e.message, true);
            })
            .finally(function () {
              backupBusy = false;
              updateWorkerProgress();
            });
        }
      },
      _('保存备用节点（不切换）')
    );
    function startBackupProbe(autoEnable) {
      workerActionStage('use');
      startWorkerOperation('use', _('独立检测与测速'), 'backup', null, backupTest);
      backupBusy = true;
      backupRequested = Date.now();
      if (autoEnable) {
        backupAutoEnable = { node: backupNode.fingerprint, started: backupRequested, job: null };
        backupProgress.textContent = _('正在检测备用线路，通过后继续启用…');
      }
      updateBackup(backupNode, backupTest, getStatus());
      return command('backup-test')
        .then(function (raw) {
          var result = JSON.parse(raw);
          if (autoEnable && backupAutoEnable) {
            if (!result.job_id) throw Error(_('未收到本次检测编号，请重新检测。'));
            backupAutoEnable.job = result.job_id;
          }
          acceptWorkerOperation('use', result, true);
        })
        .catch(function (e) {
          backupAutoEnable = null;
          failWorkerOperation('use', e.message);
          notify(e.message, true);
          backupRequested = 0;
        })
        .finally(function () {
          backupBusy = false;
          updateWorkerProgress();
        });
    }
    var backupProbe = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return startBackupProbe(false);
        }
      },
      _('独立检测与测速（约 1 MiB）')
    );
    function switchBackup(action) {
      startWorkerOperation(
        'use',
        action === 'backup-enable' ? _('手动启用备用') : _('切回主线路'),
        'switch',
        null,
        getStatus()
      );
      backupBusy = true;
      updateBackup(backupNode, backupTest, getStatus());
      return command(action)
        .then(function (raw) {
          var r = JSON.parse(raw);
          uci.set('routepilot', 'main', 'backend', r.backend);
          uci.set('routepilot', 'main', 'apply_nonce', r.nonce);
          backupSwitch = { nonce: r.nonce, backend: r.backend, started: Date.now() };
          backupProgress.textContent = _('已提交手动切换，等待实际状态确认…');
          acceptWorkerOperation('use', { message: backupProgress.textContent }, false);
          workerActions.use.waitingSwitch = true;
        })
        .catch(function (e) {
          failWorkerOperation('use', e.message);
          notify(e.message, true);
        })
        .finally(function () {
          backupBusy = false;
          updateWorkerProgress();
        });
    }
    var backupEnable = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          return backupProof(backupTest, backupNode)
            ? switchBackup('backup-enable')
            : startBackupProbe(true);
        }
      },
      _('手动启用备用')
    );
    var backupDisable = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return switchBackup('backup-disable');
        }
      },
      _('切回主线路')
    );
    var backupActionHelp = E('p', { class: 'rp-help', role: 'status' });
    var workerBackupCard = E('section', { class: 'cbi-section rp-backup-card' }, [
      E('h3', {}, _('备用节点与独立检测 / 手动使用')),
      E('p', {}, _('独立检测通过后，可手动启用备用。')),
      E('details', { class: 'rp-stat-details' }, [
        E('summary', {}, _('查看线路路径')),
        E('p', {}, _('原主线路：路由器 → Cloudflare WARP → 账户所选出口 → 网站')),
        E(
          'p',
          {},
          _('新备用：路由器 → Cloudflare 入口 → 自己的 Workers → 网站（部分目标使用公共转发）')
        )
      ]),
      backupInfo,
      workerPanel('use'),
      E('section', { class: 'rp-guide-step' }, [
        E('h4', { class: 'rp-guide-heading' }, _('备用节点设置（手动导入 / 调整入口）')),
        E('div', { class: 'rp-guide-body' }, [
          backupSource,
          backupSavedBox,
          backupURIBox,
          E('p', { class: 'rp-help' }, _('优选只改变连接入口，不决定最终出口或国家。')),
          backupEntry,
          backupIPBox,
          E('p', {}, backupImport)
        ])
      ]),
      E('p', { class: 'rp-help' }, _('备用仅代理 TCP，所选代理 UDP 会阻断。')),
      E('div', { class: 'rp-backup-actions' }, [backupProbe, backupEnable, backupDisable]),
      backupActionHelp,
      backupProgress,
      backupResult,
      helpDetails(
        _(
          '使用自己的 Workers 即可部署备用，无需自备 VPS。优选改变连接入口，实际出口请检测确认。连接会验证 TLS 证书。代理 DNS 经备用线路使用 dns.google 加密解析。检测约用 1 MiB，结果在本次开机内有效 5 分钟。重启后请重新检测。下载和限流结果供参考，线路可用性由整项检测判定。更换路由器时请重新导入备用节点凭据。'
        )
      )
    ]);
    function backupProof(test, node) {
      return !!(
        test &&
        test.usable === true &&
        test.state === 'done' &&
        node &&
        test.fingerprint === node.fingerprint &&
        Date.now() / 1000 - test.finished_at >= 0 &&
        Date.now() / 1000 - test.finished_at <= 300 &&
        Array.isArray(test.results) &&
        test.results.length === 5
      );
    }
    function updateBackup(node, test, st) {
      var initialNode = (data[5] && data[5].node) || null;
      node = node || initialNode;
      if (node && initialNode && node.fingerprint === initialNode.fingerprint)
        node = Object.assign({}, initialNode, node);
      backupNode = node;
      backupTest = test;
      var active =
          st && st.backend === 'worker' && feedback.fresh(st, Date.now()) && st.state === 'running',
        chosen = uci.get('routepilot', 'main', 'backend') === 'worker';
      var proof = backupProof(test, node);
      var newJob = test && Number(test.started_at) * 1000 >= backupRequested - 1000,
        remoteTest =
          test &&
          ['queued', 'running'].indexOf(test.state) >= 0 &&
          Date.now() / 1000 - Number(test.started_at) < 100,
        busyTest =
          remoteTest ||
          (backupRequested &&
            Date.now() - backupRequested < 100000 &&
            (!newJob || test.state === 'running' || test.state === 'queued'));
      if (backupRequested && newJob && test.state !== 'running' && test.state !== 'queued')
        backupRequested = 0;
      if (backupSwitch) {
        if (
          st &&
          feedback.fresh(st, Date.now()) &&
          st.applied_nonce === backupSwitch.nonce &&
          st.state === 'running' &&
          st.backend === backupSwitch.backend
        ) {
          backupProgress.textContent = _('手动切换已生效。');
          acceptWorkerOperation('use', { message: backupProgress.textContent }, false);
          workerActions.use.waitingSwitch = false;
          backupSwitch = null;
        } else if (st && st.state === 'error' && st.attempted_nonce === backupSwitch.nonce) {
          backupProgress.textContent = _('切换失败：') + st.message;
          failWorkerOperation('use', backupProgress.textContent);
          backupSwitch = null;
        } else if (Date.now() - backupSwitch.started > 90000) {
          backupProgress.textContent = _('尚未收到切换确认，请查看总览；不会把已提交当作已成功。');
          failWorkerOperation('use', backupProgress.textContent, 'unknown');
          backupSwitch = null;
        }
      }
      backupInfo.textContent =
        (active
          ? _('当前使用备用线路。')
          : st && feedback.fresh(st, Date.now()) && st.state === 'running'
            ? _('当前使用主线路，备用未接管。')
            : st && feedback.fresh(st, Date.now()) && st.state === 'disabled'
              ? _('本插件已关闭，未接管网络。')
              : chosen
                ? _('已选择备用，实际运行状态尚未确认。')
                : _('当前线路状态尚未确认；保存的选择为主线路。')) +
        (node && node.configured
          ? _(' 已保存：') +
            node.name +
            ' · ' +
            node.address +
            ':' +
            node.port +
            ' · ' +
            _('验证域名：') +
            node.sni
          : _(' 尚未配置备用节点。'));
      backupImport.disabled = !!(
        backupBusy ||
        workerOperationBusy('use') ||
        backupSwitch ||
        chosen
      );
      backupProbe.disabled = !!(
        backupBusy ||
        workerOperationBusy('use') ||
        backupSwitch ||
        busyTest ||
        !node ||
        !node.configured
      );
      var canEnable =
        !active &&
        node &&
        node.configured &&
        st &&
        feedback.fresh(st, Date.now()) &&
        (st.state === 'running' || (st.state === 'error' && st.fallback === 'direct'));
      if (backupAutoEnable && !backupBusy) {
        var intent = backupAutoEnable;
        var finished =
          test && test.job_id === intent.job && ['queued', 'running'].indexOf(test.state) < 0;
        if (
          !node ||
          node.fingerprint !== intent.node ||
          Date.now() - intent.started > 100000 ||
          finished
        ) {
          backupAutoEnable = null;
          if (finished && proof && canEnable && node.fingerprint === intent.node)
            return switchBackup('backup-enable');
          backupProgress.textContent = _('未启用备用，请查看检测结果并重试。');
          failWorkerOperation('use', backupProgress.textContent);
        }
      }
      backupEnable.disabled = !!(
        backupBusy ||
        workerOperationBusy('use') ||
        backupSwitch ||
        busyTest ||
        backupAutoEnable ||
        !canEnable
      );
      backupDisable.disabled = !!(backupBusy || backupSwitch || !chosen);
      backupActionHelp.textContent =
        backupBusy || workerOperationBusy('use') || backupSwitch
          ? _('等待当前备用操作完成。')
          : !node || !node.configured
            ? _('请先保存备用节点。')
            : busyTest || backupAutoEnable
              ? _('请等待本次检测完成。')
              : chosen
                ? _('切回主线路后可更改备用节点。')
                : !canEnable
                  ? _('请先开启 imOK 并确认当前线路状态。')
                  : !proof
                    ? _('启用时先重新检测。')
                    : _('备用启用后可切回主线路。');
      workerFlow.tested = !!proof;
      workerFlow.testing = !!busyTest;
      backupProbe.setAttribute('class', !proof ? 'btn cbi-button-action' : 'btn');
      backupEnable.setAttribute('class', 'btn cbi-button-action');
      backupEnable.setAttribute('title', proof ? '' : _('先重新检测，通过后继续启用'));
      backupResult.replaceChildren();
      if (busyTest) {
        backupResult.appendChild(E('p', {}, _('正在独立检测，最长约 90 秒；主线路未切换…')));
      }
      if (!test) return;
      var expired = !proof;
      backupResult.appendChild(
        E(
          'p',
          { class: 'rp-help' },
          _('备用检测时间：') +
            when(test.finished_at) +
            (busyTest
              ? _(' · 检测进行中，已完成 ') + (test.results || []).length + '/5'
              : expired
                ? _(' · 未通过、已过期或节点已改变，请重新检测')
                : (test.results || []).some(function (r) {
                      return r.state !== 'ok';
                    })
                  ? _(' · 基础连通可用，但部分项目未通过，请查看具体结果')
                  : _(' · 本次检测全部通过，可手动启用')) +
            (test.entry_ip ? _(' · 本次入口 IP：') + test.entry_ip : '') +
            (test.exit_ip ? _(' · 本次观测出口：') + test.exit_ip + _('（可能随连接变化）') : '')
        )
      );
      var names = {
        trace: _('出口观测'),
        google: 'Google',
        github: 'GitHub',
        dns: _('备用加密 DNS'),
        download: _('1 MiB 下载')
      };
      (test.results || []).forEach(function (r) {
        backupResult.appendChild(
          E(
            'p',
            { class: 'rp-help' },
            names[r.id] +
              '：' +
              (r.state === 'ok'
                ? '✓ ' + _('通过')
                : r.state === 'restricted'
                  ? '! ' + _('站点返回 HTTP ') + r.http + ' · ' + _('访问受限 / 限流')
                  : '✕ ' + _('未通过')) +
              (r.message ? ' · ' + r.message : '') +
              (r.attempt_count > 1 ? ' · ' + _('尝试次数：') + r.attempt_count : '') +
              (r.ms != null ? ' · ' + r.ms + ' ms' : '') +
              (r.id === 'download' && r.state === 'ok' && r.ms > 0
                ? ' · ' +
                  ((r.bytes * 8) / r.ms / 1000).toFixed(2) +
                  ' Mbps（' +
                  _('短测，非带宽上限') +
                  '）'
                : '')
          )
        );
      });
    }
    var relayMeta = (data[5] && data[5].relays) || {},
      savedRelayRows = relayMeta.candidates || [
        {
          address: 'proxyip.cmliussss.net:443',
          source: 'cmliu 上游文档示例；未作维护方背书',
          source_url: 'https://github.com/cmliu/edgetunnel',
          enabled: true
        }
      ],
      relaySavedHash = relayMeta.order_hash || null;
    var relayModels = [],
      relayDirty = false,
      relayBusy = false,
      relayTest = null,
      relayObserved = null;
    var relayInfo = E(
        'p',
        { role: 'status', 'aria-live': 'polite' },
        _('候选配置与检测结果正在读取…')
      ),
      relayRows = E('div'),
      relayRuntime = E('div');
    function markRelayDirty() {
      relayDirty = true;
      relayInfo.textContent = _('有尚未保存的候选修改；保存后才能逐项检测或应用。');
      refreshRelayButtons();
    }
    function relayModel(row) {
      var m = {
        address: E('input', {
          type: 'text',
          value: row.address || '',
          placeholder: 'proxy.example.com:443',
          'aria-label': _('PROXYIP 地址')
        }),
        source: E('input', {
          type: 'text',
          value: row.source || '用户提供',
          'aria-label': _('PROXYIP 来源说明')
        }),
        link: E('input', {
          type: 'url',
          value: row.source_url || '',
          placeholder: 'https://…',
          'aria-label': _('PROXYIP 公开来源链接')
        }),
        enabled: E('input', { type: 'checkbox', 'aria-label': _('启用此候选参与主备') })
      };
      m.enabled.checked = row.enabled !== false;
      [m.address, m.source, m.link].forEach(function (input) {
        input.addEventListener('input', markRelayDirty);
      });
      m.enabled.addEventListener('change', function () {
        markRelayDirty();
        renderRelayRows();
      });
      return m;
    }
    function relayValues() {
      return relayModels.map(function (m) {
        return {
          address: String(m.address.value || '').trim(),
          source: String(m.source.value || '').trim(),
          source_url: String(m.link.value || '').trim(),
          enabled: !!m.enabled.checked
        };
      });
    }
    function relayRowProof(address) {
      return relayTest && Array.isArray(relayTest.results)
        ? relayTest.results.filter(function (r) {
            return r.address === address.toLowerCase();
          })[0]
        : null;
    }
    function relayCurrent(row) {
      var node = backupNode || (data[5] && data[5].node);
      return (
        row &&
        row.profile === 'cf-relay-v1' &&
        node &&
        row.fingerprint === node.relay_fingerprint &&
        Date.now() / 1000 - Number(row.finished_at) >= -5 &&
        Date.now() / 1000 - Number(row.finished_at) <= 300
      );
    }
    function relayTaskRunning() {
      return (
        relayTest &&
        ['queued', 'running'].indexOf(relayTest.state) >= 0 &&
        Date.now() / 1000 - Number(relayTest.started_at) < 260
      );
    }
    var relayNextAction = E('p', { class: 'rp-help', role: 'status' });
    function refreshRelayButtons() {
      var running = relayBusy || workerOperationBusy('relay') || relayTaskRunning(),
        chosen = uci.get('routepilot', 'main', 'backend') === 'worker';
      relaySave.disabled = !!(running || chosen);
      relayAdd.disabled = !!(running || relayModels.length >= 6);
      relayProbe.disabled = !!(running || relayDirty || !backupNode);
      relayStatus.disabled = !!running;
      var enabled = relayValues().filter(function (r) {
        return r.enabled;
      });
      relayApply.disabled = !!(
        running ||
        relayDirty ||
        chosen ||
        !relayTest ||
        relayTest.state !== 'done' ||
        !enabled.length ||
        enabled.some(function (r) {
          var proof = relayRowProof(r.address);
          return !relayCurrent(proof) || proof.state !== 'ok' || proof.forced !== true;
        })
      );
      relayNextAction.textContent = running
        ? _('等待当前转发操作完成。')
        : chosen
          ? _('切回主线路后可修改备用顺序。')
          : relayDirty
            ? _('先保存候选，再开始检测。')
            : !backupNode
              ? _('先保存可供检测的 Workers 备用节点。')
              : relayApply.disabled
                ? _('启用的候选全部通过本次检测后才能应用。')
                : '';
      if (relayModels.length >= 6)
        relayNextAction.textContent = [
          relayNextAction.textContent,
          _('已满 6 项，移除一项后可添加。')
        ]
          .filter(Boolean)
          .join(' ');
      var primary = relayDirty ? relaySave : relayApply.disabled ? relayProbe : relayApply;
      [relaySave, relayProbe, relayApply].forEach(function (button) {
        button.setAttribute('class', button === primary ? 'btn cbi-button-action' : 'btn');
      });
    }
    function renderRelayRows() {
      relayRows.replaceChildren();
      var enabledRank = 0;
      relayModels.forEach(function (m, index) {
        var rank = m.enabled.checked ? ++enabledRank : 0,
          proof = relayRowProof(String(m.address.value || '').trim()),
          fresh = relayCurrent(proof),
          info = E('p', { class: 'rp-help' });
        info.textContent = !proof
          ? _('尚未强制检测此候选')
          : !fresh
            ? _('历史检测已过期或节点改变，请重新检测')
            : proof.state === 'ok'
              ? _('✓ 强制转发两个 Cloudflare 目标通过')
              : _('✕ 未通过，不会用直连或其他候选掩盖');
        if (proof) {
          info.textContent += ' · ' + _('检测时间：') + when(proof.finished_at);
          (proof.checks || []).forEach(function (check, n) {
            info.textContent +=
              ' · ' +
              (n === 0 ? 'Cloudflare 官网' : 'Cloudflare 测速服务') +
              ': HTTP ' +
              check.http +
              ' / ' +
              check.ms +
              ' ms' +
              (check.curl_code ? ' / curl ' + check.curl_code : '');
          });
          if (proof.download)
            info.textContent +=
              ' · ' +
              (proof.download.ok
                ? _('64 KiB 下载完整')
                : proof.download.http === 429
                  ? _('测速站点限流')
                  : _('下载未通过'));
        }
        function move(delta) {
          var next = index + delta;
          if (next < 0 || next >= relayModels.length) return;
          relayModels.splice(index, 1);
          relayModels.splice(next, 0, m);
          markRelayDirty();
          renderRelayRows();
        }
        var up = E(
            'button',
            {
              class: 'btn',
              disabled: index === 0,
              click: function () {
                move(-1);
              }
            },
            _('上移')
          ),
          down = E(
            'button',
            {
              class: 'btn',
              disabled: index === relayModels.length - 1,
              click: function () {
                move(1);
              }
            },
            _('下移')
          );
        var remove = E(
          'button',
          {
            class: 'btn',
            disabled: relayModels.length === 1,
            click: function () {
              relayModels.splice(index, 1);
              markRelayDirty();
              renderRelayRows();
            }
          },
          _('移除')
        );
        up.disabled = index === 0;
        down.disabled = index === relayModels.length - 1;
        remove.disabled = relayModels.length === 1;
        var sourceLink = String(m.link.value || '').trim(),
          sourceNote = E(
            'p',
            { class: 'rp-help' },
            _('来源为填写者声明，检测通过不代表维护方背书或长期可靠。')
          );
        if (/^https:\/\//i.test(sourceLink))
          sourceNote.appendChild(
            E(
              'a',
              { href: sourceLink, target: '_blank', rel: 'noopener noreferrer' },
              _(' 查看公开来源 ↗')
            )
          );
        relayRows.appendChild(
          E('section', { class: 'rp-relay-row' }, [
            E('h4', {}, rank === 1 ? _('主用') : rank > 1 ? _('备用 ') + (rank - 1) : _('未启用')),
            E('label', { class: 'rp-inline-check' }, [m.enabled, _('启用，按此顺序参与主备')]),
            E('div', { class: 'rp-cloud-grid' }, [
              cloudField(_('转发主机:端口'), m.address),
              cloudField(_('来源说明'), m.source),
              cloudField(
                _('公开来源链接（可选）'),
                m.link,
                _('只填写公开资料链接，不填私人订阅、账户或口令。')
              )
            ]),
            sourceNote,
            info,
            E('div', { class: 'rp-backup-actions' }, [up, down, remove]),
            E(
              'p',
              { class: 'rp-help' },
              relayModels.length === 1
                ? _('至少保留一项候选。')
                : index === 0
                  ? _('这项已在列表顶部。')
                  : index === relayModels.length - 1
                    ? _('这项已在列表底部。')
                    : ''
            )
          ])
        );
      });
      refreshRelayButtons();
    }
    function saveRelayRows() {
      var rows = relayValues();
      return submitInput('relay-input', 'relay-save', { candidates: rows }).then(function (result) {
        savedRelayRows = result.candidates;
        relaySavedHash = result.order_hash;
        relayDirty = false;
        cloudRelay.value = savedRelayRows
          .filter(function (r) {
            return r.enabled;
          })
          .map(function (r) {
            return r.address;
          })
          .join(',');
        relayInfo.textContent = result.message;
        return result;
      });
    }
    function relayAction(action) {
      workerActionStage('relay');
      startWorkerOperation(
        'relay',
        {
          save: _('保存候选与顺序'),
          test: _('逐项强制检测 PROXYIP'),
          apply: _('应用已通过的主备顺序'),
          status: _('读取实例切换记录')
        }[action],
        'relay',
        null,
        relayTest
      );
      relayBusy = true;
      refreshRelayButtons();
      return (action === 'save' ? saveRelayRows() : command('relay-' + action))
        .then(function (raw) {
          var r = typeof raw === 'string' ? JSON.parse(raw) : raw;
          acceptWorkerOperation('relay', r, action === 'test');
          if (action === 'apply') return refreshWorkerMetadata();
        })
        .catch(function (e) {
          failWorkerOperation('relay', e.message);
          notify(e.message, true);
        })
        .finally(function () {
          relayBusy = false;
          refreshRelayButtons();
          updateWorkerProgress();
        });
    }
    var relayAdd = E(
      'button',
      {
        class: 'btn',
        click: function () {
          relayModels.push(relayModel({ enabled: true }));
          markRelayDirty();
          renderRelayRows();
        }
      },
      _('添加候选')
    );
    var relaySave = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return relayAction('save');
        }
      },
      _('保存候选与顺序（不切换）')
    );
    var relayProbe = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return relayAction('test');
        }
      },
      _('逐项强制检测 PROXYIP')
    );
    var relayApply = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return relayAction('apply');
        }
      },
      _('应用已通过的主备顺序（不启用）')
    );
    var relayStatus = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return relayAction('status');
        }
      },
      _('查询 Worker 实例切换记录')
    );
    var relayCard = E('section', { class: 'cbi-section rp-backup-card' }, [
      E('h3', {}, _('公共转发 PROXYIP · 主用与备用')),
      E('p', { class: 'rp-help' }, _('保存候选，全部通过检测后应用顺序。')),
      relayInfo,
      workerPanel('relay'),
      relayRows,
      E('div', { class: 'rp-backup-actions' }, [
        relayAdd,
        relaySave,
        relayProbe,
        relayApply,
        relayStatus
      ]),
      relayNextAction,
      relayRuntime,
      helpDetails(
        _(
          '最多保存 6 项。启用项按列表顺序作为主用和备用。逐项用两个 Cloudflare 目标验证 HTTPS 转发，整条线路另测 Google 和 GitHub。连接失败时按顺序尝试下一项，失败项暂避 30 秒。检测结果有效 5 分钟，已中断的连接需重新建立。应用顺序后可回到最后一步检测和启用备用。'
        )
      )
    ]);
    relayModels = savedRelayRows.map(relayModel);
    function updateRelays(test, observed) {
      relayTest = test;
      relayObserved = observed;
      if (!relayDirty && !relayBusy) {
        var node = backupNode || (data[5] && data[5].node),
          applied = node && relaySavedHash && node.relay_order_hash === relaySavedHash;
        relayInfo.textContent = relayTaskRunning()
          ? _('正在强制逐项检测，已完成 ') +
            (test.results || []).length +
            '/' +
            (test.total || savedRelayRows.length)
          : test && test.state === 'failed'
            ? test.message
            : applied
              ? _('已在当前备用节点固定此主备顺序；主线路未改变。')
              : _('候选已列出；尚未在当前备用节点应用此顺序。');
      }
      if (!relayDirty && !(relayRows.contains && relayRows.contains(document.activeElement)))
        renderRelayRows();
      else refreshRelayButtons();
      relayRuntime.replaceChildren();
      if (observed) {
        relayRuntime.appendChild(
          E(
            'p',
            { class: 'rp-help' },
            (Date.now() / 1000 - observed.time > 60 ? _('历史：') : '') +
              (observed.message || _('未取得实例状态说明')) +
              ' · ' +
              when(observed.time)
          )
        );
        Object.keys(observed.observed || {}).forEach(function (key) {
          var h = observed.observed[key];
          relayRuntime.appendChild(
            E(
              'p',
              { class: 'rp-help' },
              key +
                ' · ' +
                _('连接成功 ') +
                (h.successes || 0) +
                ' / ' +
                _('失败 ') +
                (h.failures || 0) +
                ' · ' +
                ({
                  connected: _('最近连接成功'),
                  connect_failed: _('最近连接失败'),
                  stream_failed: _('传输中断')
                }[h.last] || _('未知'))
            )
          );
        });
      }
    }

    var cloudMeta = (data[5] && data[5].cloud) || {},
      cloudBusy = false,
      cloudState = null,
      cloudDirectory = cloudMeta.directory || null,
      cloudDirectoryAuto = false,
      cloudZones = [],
      cloudZonesFor = '',
      cloudAccountKey = '',
      cloudZoneKey = '',
      scanState = null,
      scanBusy = false;
    var cloudInfo = E('p', { role: 'status', 'aria-live': 'polite' }),
      cloudSummary = E('p', { class: 'rp-help' }),
      cloudResult = workerPanel('deploy');
    function cloudField(label, control, help) {
      return E(
        'label',
        { class: 'rp-cloud-field' },
        [
          E('span', {}, label),
          control,
          help ? E('small', { class: 'rp-help' }, help) : null
        ].filter(function (item) {
          return item != null;
        })
      );
    }
    var cloudToken = E('input', {
      type: 'password',
      autocomplete: 'new-password',
      'aria-label': _('Cloudflare API 令牌'),
      placeholder: cloudMeta.token_saved
        ? _('已保存；留空保留原令牌')
        : _('仅填写受限 API 令牌，不使用 Global API Key')
    });
    var cloudAccount = E('input', {
      type: 'text',
      value: cloudMeta.account_id || '',
      'aria-label': _('Cloudflare 账户 ID'),
      placeholder: _('32 位账户 ID')
    });
    var cloudAccountChoice = E('select', { 'aria-label': _('选择已授权的 Cloudflare 账户') }, [
      E(
        'option',
        { value: cloudMeta.account_id || '' },
        cloudMeta.account_id
          ? _('已保存账户 · ') + cloudMeta.account_id.slice(0, 8)
          : _('先读取已授权账户，或手工填写账户 ID')
      )
    ]);
    var cloudName = E('input', {
      type: 'text',
      value: cloudMeta.script || 'routepilot-backup',
      'aria-label': _('Worker 名称')
    });
    var cloudHost = E('input', {
        type: 'text',
        readonly: true,
        value: cloudMeta.hostname || '',
        'aria-label': _('绑定的子域名'),
        placeholder: _('选择根域名后自动组成')
      }),
      cloudPrefix = E('input', {
        type: 'text',
        value: cloudMeta.hostname ? cloudMeta.hostname.split('.')[0] : 'backup',
        'aria-label': _('子域名前缀'),
        placeholder: 'backup'
      }),
      prefixInitialized = !cloudMeta.hostname;
    var cloudZone = E('select', { 'aria-label': _('选择账户下可用的根域名') }, [
      E(
        'option',
        { value: cloudMeta.zone_id || '' },
        cloudMeta.zone_id
          ? _('已保存区域 · ') + cloudMeta.zone_id.slice(0, 8)
          : _('请先读取账户下可用域名')
      )
    ]);
    var cloudRelay = E('input', {
      type: 'text',
      value: savedRelayRows
        .filter(function (r) {
          return r.enabled;
        })
        .map(function (r) {
          return r.address;
        })
        .join(','),
      'aria-label': _('公共转发 PROXYIP')
    });
    function saveCloud() {
      var input = {
        token: cloudToken.value,
        account_id: cloudAccount.value.trim(),
        script: cloudName.value.trim(),
        hostname: cloudHost.value.trim(),
        zone_id: cloudZone.value,
        proxyip: savedRelayRows
          .filter(function (r) {
            return r.enabled;
          })
          .map(function (r) {
            return r.address;
          })
          .join(',')
      };
      cloudToken.value = '';
      return submitInput('cloud-input', 'cloud-save', input);
    }
    function cloudAction(action, saveFirst) {
      var title =
        {
          verify: saveFirst ? _('保存授权并读取账户 / 域名') : _('只读刷新账户 / 根域名'),
          target: _('检查名称与域名占用'),
          deploy: _('创建 Workers 与绑定域名'),
          check: _('只读核验部署与证书'),
          promote: _('将部署保存为备用节点'),
          forget: _('清除本机 API 授权')
        }[action] || action;
      if (action !== 'verify') workerActionStage('deploy');
      var async = ['verify', 'target', 'deploy', 'check'].indexOf(action) >= 0;
      startWorkerOperation(
        'deploy',
        title,
        action === 'verify' ? 'directory' : 'cloud',
        async ? action : null,
        action === 'verify' ? cloudDirectoryJob : cloudState
      );
      cloudBusy = true;
      updateCloud(cloudState);
      return Promise.resolve()
        .then(function () {
          return saveFirst ? saveCloud() : null;
        })
        .then(function () {
          return command('cloud-' + action);
        })
        .then(function (raw) {
          var result = JSON.parse(raw);
          acceptWorkerOperation('deploy', result, async);
          if (!async && result.saved) return refreshWorkerMetadata();
        })
        .catch(function (e) {
          failWorkerOperation('deploy', e.message);
          notify(e.message, true);
        })
        .finally(function () {
          cloudBusy = false;
          updateCloud(cloudState);
          updateWorkerProgress();
        });
    }
    var cloudSave = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return cloudAction('verify', true);
        }
      },
      _('保存令牌并读取账户 / 域名')
    );
    var cloudTarget = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return cloudAction('target', true);
        }
      },
      _('检查名称和子域名是否占用')
    );
    var cloudDeploy = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          return cloudAction('deploy', true);
        }
      },
      _('一键创建 Workers 并绑定域名')
    );
    var cloudCheck = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return cloudAction('check', false);
        }
      },
      _('检查部署与证书状态')
    );
    var cloudNextAction = E('p', { class: 'rp-help', role: 'status' });
    var cloudPromote = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return cloudAction('promote', false);
        }
      },
      _('将新部署设为备用节点（不启用）')
    );
    var cloudForget = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return cloudAction('forget', false);
        }
      },
      _('清除本机 API 令牌')
    );
    var tokenGuide = E('details', { class: 'rp-guide-step' }, [
      E('summary', {}, _('没有 API 令牌？按这里创建（新手指引）')),
      E('div', { class: 'rp-guide-body' }, [
        E('ol', {}, [
          E('li', {}, [
            E(
              'a',
              {
                href: 'https://dash.cloudflare.com/profile/api-tokens',
                target: '_blank',
                rel: 'noopener noreferrer'
              },
              _('打开 Cloudflare → 我的个人资料 → API 令牌')
            ),
            _('，选择创建令牌 → 创建自定义令牌。')
          ]),
          E(
            'li',
            {},
            _(
              '权限添加：账户 → Workers Scripts → 编辑；区域 → Zone → 读取；区域 → DNS → 读取；区域 → Workers Routes → 编辑。不要使用 Global API Key。'
            )
          ),
          E(
            'li',
            {},
            _(
              '首次绑定或更换账户时，添加账户权限 Access: Organizations, Identity Providers, and Groups → 读取。'
            )
          ),
          E('li', {}, _('账户和区域资源只选此次要部署的账户和域名。')),
          E(
            'li',
            {},
            _(
              '设置合适的到期时间。创建后复制令牌，仅粘贴到本页私密输入框，不发聊天、不放普通备份。'
            )
          ),
          E(
            'li',
            {},
            _(
              '无需先复制账户 ID：留空后读取账户。有需要时也可在高级项手工填写 32 位账户 ID。子域名请用尚未使用的名称，例如 backup.你的域名。点击保存并读取可用域名，选择根域名，再填一个未使用的子域名前缀并部署。'
            )
          )
        ]),
        helpDetails(
          _(
            '令牌保存在路由器的私有文件。请到 Cloudflare 的 API 令牌页面撤销授权，并在那里管理付费计划。'
          )
        )
      ])
    ]);
    var cloudDirectoryInfo = E('p', { class: 'rp-help', role: 'status', 'aria-live': 'polite' });
    var cloudDirectoryRefresh = E(
      'button',
      {
        class: 'btn',
        click: function () {
          cloudDirectoryAuto = true;
          return cloudAction('verify', false);
        }
      },
      _('刷新账户与根域名（只读）')
    );
    function updateDirectory(data) {
      if (data && data.state && data.started_at) cloudDirectoryJob = data;
      if (
        data &&
        Array.isArray(data.accounts) &&
        (!cloudDirectory || Number(data.read_at || 0) >= Number(cloudDirectory.read_at || 0))
      ) {
        cloudDirectory = data;
      }
      var source = cloudDirectory;
      if (cloudToken.value) {
        cloudDirectoryInfo.textContent = _('令牌尚未保存；保存后才能读取它授权的账户和域名。');
        cloudDirectoryRefresh.disabled = true;
        return;
      }
      var hasToken =
        cloudState && cloudState.configured === false
          ? false
          : cloudMeta.token_saved || (cloudState && cloudState.token_saved);
      cloudDirectoryRefresh.disabled = !!(
        cloudBusy ||
        !hasToken ||
        (data &&
          ['queued', 'running'].indexOf(data.state) >= 0 &&
          Date.now() / 1000 - Number(data.started_at) < 300)
      );
      if (!source) {
        cloudDirectoryInfo.textContent =
          data && data.state === 'failed'
            ? data.message || _('域名读取失败，请重试；不能据此认定账户没有域名。')
            : _('尚未读取可用根域名；已保存绑定保持不变。');
        return;
      }
      var account = cloudAccount.value || source.discovered_account_id || '',
        accounts = source.accounts || [];
      var accountKey = JSON.stringify(accounts) + '|' + account;
      if (accountKey !== cloudAccountKey) {
        cloudAccountKey = accountKey;
        cloudAccountChoice.replaceChildren(E('option', { value: '' }, _('请选择已授权账户')));
        accounts.forEach(function (a) {
          cloudAccountChoice.appendChild(
            E('option', { value: a.id }, a.name + ' · ' + a.id.slice(0, 8))
          );
        });
        if (
          account &&
          !accounts.some(function (a) {
            return a.id === account;
          })
        )
          cloudAccountChoice.appendChild(
            E('option', { value: account }, _('保留原账户（本次目录未确认）'))
          );
      }
      cloudAccountChoice.value = account;
      if (!cloudAccount.value && account) cloudAccount.value = account;
      var zones = Array.isArray(source.all_zones)
        ? source.all_zones.filter(function (z) {
            return z.account_id === account;
          })
        : source.discovered_account_id === account || source.account_id === account
          ? source.zones || []
          : [];
      cloudZones = zones;
      cloudZonesFor = account;
      var zoneKey = account + JSON.stringify(zones);
      if (zoneKey !== cloudZoneKey) {
        cloudZoneKey = zoneKey;
        var value =
          cloudZone.value ||
          ((cloudMeta.account_id || '') === account ? cloudMeta.zone_id : '') ||
          '';
        cloudZone.replaceChildren(E('option', { value: '' }, _('请选择根域名')));
        zones.forEach(function (z) {
          cloudZone.appendChild(E('option', { value: z.id }, z.name));
        });
        if (
          !zones.some(function (z) {
            return z.id === value;
          })
        )
          value = '';
        if (!value && zones.length === 1) value = zones[0].id;
        cloudZone.value = value;
        var chosen = zones.filter(function (z) {
          return z.id === value;
        })[0];
        if (
          !prefixInitialized &&
          chosen &&
          cloudMeta.hostname &&
          cloudMeta.hostname.endsWith('.' + chosen.name)
        ) {
          cloudPrefix.value = cloudMeta.hostname.slice(0, -chosen.name.length - 1);
          prefixInitialized = true;
        }
        if (chosen) syncCloudHostname();
      }
      cloudDirectoryInfo.textContent =
        (data && ['queued', 'running'].indexOf(data.state) >= 0
          ? _('正在只读刷新；已保存绑定不变。 ')
          : '') +
        _('当前账户可用根域名：') +
        zones.length +
        _(' 个；仅含此令牌授权范围。') +
        (source.read_at ? _(' 读取时间：') + when(source.read_at) : '') +
        (Date.now() / 1000 - Number(source.read_at) > 900 ? _(' · 历史目录，请只读刷新') : '') +
        (data && data.state === 'failed'
          ? ' · ' + (data.message || _('本次刷新失败')) + _('；保留上次目录')
          : '');
    }
    var cloudCard = E('section', { class: 'cbi-section rp-backup-card' }, [
      E('h3', {}, _('创建自己的 Workers 备用线路')),
      E('p', {}, _('授权后可创建 Worker 并绑定子域名。')),
      cloudInfo,
      cloudSummary,
      cloudResult,
      E('div', { class: 'rp-directory-toolbar' }, [cloudDirectoryRefresh, cloudDirectoryInfo]),
      tokenGuide,
      E('div', { class: 'rp-cloud-grid' }, [
        cloudField(_('私密 API 令牌'), cloudToken, _('首次填写；已保存时留空即可保留。')),
        cloudField(
          _('Cloudflare 账户'),
          cloudAccountChoice,
          _('选择「账户与协议」中团队所属的账户。首次绑定需允许读取该账户的 Zero Trust 组织。')
        ),
        cloudField(_('账户 ID（可选，手工填写）'), cloudAccount),
        cloudField(_('Worker 名称'), cloudName, _('以 routepilot- 开头；不会覆盖其他项目。')),
        cloudField(
          _('可用根域名'),
          cloudZone,
          _('显示此令牌可读取且已生效的根域名；绑定权限在占用检查和部署时另行确认。')
        ),
        cloudField(_('子域名前缀'), cloudPrefix, _('例如 backup，不填写 https:// 或根域名。')),
        cloudField(
          _('最终绑定的子域名'),
          cloudHost,
          _('系统会检查占用；不用先到 Cloudflare 添加这条 DNS。')
        )
      ]),
      helpDetails(_('在候选池保存 PROXYIP 顺序后，新部署会按该顺序使用已启用的候选。')),
      E('div', { class: 'rp-backup-actions' }, [
        cloudSave,
        cloudTarget,
        cloudDeploy,
        cloudCheck,
        cloudPromote
      ]),
      cloudNextAction,
      E('details', { class: 'rp-danger-area' }, [
        E('summary', {}, _('危险操作：清除本机授权')),
        E(
          'p',
          {},
          _(
            '仅删除路由器保存的 API 令牌和域名目录；不会删除云端资源、撤销 Cloudflare 令牌或切换当前线路。'
          )
        ),
        cloudForget
      ])
    ]);
    function updateCloud(state) {
      cloudState = state;
      if (
        state &&
        Array.isArray(state.projects) &&
        Number(state.projects_read_at || 0) >= workerProjectsAt
      ) {
        workerProjectRows = state.projects;
        workerProjectsAt = Number(state.projects_read_at || 0);
      }
      var busy =
          cloudBusy ||
          workerOperationBusy('deploy') ||
          (state &&
            ['running', 'queued'].indexOf(state.state) >= 0 &&
            Date.now() / 1000 - Number(state.started_at) < 300),
        configured = !!(cloudMeta.configured || (state && state.configured));
      if (state && state.configured === false) {
        configured = false;
        cloudMeta = { configured: false };
        cloudDirectory = null;
        cloudZones = [];
        cloudZonesFor = '';
        cloudZoneKey = '';
        cloudDirectoryInfo.textContent = _('本机 API 令牌已清除，需重新授权后读取域名。');
      }
      cloudInfo.textContent =
        state && state.message
          ? state.message
          : configured
            ? _('已保存 Cloudflare 配置，云端部署状态尚未检查。')
            : _('尚未保存 API 令牌；可先展开下方创建指引。');
      var saved = state && state.configured ? state : cloudMeta,
        dirty =
          !!cloudToken.value ||
          ['account_id', 'script', 'hostname', 'zone_id'].some(function (k) {
            var controls = {
              account_id: cloudAccount,
              script: cloudName,
              hostname: cloudHost,
              zone_id: cloudZone,
              proxyip: cloudRelay
            };
            return String(controls[k].value || '').trim() !== String(saved[k] || '');
          });
      cloudSummary.textContent =
        (saved.hostname
          ? _('当前保存目标：') + saved.script + ' · ' + saved.hostname + '。 '
          : '') +
        _('改动后先保存并读取区域 / 部署；HTTPS 就绪后仍需独立联网检测。') +
        (dirty ? _(' 表单有尚未保存的改动。') : '');
      updateDirectory(
        state && (Array.isArray(state.accounts) || state.directory)
          ? state.directory || state
          : null
      );
      dirty =
        !!cloudToken.value ||
        ['account_id', 'script', 'hostname', 'zone_id'].some(function (k) {
          var controls = {
            account_id: cloudAccount,
            script: cloudName,
            hostname: cloudHost,
            zone_id: cloudZone,
            proxyip: cloudRelay
          };
          return String(controls[k].value || '').trim() !== String(saved[k] || '');
        });
      workerFlow.cloudDirty = dirty;
      workerFlow.deployed = !!(state && state.state === 'ready' && !dirty);
      var alreadySaved =
        backupNode &&
        backupNode.configured &&
        workerProjectRows.some(function (row) {
          return (
            row.saved_as_backup &&
            row.script === cloudName.value.trim() &&
            row.hostname === cloudHost.value.trim()
          );
        });
      if (alreadySaved)
        cloudSummary.textContent += _(
          ' 当前部署已保存为备用，无需重复保存；可继续配置或检测 PROXYIP。'
        );
      var primary =
        !configured || cloudToken.value
          ? cloudSave
          : alreadySaved
            ? cloudCheck
            : workerFlow.deployed
              ? cloudPromote
              : cloudDeploy;
      [cloudSave, cloudDeploy, cloudPromote, cloudCheck].forEach(function (button) {
        button.setAttribute('class', button === primary ? 'btn cbi-button-action' : 'btn');
      });
      cloudSave.disabled = !!busy;
      var validTarget =
        cloudZonesFor === cloudAccount.value.trim().toLowerCase() &&
        !cloudToken.value &&
        cloudZones.some(function (z) {
          return z.id === cloudZone.value;
        }) &&
        !!cloudHost.value;
      cloudDeploy.disabled = !!(
        busy ||
        !validTarget ||
        (workerActions.deploy &&
          workerActions.deploy.result &&
          workerActions.deploy.result.state === 'unknown')
      );
      cloudTarget.disabled = !!(busy || !validTarget);
      cloudCheck.disabled = !!(busy || !configured || dirty);
      cloudForget.disabled = !!(busy || !configured);
      cloudPromote.disabled = !!(
        alreadySaved ||
        busy ||
        dirty ||
        !state ||
        state.state !== 'ready' ||
        uci.get('routepilot', 'main', 'backend') === 'worker'
      );
      cloudNextAction.textContent = busy
        ? _('等待当前云端操作完成。')
        : !configured || cloudToken.value
          ? _('先保存 Cloudflare 授权。')
          : !validTarget
            ? _('读取账户域名并选择部署子域名。')
            : cloudDeploy.disabled
              ? _('上次部署结果未确认，请先检查云端状态。')
              : dirty
                ? _('先保存表单修改，再检查部署。')
                : alreadySaved
                  ? _('已设为备用，可继续配置 PROXYIP。')
                  : uci.get('routepilot', 'main', 'backend') === 'worker'
                    ? _('切回主线路后可更换备用节点。')
                    : cloudPromote.disabled
                      ? _('云端部署就绪后可保存为备用。')
                      : '';
    }
    function syncCloudHostname() {
      var zone = cloudZones.filter(function (z) {
        return z.id === cloudZone.value;
      })[0];
      var prefix = String(cloudPrefix.value || '')
        .trim()
        .toLowerCase();
      cloudHost.value =
        zone && /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(prefix)
          ? prefix + '.' + zone.name
          : '';
    }
    cloudPrefix.addEventListener('input', function () {
      prefixInitialized = true;
      syncCloudHostname();
      updateCloud(cloudState);
    });
    cloudAccountChoice.addEventListener('change', function () {
      cloudAccount.value = cloudAccountChoice.value;
      cloudZones = [];
      cloudZonesFor = '';
      cloudZoneKey = '';
      cloudZone.replaceChildren(E('option', { value: '' }, _('账户已变更，请重新读取域名')));
      cloudZone.value = '';
      cloudHost.value = '';
      updateDirectory(cloudDirectory);
      updateCloud(cloudState);
    });
    cloudToken.addEventListener('input', function () {
      cloudZones = [];
      cloudZonesFor = '';
      cloudZoneKey = '';
      if (!cloudToken.value) updateDirectory(cloudMeta.directory);
      else updateDirectory(null);
      updateCloud(cloudState);
    });
    cloudAccount.addEventListener('input', function () {
      cloudZoneKey = '';
      cloudZone.value = '';
      updateDirectory(cloudDirectory);
      updateCloud(cloudState);
    });
    [cloudName, cloudHost, cloudRelay].forEach(function (field) {
      field.addEventListener('input', function () {
        updateCloud(cloudState);
      });
    });
    cloudZone.addEventListener('change', function () {
      syncCloudHostname();
      updateCloud(cloudState);
    });
    updateCloud(null);
    updateDirectory(cloudDirectory);
    var scanInfo = E(
        'p',
        { role: 'status', 'aria-live': 'polite' },
        _('尚未优选；原备用入口保持不变。')
      ),
      scanRows = E('div');
    function scanAction(action) {
      workerActionStage('scan');
      startWorkerOperation(
        'scan',
        action === 'backup-scan' ? _('优选 Cloudflare 入口') : _('保存推荐备用入口'),
        'scan',
        null,
        scanState
      );
      scanBusy = true;
      updateScan(scanState);
      return command(action)
        .then(function (raw) {
          var r = JSON.parse(raw);
          acceptWorkerOperation('scan', r, action === 'backup-scan');
          if (action === 'backup-scan-apply') return refreshWorkerMetadata();
        })
        .catch(function (e) {
          failWorkerOperation('scan', e.message);
          notify(e.message, true);
        })
        .finally(function () {
          scanBusy = false;
          updateWorkerProgress();
        });
    }
    var scanStart = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return scanAction('backup-scan');
        }
      },
      _('开始自动优选（约 1～4 分钟）')
    );
    var scanNextAction = E('p', { class: 'rp-help', role: 'status' });
    var scanApply = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return scanAction('backup-scan-apply');
        }
      },
      _('应用推荐入口（不切换）')
    );
    var scanCard = E('section', { class: 'cbi-section rp-backup-card' }, [
      E('h3', {}, _('自动优选 Cloudflare 入口 IP')),
      E('p', { class: 'rp-help' }, _('用当前 Worker 测试最多 8 个入口。')),
      scanInfo,
      workerPanel('scan'),
      E('div', { class: 'rp-backup-actions' }, [scanStart, scanApply]),
      scanNextAction,
      scanRows,
      helpDetails(
        _(
          '仅推荐 Google 和 GitHub 都通过的入口，按两站延迟排序。下载和限流结果单独显示。结果有效 5 分钟，应用后请重新检测备用线路。'
        )
      )
    ]);
    function updateScan(state) {
      scanState = state;
      var busy =
        scanBusy ||
        workerOperationBusy('scan') ||
        (state &&
          ['queued', 'running'].indexOf(state.state) >= 0 &&
          Date.now() / 1000 - Number(state.started_at) < 300);
      scanStart.disabled = !!(busy || !backupNode || !backupNode.configured);
      scanApply.disabled = !!(
        busy ||
        !state ||
        state.state !== 'done' ||
        !state.best_ip ||
        Date.now() / 1000 - Number(state.finished_at) > 300 ||
        !backupNode ||
        state.fingerprint !== backupNode.fingerprint ||
        uci.get('routepilot', 'main', 'backend') === 'worker'
      );
      scanNextAction.textContent = busy
        ? _('等待本次入口优选完成。')
        : !backupNode || !backupNode.configured
          ? _('先到「导入、检测与启用」保存备用节点。')
          : uci.get('routepilot', 'main', 'backend') === 'worker'
            ? _('切回主线路后可应用备用入口。')
            : scanApply.disabled
              ? _('先取得当前节点的有效优选结果。')
              : '';
      scanStart.setAttribute('class', scanApply.disabled ? 'btn cbi-button-action' : 'btn');
      scanApply.setAttribute('class', scanApply.disabled ? 'btn' : 'btn cbi-button-action');
      if (state)
        scanInfo.textContent =
          state.message ||
          (busy
            ? _('正在优选，已完成 ') +
              (state.rows || []).length +
              '/' +
              (state.total || 8) +
              (state.progress != null ? ' · ' + state.progress + '%' : '')
            : _('等待结果'));
      scanRows.replaceChildren();
      if (!state || !Array.isArray(state.rows)) return;
      state.rows.forEach(function (r) {
        var checks = r.checks || [],
          all =
            checks.length === 2 &&
            checks.every(function (c) {
              return c.ok;
            }),
          avg = checks.length
            ? Math.round(
                checks.reduce(function (n, c) {
                  return n + c.ms;
                }, 0) / checks.length
              )
            : null;
        scanRows.appendChild(
          E(
            'p',
            { class: 'rp-help' },
            (r.ip === state.best_ip ? '★ ' : '') +
              r.ip +
              ' · ' +
              (all ? _('两站通过') : _('未完整通过')) +
              (avg != null ? ' · ' + avg + ' ms' : '') +
              (r.download && r.download.ok
                ? ' · ' + ((r.download.mibps * 8 * 1048576) / 1000000).toFixed(2) + ' Mbps（短测）'
                : r.download && r.download.warning
                  ? ' · ' + r.download.warning
                  : '')
          )
        );
      });
    }
    updateScan(null);

    updateRelays(null, null);
    var workerInventory = E('section', {
      class: 'rp-worker-inventory',
      'aria-label': _('Workers 节点与部署记录')
    });
    function openWorkerStage(key) {
      Object.keys(workerStages).forEach(function (k) {
        workerStages[k].setAttribute('data-current', k === key ? 'true' : 'false');
        if (workerStageLinks[k])
          workerStageLinks[k].setAttribute('aria-pressed', k === key ? 'true' : 'false');
      });
    }
    function workerWorkspaceView(stages) {
      var names = {
        deploy: _('部署与授权'),
        relay: _('PROXYIP 候选池'),
        scan: _('入口优选'),
        use: _('导入、检测与启用')
      };
      var nav = E(
        'nav',
        { class: 'imok-worker-nav', 'aria-label': _('Workers 阶段选择') },
        Object.keys(names).map(function (key) {
          var state = (workerStageStates[key] = E('small', {}, _('正在读取…')));
          return (workerStageLinks[key] = E(
            'button',
            {
              class: 'btn imok-stage-link',
              type: 'button',
              'aria-controls': 'imok-worker-' + key,
              'aria-pressed': 'false',
              click: function () {
                workerPinned = true;
                openWorkerStage(key);
              }
            },
            [E('span', {}, names[key]), state]
          ));
        })
      );
      var showAll = E(
        'button',
        {
          class: 'btn',
          type: 'button',
          'aria-pressed': 'false',
          click: function () {
            workerPinned = true;
            var all = showAll.getAttribute
              ? showAll.getAttribute('aria-pressed') !== 'true'
              : false;
            showAll.setAttribute('aria-pressed', all ? 'true' : 'false');
            workerWorkspace.setAttribute('data-show-all', all ? 'true' : 'false');
            showAll.textContent = all ? _('只显示当前阶段') : _('展开全部设置');
          }
        },
        _('展开全部设置')
      );
      nav.appendChild(showAll);
      workerWorkspace = E('div', { class: 'imok-worker-workspace', 'data-show-all': 'false' }, [
        nav,
        E('div', { class: 'imok-worker-content' }, stages)
      ]);
      return workerWorkspace;
    }

    function workerPanel(key) {
      if (!workerPanels[key])
        workerPanels[key] = E('div', {
          class: 'rp-operation-feedback',
          role: 'status',
          'aria-live': 'polite'
        });
      return workerPanels[key];
    }
    function workerOperationBusy(key) {
      var op = workerActions[key];
      return !!(op && (op.submitting || op.pending || op.waitingSwitch));
    }
    function startWorkerOperation(key, title, kind, action, job) {
      workerActions[key] = {
        title: title,
        kind: kind,
        action: action,
        started: Date.now(),
        submitting: true,
        pending: null,
        baseline: feedback.workerJobKey(job)
      };
      updateWorkerProgress();
    }
    function acceptWorkerOperation(key, result, async) {
      var op = workerActions[key];
      if (!op) return;
      op.submitting = false;
      op.finished = Date.now();
      if (async && result && result.state === 'submitted') {
        op.pending = { started: op.started, baseline: op.baseline, action: op.action };
      } else if (async && result && result.state === 'running') {
        op.result = {
          state: 'waiting',
          message: _('已有后台任务正在运行；本次没有重复创建或检测。请等待下方实际状态更新。')
        };
      } else if (async) {
        op.result = {
          state: 'unknown',
          message: _('提交响应未给出明确任务状态，请只读检查结果；不能据此确认创建成功。')
        };
      } else {
        op.result = {
          state: 'success',
          message: (result && result.message) || _('✓ 操作已完成并保存。')
        };
      }
      updateWorkerProgress();
    }
    function failWorkerOperation(key, message, state) {
      var op = workerActions[key] || {};
      op.submitting = false;
      op.waitingSwitch = false;
      op.pending = null;
      op.finished = Date.now();
      op.result = { state: state || 'error', message: message };
      workerActions[key] = op;
      updateWorkerProgress();
    }
    function refreshWorkerMetadata() {
      return fs.exec('/usr/libexec/routepilot-backup-list', []).then(function (r) {
        if (r.code) throw Error(_('保存操作已完成，但节点列表读取失败；请刷新页面确认。'));
        var next = JSON.parse(r.stdout);
        if (next.cloud) {
          cloudMeta = next.cloud;
          workerProjectRows = next.cloud.projects || [];
          workerProjectsAt = Number(next.cloud.projects_read_at || 0);
        }
        if (next.node) {
          backupNode = next.node;
          updateBackup(next.node, backupTest, getStatus());
        }
        updateWorkerProgress();
      });
    }
    function progressText(key, state, job, kind) {
      var elapsed = _('已等待 ') + state.elapsed + _(' 秒'),
        count = state.total ? _('已完成 ') + state.completed + '/' + state.total : '';
      if (state.state === 'busy')
        return (
          (state.phase === 'awaiting'
            ? _('请求已提交，正在等待后台接收确认')
            : state.message || _('后台正在处理')) +
          ' · ' +
          (count ? count + ' · ' : '') +
          elapsed
        );
      if (state.state === 'unknown')
        return (
          _('较长时间未收到完成确认，当前结果未知；请只读检查状态，避免反复创建。') +
          ' · ' +
          elapsed
        );
      if (state.state === 'error')
        return '✕ ' + (state.message || _('操作未完成；请检查具体原因后重试。'));
      if (state.state === 'waiting')
        return '! ' + (state.message || _('仍在等待 DNS 或证书生效；稍后只读检查，无需重复创建。'));
      if (state.state === 'warning')
        return '! ' + (state.message || _('发现冲突或访问受限，请核对后再继续。'));
      if (key === 'deploy' && state.state === 'success')
        return job && job.state === 'ready'
          ? _('✓ Worker 与域名 HTTPS 已确认就绪；这不等于备用线路已经启用。')
          : job && job.state === 'verified'
            ? _('✓ 账户和根域名读取完成；尚未因此创建节点。')
            : job && job.state === 'available'
              ? _('✓ 名称和子域名占用检查通过；尚未创建节点。')
              : _('✓ 操作完成。');
      if (key === 'relay' && job && job.state === 'done') {
        var rows = job.results || [],
          passed = rows.filter(function (r) {
            return r.state === 'ok';
          }).length,
          enabled = relayValues().filter(function (r) {
            return r.enabled;
          }),
          current =
            !relayDirty &&
            enabled.length &&
            enabled.every(function (r) {
              var proof = relayRowProof(r.address);
              return relayCurrent(proof) && proof.state === 'ok' && proof.forced === true;
            });
        state.state = current ? 'success' : 'warning';
        return (
          (current ? '✓ ' : '! ') +
          _('逐项检测结束：通过 ') +
          passed +
          '/' +
          rows.length +
          (current
            ? _('；请点击“应用已通过的主备顺序”，检测不会自动保存应用。')
            : _('；结果已过期、节点 / 候选改变或有项目未通过，请查看逐项结果。'))
        );
      }
      if (key === 'scan' && job && job.state === 'done') {
        var age = Date.now() / 1000 - Number(job.finished_at),
          current =
            backupNode &&
            age >= -5 &&
            age <= 300 &&
            (job.fingerprint === backupNode.fingerprint ||
              job.applied_fingerprint === backupNode.fingerprint);
        state.state = current && job.best_ip ? 'success' : 'warning';
        return current && job.best_ip
          ? '✓ ' +
              _('本轮推荐入口：') +
              job.best_ip +
              (job.applied_fingerprint === backupNode.fingerprint
                ? _('；已保存，下一步独立检测。')
                : _('；尚未应用。可应用推荐入口，也可保留原入口进入下一步。'))
          : '! ' + _('没有当前有效的推荐结果；历史结果或没有通过的入口不能当作已完成优化。');
      }
      if (key === 'use' && job && job.state === 'done') {
        state.state = workerFlow.tested ? 'success' : 'warning';
        return workerFlow.tested
          ? '✓ ' + _('当前节点基础联网检测通过，可手动启用；检测不会切换现有主线路。')
          : '! ' + _('独立检测已结束，但未通过、已过期或节点已变化；当前不能据此启用备用。');
      }
      return state.message || _('等待开始');
    }
    function updateWorkerInventory() {
      if (!workerInventory) return;
      var node = backupNode || (data[5] && data[5].node),
        st = getStatus(),
        running =
          feedback.fresh(st, Date.now()) && st.state === 'running' && st.backend === 'worker';
      var signature = JSON.stringify([node, workerProjectRows, running, workerFlow.tested]);
      if (signature === workerInventory._signature) return;
      workerInventory._signature = signature;
      var saved = E('div', { class: 'rp-node-summary' }, [
        E('h4', {}, _('当前保存的备用节点')),
        E(
          'p',
          {},
          node && node.configured
            ? '✓ ' + _('已保存到路由器 · ') + (node.name || node.sni || _('备用节点'))
            : _('尚未保存备用节点。可创建或导入节点。')
        )
      ]);
      if (node && node.configured) {
        saved.appendChild(
          E(
            'p',
            {},
            _('验证域名：') + node.sni + ' · ' + _('连接入口：') + node.address + ':' + node.port
          )
        );
        saved.appendChild(
          E(
            'p',
            {},
            running
              ? _('✓ 备用正在运行')
              : st && st.backend === 'worker'
                ? _('! 已选择备用，实际运行状态待确认')
                : _('○ 备用未启用，现有主线路仍在使用')
          )
        );
        saved.appendChild(
          E(
            'p',
            { class: 'rp-help' },
            workerFlow.tested ? _('✓ 当前节点有有效的独立检测结果') : _('当前节点需要重新检测。')
          )
        );
      }
      saved.appendChild(
        E(
          'button',
          {
            class: 'btn',
            click: function () {
              workerPinned = true;
              openWorkerStage('use');
            }
          },
          _('前往导入、检测与启用')
        )
      );
      var created = E('div', { class: 'rp-created-nodes' }, [
        E('h4', {}, _('本路由器创建的云端节点'))
      ]);
      if (!workerProjectRows.length)
        created.appendChild(E('p', { class: 'rp-help' }, _('暂无本路由器创建记录。')));
      workerProjectRows.forEach(function (row) {
        created.appendChild(
          E('div', { class: 'rp-created-node' }, [
            E('strong', {}, row.script),
            E('span', {}, row.hostname),
            E(
              'span',
              { class: 'rp-help' },
              (row.uploaded ? _('✓ Worker 已上传') : _('○ 已预留，上传待确认')) +
                ' · ' +
                (row.bound ? _('✓ 域名已绑定') : _('○ 域名尚未确认')) +
                ' · ' +
                (row.saved_as_backup ? _('✓ 已保存为当前备用') : _('○ 未保存为当前备用'))
            )
          ])
        );
      });
      created.appendChild(helpDetails(_('在「导入、检测与启用」检查备用线路能否联网。')));
      workerInventory.replaceChildren(
        E('h3', {}, _('备用节点与云端部署记录')),
        workerActivity,
        saved,
        created
      );
    }
    function updateWorkerProgress() {
      if (!workerInventory) return;
      var jobs = {
        deploy: cloudState || cloudDirectoryJob,
        relay: relayTest,
        scan: scanState,
        use: backupTest
      };
      ['deploy', 'relay', 'scan', 'use'].forEach(function (key) {
        var op = workerActions[key],
          job =
            (op && op.completedJob) ||
            (op && op.kind === 'directory' ? cloudDirectoryJob : jobs[key]),
          kind = op ? op.kind : key === 'deploy' ? 'cloud' : key === 'use' ? 'backup' : key;
        var display =
          op && op.submitting
            ? {
                state: 'busy',
                phase: 'submitting',
                elapsed: Math.max(0, Math.floor((Date.now() - op.started) / 1000)),
                completed: 0,
                total: 0,
                message: _('正在提交操作，请勿重复点击…')
              }
            : op && op.result
              ? Object.assign({ elapsed: 0, completed: 0, total: 0 }, op.result)
              : feedback.workerJobProgress(
                  job,
                  op && op.pending,
                  Date.now(),
                  kind,
                  key === 'use' ? 5 : 0
                );
        var message = op && op.result ? display.message : progressText(key, display, job, kind);
        if (op && op.pending && display.matched && ['busy', 'unknown'].indexOf(display.state) < 0) {
          op.pending = null;
          op.completedJob = job;
        }
        if (
          key === 'deploy' &&
          workerFlow.cloudDirty &&
          display.state === 'success' &&
          !(op && op.result)
        ) {
          display.state = 'warning';
          message = _('这是上次保存目标的结果；表单有未保存编辑，不能代表新目标已经创建。');
        }
        if (op && op.waitingSwitch) {
          display.state = 'busy';
          display.elapsed = Math.max(0, Math.floor((Date.now() - op.started) / 1000));
          message =
            _('切换请求已提交，正在等待实际运行状态确认…') +
            ' · ' +
            _('已等待 ') +
            display.elapsed +
            _(' 秒');
        }
        if (op && op.pending && display.state === 'unknown') {
          op.pending = null;
          op.result = { state: 'unknown', message: message, elapsed: display.elapsed };
        }
        if (op) op.display = display;
        workerDisplays[key] = display;
        workerMessages[key] = message;
        var panel = workerPanel(key),
          title =
            (op && op.title) ||
            {
              deploy: _('部署与授权操作结果'),
              relay: _('PROXYIP 检测 / 应用结果'),
              scan: _('入口优选结果'),
              use: _('备用节点独立检测结果')
            }[key],
          stamp =
            op && op.finished && !workerOperationBusy(key)
              ? when(op.finished / 1000)
              : job && job.finished_at && !workerOperationBusy(key)
                ? when(job.finished_at)
                : '',
          signature = JSON.stringify([title, display, message, stamp, workerFlow.tested]);
        if (panel._signature === signature) return;
        panel._signature = signature;
        panel.setAttribute('data-state', display.state);
        panel.setAttribute('aria-busy', display.state === 'busy' ? 'true' : 'false');
        var children = [E('strong', {}, title), E('p', {}, message)];
        if (display.total)
          children.push(
            E('progress', {
              max: display.total,
              value: Math.min(display.completed, display.total),
              'aria-label': _('实际已完成项目')
            })
          );
        if (
          key === 'deploy' &&
          job &&
          ['deploy', 'check'].indexOf(job.action) >= 0 &&
          !(op && op.kind === 'directory') &&
          !(op && op.result)
        ) {
          var waitingForNew = !!(op && (op.submitting || op.pending) && !display.matched);
          var steps = [
              _('项目归属与占用'),
              _('Worker 上传 / 存在'),
              _('子域名绑定'),
              _('DNS 与 HTTPS 证书')
            ],
            index = { connecting: 0, checking: 0, uploading: 1, binding: 2, waiting_dns: 3 }[
              job.phase
            ],
            ready = job.state === 'ready' && !workerFlow.cloudDirty && !waitingForNew;
          if (waitingForNew) index = -1;
          else if (job.state === 'waiting_dns') index = 3;
          children.push(
            E(
              'ol',
              { class: 'rp-job-steps' },
              steps.map(function (label, i) {
                var tone = ready
                  ? 'success'
                  : Number.isFinite(index) && i < index
                    ? 'success'
                    : i === index
                      ? display.state === 'error'
                        ? 'error'
                        : 'busy'
                      : 'pending';
                return E(
                  'li',
                  { 'data-state': tone },
                  (tone === 'success'
                    ? '✓ '
                    : tone === 'error'
                      ? '✕ '
                      : tone === 'busy'
                        ? '… '
                        : '○ ') +
                    label +
                    (i === index && display.state === 'busy' ? _(' · 正在处理') : '')
                );
              })
            )
          );
        }
        if (stamp) children.push(E('small', {}, _('结果记录时间：') + stamp));
        if (['success', 'warning', 'waiting'].indexOf(display.state) >= 0) {
          var next =
              key === 'deploy'
                ? backupNode && backupNode.configured && backupNode.sni === cloudHost.value
                  ? 'relay'
                  : 'deploy'
                : key === 'relay'
                  ? 'relay'
                  : key === 'scan'
                    ? 'use'
                    : 'use',
            text =
              key === 'deploy'
                ? next === 'relay'
                  ? _('下一步：配置 / 检测 PROXYIP')
                  : _('前往创建部署，检查或保存为备用节点')
                : key === 'relay'
                  ? relayApply && !relayApply.disabled
                    ? _('下一步：应用已通过的主备顺序')
                    : _('查看候选与逐项结果')
                  : key === 'scan'
                    ? _('保留当前入口，前往独立检测')
                    : _('查看保存节点与检测结果');
          children.push(
            E(
              'button',
              {
                class: 'btn',
                click: function () {
                  workerPinned = true;
                  openWorkerStage(next);
                }
              },
              text
            )
          );
        }
        panel.replaceChildren.apply(panel, children);
      });
      updateWorkerInventory();
      var keys = ['deploy', 'relay', 'scan', 'use'],
        recent =
          keys
            .filter(function (k) {
              return workerActions[k];
            })
            .sort(function (a, b) {
              return workerActions[b].started - workerActions[a].started;
            })[0] ||
          keys
            .filter(function (k) {
              return jobs[k] && jobs[k].started_at;
            })
            .sort(function (a, b) {
              return Number(jobs[b].started_at) - Number(jobs[a].started_at);
            })[0];
      if (recent) {
        var d = workerDisplays[recent],
          a = workerActions[recent],
          title = {
            deploy: _('部署与授权'),
            relay: _('PROXYIP'),
            scan: _('优选入口'),
            use: _('保存与独立检测')
          }[recent],
          text = (a ? a.title : title) + ' · ' + workerMessages[recent],
          signature = JSON.stringify([recent, d && d.state, text]);
        if (workerActivity._signature !== signature) {
          workerActivity._signature = signature;
          workerActivity.setAttribute('data-state', (d && d.state) || 'idle');
          workerActivity.replaceChildren(E('strong', {}, _('最近操作结果')), E('p', {}, text));
        }
      }
    }
    function workerStage(key, title, card) {
      var heading = E('h3', { class: 'rp-worker-stage-heading' }, [
          E('span', {}, title),
          (workerBadges[key] = E('span', { class: 'rp-group-state' }))
        ]),
        box = E(
          'section',
          { class: 'rp-worker-stage', id: 'imok-worker-' + key, 'data-stage': key },
          [heading, E('div', { class: 'rp-worker-stage-body' }, card)]
        );
      workerStages[key] = box;
      return box;
    }
    function workerActionStage(key) {
      workerPinned = true;
      openWorkerStage(key);
    }
    function updateWorkerFlow() {
      if (!Object.keys(workerStages).length) return;
      var node = backupNode || (data[5] && data[5].node),
        configured = !!(node && node.configured),
        now = Date.now() / 1000;
      var savedProject =
          configured &&
          workerProjectRows.some(function (row) {
            return row.saved_as_backup && row.hostname === node.sni;
          }),
        imported = configured && !savedProject,
        deployed = !!workerFlow.deployed;
      var relayApplied =
        configured && !relayDirty && !!relaySavedHash && node.relay_order_hash === relaySavedHash;
      var scanCurrent =
        scanState &&
        scanState.state === 'done' &&
        configured &&
        now - Number(scanState.finished_at) >= -5 &&
        now - Number(scanState.finished_at) <= 300 &&
        (scanState.fingerprint === node.fingerprint ||
          scanState.applied_fingerprint === node.fingerprint);
      var relayEnabled = relayValues().filter(function (r) {
          return r.enabled;
        }),
        relayPassed =
          !relayDirty &&
          relayTest &&
          relayTest.state === 'done' &&
          relayEnabled.length &&
          relayEnabled.every(function (r) {
            var proof = relayRowProof(r.address);
            return relayCurrent(proof) && proof.state === 'ok' && proof.forced === true;
          });
      var scanned = !!(scanCurrent && scanState.best_ip),
        scanApplied = !!(scanned && scanState.applied_fingerprint === node.fingerprint),
        tested = !!workerFlow.tested;
      var states = {
        deploy: imported
          ? _('已有导入节点')
          : savedProject
            ? _('✓ 已部署并设为备用')
            : deployed
              ? _('部署就绪，待设为备用')
              : _('○ 待部署 / 检查'),
        relay: relayDirty
          ? _('有未保存编辑')
          : relayApplied
            ? _('✓ 顺序已应用')
            : relayPassed
              ? _('✓ 检测通过，待应用顺序')
              : _('○ 待检测 / 应用'),
        scan: scanApplied
          ? _('✓ 已应用，待整线检测')
          : scanned
            ? _('✓ 推荐可用，待应用')
            : _('○ 可选：保留原入口'),
        use: tested ? _('✓ 检测有效，可手动启用') : _('○ 待独立检测')
      };
      Object.keys(workerBadges).forEach(function (k) {
        var op = workerActions[k],
          d = workerDisplays[k];
        workerBadges[k].textContent =
          d && d.state === 'busy'
            ? '… ' +
              ((op && op.title) || _('后台正在处理')) +
              ' · ' +
              _('已等待 ') +
              d.elapsed +
              's'
            : d && d.state === 'error'
              ? _('✕ 操作失败，查看本项结果')
              : d && d.state === 'unknown'
                ? _('! 结果未确认，查看本项提示')
                : states[k];
        if (workerStageStates[k]) workerStageStates[k].textContent = workerBadges[k].textContent;
        workerStages[k].setAttribute(
          'data-state',
          d && ['busy', 'error', 'unknown', 'waiting'].indexOf(d.state) >= 0
            ? d.state
            : states[k].indexOf('✓') === 0
              ? 'success'
              : 'pending'
        );
      });
      if (workerPinned) return;
      var cloudRunning =
        cloudBusy ||
        workerOperationBusy('deploy') ||
        (cloudState &&
          ['queued', 'running'].indexOf(cloudState.state) >= 0 &&
          now - Number(cloudState.started_at) < 300);
      var scanRunning =
        scanBusy ||
        workerOperationBusy('scan') ||
        (scanState &&
          ['queued', 'running'].indexOf(scanState.state) >= 0 &&
          now - Number(scanState.started_at) < 300);
      var key = cloudRunning
        ? 'deploy'
        : relayBusy || workerOperationBusy('relay') || relayTaskRunning()
          ? 'relay'
          : scanRunning
            ? 'scan'
            : workerFlow.testing || tested
              ? 'use'
              : !configured
                ? 'deploy'
                : !relayApplied
                  ? 'relay'
                  : !scanned
                    ? 'scan'
                    : scanApplied
                      ? 'use'
                      : 'scan';
      openWorkerStage(key);
    }
    return {
      render: function (centerGroup) {
        return thisPage.render(
          centerGroup,
          workerInventory,
          function (ev) {
            if (ev && ev.preventDefault) ev.preventDefault();
            workerPinned = true;
            openWorkerStage('use');
          },
          workerWorkspaceView,
          workerStage,
          cloudMeta,
          cloudCard,
          relayCard,
          scanCard,
          workerBackupCard
        );
      },
      updateDirectory,
      update: function (node, test, status, cloud, scan, relays, observed) {
        updateBackup(node, test, status);
        updateCloud(cloud);
        updateScan(scan);
        updateRelays(relays, observed);
      },
      updateProgress: function () {
        updateWorkerProgress();
        updateWorkerFlow();
      },
      refreshDirectoryIfNeeded: function () {
        if (
          !cloudDirectoryAuto &&
          !cloudBusy &&
          !workerActions.deploy &&
          cloudMeta.token_saved &&
          !cloudToken.value &&
          (!cloudDirectory || cloudDirectory.stale) &&
          !(cloudState && ['queued', 'running'].indexOf(cloudState.state) >= 0)
        ) {
          cloudDirectoryAuto = true;
          cloudAction('verify', false);
        }
      },
      authorization: function () {
        return cloudState && cloudState.configured != null ? cloudState : cloudMeta;
      },
      hasConfiguredBackup: function () {
        return backupNode && backupNode.configured;
      }
    };
  }
});

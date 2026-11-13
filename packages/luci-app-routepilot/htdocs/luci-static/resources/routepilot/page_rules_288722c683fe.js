'use strict';
'require baseclass';

return baseclass.extend({
  renderForm: function (node, ruleSection, ruleNames, panel) {
    function take(keys) {
      return keys
        .map(function (k) {
          return node.querySelector('[id="cbi-routepilot-main-' + k + '"]');
        })
        .filter(Boolean);
    }
    var contents;
    if (ruleSection === 'sources') {
      var schedule = take(['update_hours']),
        sources = ['china_url', 'china_domains_url', 'gfw_url', 'whitelist_url'].map(
          function (key) {
            return panel(ruleNames[key.replace('_url', '')], null, take([key]));
          }
        );
      contents = [
        panel(
          _('自动更新频率'),
          _('单位为小时：24 为每天，168 为每周；修改后保存并应用。'),
          schedule
        ),
        E('div', { class: 'imok-settings-pair' }, sources)
      ];
    } else
      contents = [
        E('div', { class: 'imok-settings-pair' }, [
          panel(
            _('始终直连'),
            _('使用本地宽带出口，保留原有校验与优先级。'),
            take(['custom_direct_domains', 'custom_direct_ips'])
          ),
          panel(
            _('始终代理'),
            _('使用当前代理线路，保存后仍需确认实际生效。'),
            take(['custom_proxy_domains', 'custom_proxy_ips'])
          )
        ])
      ];
    node.replaceChildren(
      E(
        'section',
        {
          class: 'rp-rule-settings imok-settings-section',
          'aria-label': ruleSection === 'sources' ? _('规则来源与更新频率') : _('自定义分流设置')
        },
        contents
      )
    );

    return node;
  },
  render: function (section, nav, overview, status, sources, browser, custom) {
    if (section === 'overview') return [nav, overview, status];
    if (section === 'sources') return [nav, sources];
    if (section === 'manage') return [nav, browser];
    if (section === 'custom') return [nav, custom];
    return [];
  },
  create: function ({
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
  }) {
    var rulesCard = E('div', { class: 'cbi-section' }, [
      E('h3', {}, _('规则更新反馈')),
      rulesProgress,
      rulesTime,
      rulesCounts
    ]);
    var rulesCatalog = null,
      rulesInfoBusy = false,
      rulesQueryBusy = false,
      rulesMutationBusy = false,
      rulesInfoAt = 0,
      rulesQuerySerial = 0,
      rulesInfoSerial = 0,
      rulesPage = 1,
      rulesPages = 1,
      rulesSearchApplied = '';
    var ruleNames = {
        china: _('中国 IP'),
        china_domains: _('中国域名'),
        gfw: _('代理域名'),
        whitelist: _('直连域名')
      },
      rulesRefreshButtons = [];
    var rulesPlan = E('div', { 'aria-live': 'polite' }, _('正在读取当前更新计划…')),
      rulesVersion = E('div', { 'aria-live': 'polite' }),
      rulesSources = E('div'),
      rulesChanges = E('div'),
      rulesCandidate = E('div'),
      rulesPersonalSummary = E('div'),
      rulesOperation = E('p', { role: 'status', 'aria-live': 'polite' });
    var rulesKind = E(
      'select',
      { class: 'cbi-input-select', 'aria-label': _('规则分类') },
      Object.keys(ruleNames).map(function (k) {
        return E('option', { value: k }, ruleNames[k]);
      })
    );
    rulesKind.value = 'china';
    var rulesView = E('select', { class: 'cbi-input-select', 'aria-label': _('规则内容范围') }, [
      E('option', { value: 'effective' }, _('合并规则（订阅 + 个人覆盖）')),
      E('option', { value: 'current' }, _('订阅原始规则')),
      E('option', { value: 'personal_added' }, _('个人新增')),
      E('option', { value: 'personal_excluded' }, _('个人排除')),
      E('option', { value: 'added' }, _('本次订阅新增')),
      E('option', { value: 'removed' }, _('本次订阅删除')),
      E('option', { value: 'candidate' }, _('待确认订阅规则')),
      E('option', { value: 'candidate_added' }, _('待确认订阅新增')),
      E('option', { value: 'candidate_removed' }, _('待确认订阅删除'))
    ]);
    rulesView.value = 'effective';
    var rulesSearch = E('input', {
      class: 'cbi-input-text',
      type: 'search',
      maxlength: 253,
      placeholder: _('搜索域名或 IP 网段'),
      'aria-label': _('搜索规则内容')
    });
    var rulesRows = E('div', { class: 'imok-rule-rows', 'aria-live': 'polite' }),
      rulesPageInfo = E('span', { 'aria-live': 'polite' }, _('尚未读取规则内容'));
    function rulesRefreshButton() {
      var button = E(
        'button',
        {
          class: 'btn',
          click: function () {
            return readRulesInfo(true);
          }
        },
        _('刷新规则状态')
      );
      rulesRefreshButtons.push(button);
      return button;
    }
    var rulesRefresh = rulesRefreshButton();
    var rulesSearchButton = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          rulesSearchApplied = rulesSearch.value.trim();
          return readRulesPage(1);
        }
      },
      _('搜索规则')
    );
    var rulesPrevious = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return readRulesPage(rulesPage - 1);
        }
      },
      _('上一页')
    );
    var rulesNext = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return readRulesPage(rulesPage + 1);
        }
      },
      _('下一页')
    );
    var rulesAccept = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          return confirmRuleChange('accept');
        }
      },
      _('核对并应用异常候选')
    );
    var rulesRollback = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return confirmRuleChange('rollback');
        }
      },
      _('恢复上一版规则')
    );
    var rulesAddInput = E('input', {
      class: 'cbi-input-text',
      type: 'text',
      maxlength: 253,
      placeholder: _('域名或公网 IPv4/CIDR'),
      'aria-label': _('个人新增规则')
    });
    var rulesAddButton = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          var value = rulesAddInput.value.trim();
          if (!value) {
            rulesOperation.textContent = _('请先输入需要新增的域名或 IP 网段。');
            return;
          }
          return confirmPersonalChange('add', value);
        }
      },
      _('新增到本分类')
    );
    var rulesImmediateUpdate = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          return self.requestRulesUpdate();
        }
      },
      _('立即更新已保存来源')
    );
    function rulesStamp(ts) {
      return when(ts, _('暂无记录'));
    }
    function rulesShort(value) {
      return value ? String(value).slice(0, 12) : _('未知');
    }
    function rulesOverrideRevision(info) {
      return (info && info.overrides && info.overrides.revision) || '';
    }
    function sourceValue(picker) {
      var element =
        typeof picker.option.getUIElement === 'function'
          ? picker.option.getUIElement('main')
          : null;
      return element && typeof element.getValue === 'function'
        ? String(element.getValue() || '')
        : String(picker.widget.value || picker.initial || '');
    }
    function updateSourcePickers(info) {
      Object.keys(sourcePickers).forEach(function (kind) {
        var picker = sourcePickers[kind],
          current = sourceValue(picker),
          presets = (info.source_presets || []).filter(function (p) {
            return Array.isArray(p.kinds) && p.kinds.indexOf(kind) >= 0;
          });
        var signature = JSON.stringify(presets);
        if (picker.signature !== signature) {
          picker.signature = signature;
          picker.presets = presets;
          picker.select.replaceChildren(
            E('option', { value: 'custom' }, _('自定义地址 / 保留当前地址'))
          );
          presets.forEach(function (p) {
            picker.select.appendChild(
              E(
                'option',
                { value: p.id },
                p.label +
                  (p.mirror && String(p.label).indexOf('镜像') < 0
                    ? _(' · 分发镜像，可能滞后')
                    : '')
              )
            );
          });
        }
        picker.select.value = picker.manual
          ? 'custom'
          : (
              presets.filter(function (p) {
                return p.url === current;
              })[0] || {}
            ).id || 'custom';
        picker.select.setAttribute(
          'data-selected',
          picker.select.value !== 'custom' ? 'preset' : picker.touched ? 'custom' : 'none'
        );
        var historical = info.current && info.current.sources && info.current.sources[kind];
        picker.downloaded.textContent =
          _('已下载版本来源：') + (historical || _('旧版本未记录来源'));
      });
    }
    function rulesControls() {
      var busy = rulesQueryBusy || rulesMutationBusy;
      rulesRefreshButtons.forEach(function (button) {
        button.disabled = rulesInfoBusy || rulesMutationBusy;
      });
      rulesSearchButton.disabled = busy || !rulesCatalog;
      rulesKind.disabled = busy;
      rulesView.disabled = busy;
      Array.prototype.forEach.call(rulesView.children, function (option) {
        if (
          String(
            option.value || (option.getAttribute && option.getAttribute('value')) || ''
          ).indexOf('candidate') === 0
        )
          option.disabled = !rulesCatalog || !rulesCatalog.candidate;
      });
      rulesSearch.disabled = busy;
      rulesPrevious.disabled = busy || rulesPage <= 1;
      rulesNext.disabled = busy || rulesPage >= rulesPages;
      rulesAccept.disabled =
        rulesInfoBusy || rulesMutationBusy || !rulesCatalog || !rulesCatalog.candidate;
      rulesRollback.disabled =
        rulesInfoBusy || rulesMutationBusy || !rulesCatalog || !rulesCatalog.previous;
      rulesAddInput.disabled = rulesMutationBusy || !rulesCatalog;
      rulesAddButton.disabled = rulesInfoBusy || rulesMutationBusy || !rulesCatalog;
      rulesImmediateUpdate.disabled = rulesMutationBusy;
    }
    function showRulesCatalog(info) {
      var current = info.current || {},
        effective = info.effective || current,
        overrides = info.overrides || {},
        application = info.application || {},
        schedule = info.schedule || {},
        candidate = info.candidate;
      rulesPlan.replaceChildren(
        E(
          'p',
          {},
          _('已保存的自动更新间隔：') +
            (schedule.hours ? String(schedule.hours) + _(' 小时') : _('待确认'))
        ),
        E(
          'p',
          {},
          _('最近尝试：') +
            rulesStamp(schedule.last_attempt_at) +
            ' · ' +
            _('最近成功：') +
            rulesStamp(schedule.last_success_at)
        ),
        E(
          'p',
          {},
          _('下次计划：') +
            (!schedule.next_due_at && info.candidate
              ? _('等待异常候选确认，自动更新暂缓')
              : rulesStamp(schedule.next_due_at)) +
            (schedule.state === 'paused'
              ? _('（当前暂停，恢复运行后再执行）')
              : schedule.state === 'running'
                ? _('（正在更新）')
                : schedule.retry_pending
                  ? _('（失败后的重试计划）')
                  : '')
        ),
        E(
          'p',
          {},
          (schedule.message || '') +
            (schedule.retries_exhausted
              ? _(' · 已完成3次重试，将按正常间隔检查')
              : schedule.retry_count
                ? _(' · 退避重试阶段：') + schedule.retry_count + '/' + (schedule.retry_limit || 3)
                : '')
        )
      );
      var appliedText =
        {
          applied: _('已在当前连接中生效'),
          pending: _('已保存，等待运行中应用'),
          inactive: _('当前未接管网络，规则已保存'),
          unknown: _('当前运行版本尚未确认')
        }[application.state] || _('当前运行版本尚未确认');
      rulesVersion.replaceChildren(
        E(
          'p',
          {},
          _('当前已保存版本：') +
            rulesShort(effective.revision) +
            ' · ' +
            _('当前订阅版本：') +
            rulesShort(current.revision) +
            ' · ' +
            rulesStamp(current.time)
        ),
        E(
          'p',
          {},
          _('运行状态：') +
            appliedText +
            (application.revision ? ' · ' + _('运行版本：') + rulesShort(application.revision) : '')
        ),
        E('p', { class: 'rp-help' }, _('已保存不等于已生效；订阅更新保留个人新增与排除。'))
      );
      rulesSources.replaceChildren();
      var changeItems = [];
      rulesChanges.replaceChildren();
      Object.keys(ruleNames).forEach(function (k) {
        var change = current.changes && current.changes[k],
          source = current.sources && current.sources[k];
        rulesSources.appendChild(
          E('p', {}, [
            E('strong', {}, ruleNames[k] + _('：')),
            E(
              'span',
              { style: 'overflow-wrap:anywhere;word-break:break-word;' },
              source || _('当前版本未记录来源')
            ),
            E(
              'small',
              {},
              current.hashes && current.hashes[k]
                ? ' · SHA-256 ' + rulesShort(current.hashes[k])
                : ''
            )
          ])
        );
        changeItems.push(
          ruleNames[k] +
            _('：新增 ') +
            (change ? change.added : '—') +
            _(' 条，删除 ') +
            (change ? change.removed : '—') +
            _(' 条')
        );
      });
      rulesChanges.appendChild(E('p', {}, changeItems.join('； ')));
      rulesPersonalSummary.replaceChildren(
        E(
          'p',
          {},
          _('当前已保存订阅版本：') +
            rulesShort(current.revision) +
            ' · ' +
            _('个人覆盖版本：') +
            rulesShort(overrides.revision)
        ),
        E('p', {}, _('合并规则版本：') + rulesShort(effective.revision) + ' · ' + appliedText),
        E('p', { class: 'rp-help' }, _('合并内容是否已生效，以运行状态为准。'))
      );
      var personalCounts = Object.keys(ruleNames).map(function (k) {
        var count = (overrides.counts && overrides.counts[k]) || {};
        return (
          ruleNames[k] + _('：新增 ') + (count.added || 0) + _('，排除 ') + (count.excluded || 0)
        );
      });
      rulesPersonalSummary.appendChild(E('p', {}, personalCounts.join('； ')));
      updateSourcePickers(info);
      rulesCandidate.replaceChildren();
      if (candidate) {
        var notices = Object.keys(candidate.declines || {}).map(function (k) {
          var d = candidate.declines[k];
          return (ruleNames[k] || k) + ' ' + d.before + ' → ' + d.after;
        });
        rulesCandidate.appendChild(
          E(
            'p',
            { role: 'alert' },
            _('检测到规则数量异常下降，本次候选尚未覆盖当前版本：') + notices.join('；')
          )
        );
        rulesCandidate.appendChild(
          E(
            'p',
            {},
            _('候选版本：') +
              rulesShort(candidate.revision) +
              ' · ' +
              _('确认有效期至：') +
              rulesStamp(candidate.expires_at)
          )
        );
        rulesCandidate.appendChild(
          E(
            'p',
            { class: 'rp-help' },
            _('可先到“规则管理”核对候选及其增删内容；确认后才保存候选，个人覆盖继续保留。')
          )
        );
        rulesCandidate.appendChild(rulesAccept);
      }
      rulesControls();
    }
    function readRulesInfo(force) {
      if (
        !ruleDataPage ||
        !isCurrentView() ||
        rulesMutationBusy ||
        rulesInfoBusy ||
        (!force && Date.now() - rulesInfoAt < 10000)
      )
        return Promise.resolve();
      rulesInfoBusy = true;
      rulesControls();
      var serial = ++rulesInfoSerial;
      return command('rules-info')
        .then(JSON.parse)
        .then(function (info) {
          if (!isCurrentView() || serial !== rulesInfoSerial) return;
          var old = rulesCatalog && rulesCatalog.current && rulesCatalog.current.revision,
            oldOverrides = rulesOverrideRevision(rulesCatalog),
            oldCandidate =
              rulesCatalog && rulesCatalog.candidate && rulesCatalog.candidate.revision,
            candidateChanged = oldCandidate !== (info.candidate && info.candidate.revision),
            candidateView = rulesView.value.indexOf('candidate') === 0;
          rulesCatalog = info;
          if (rulesOperation._infoError) {
            rulesOperation.textContent = '';
            rulesOperation._infoError = false;
          }
          if (candidateView && !info.candidate) rulesView.value = 'effective';
          rulesInfoAt = Date.now();
          showRulesCatalog(info);
          if (
            ruleSection === 'manage' &&
            (old !== (info.current || {}).revision ||
              oldOverrides !== rulesOverrideRevision(info) ||
              (candidateView && candidateChanged))
          ) {
            rulesQuerySerial++;
            rulesQueryBusy = false;
            rulesRows.replaceChildren();
            return readRulesPage(1);
          }
        })
        .catch(function (e) {
          if (isCurrentView() && serial === rulesInfoSerial) {
            rulesOperation._infoError = true;
            rulesOperation.textContent = _('规则状态读取失败：') + e.message;
          }
        })
        .finally(function () {
          if (serial === rulesInfoSerial) {
            rulesInfoBusy = false;
            rulesControls();
          }
        });
    }
    function readRulesPage(pageNumber) {
      if (ruleSection !== 'manage' || !isCurrentView() || !rulesCatalog || rulesMutationBusy)
        return Promise.resolve();
      var serial = ++rulesQuerySerial,
        revision = (rulesCatalog.current || {}).revision,
        overridesRevision = rulesOverrideRevision(rulesCatalog),
        candidateRevision = rulesCatalog.candidate && rulesCatalog.candidate.revision,
        payload = {
          kind: rulesKind.value,
          view: rulesView.value,
          search: rulesSearchApplied,
          page: Math.max(1, pageNumber),
          page_size: 100,
          revision: revision,
          overrides_revision: overridesRevision
        };
      rulesQueryBusy = true;
      rulesRows.setAttribute('aria-busy', 'true');
      rulesPageInfo.textContent = _('正在读取规则内容…');
      rulesControls();
      return submitInput('rules-input', 'rules-query', payload)
        .then(function (result) {
          if (!isCurrentView() || serial !== rulesQuerySerial) return;
          if (
            result.revision !== revision ||
            result.overrides_revision !== overridesRevision ||
            result.kind !== payload.kind ||
            result.view !== payload.view ||
            result.search !== payload.search ||
            (payload.view.indexOf('candidate') === 0 &&
              result.candidate_revision !== candidateRevision)
          )
            throw Error(_('规则版本或查询条件已改变，请刷新后重试'));
          rulesPage = result.page;
          rulesPages = result.pages;
          rulesRows.replaceChildren();
          (result.entries || []).forEach(function (line) {
            var origin = (result.origins && result.origins[line]) || 'subscription',
              row = [E('code', {}, line)],
              action = null,
              label = '';
            if (payload.view.indexOf('candidate') !== 0) {
              if (origin === 'excluded' || payload.view === 'personal_excluded') {
                action = 'restore';
                label = _('撤销排除');
              } else if (origin === 'personal' || payload.view === 'personal_added') {
                action = 'remove_added';
                label = _('删除个人新增');
              } else if (['current', 'effective', 'added', 'removed'].indexOf(payload.view) >= 0) {
                action = 'exclude';
                label = _('长期排除');
              }
            }
            if (origin === 'personal') row.push(E('small', {}, _('个人新增')));
            if (origin === 'excluded') row.push(E('small', {}, _('已排除，不参与生效')));
            if (action)
              row.push(
                E(
                  'button',
                  {
                    class: 'btn',
                    click: function () {
                      return confirmPersonalChange(action, line, {
                        kind: payload.kind,
                        revision: revision,
                        overrides_revision: overridesRevision
                      });
                    }
                  },
                  label
                )
              );
            rulesRows.appendChild(E('div', { class: 'rp-rule-entry rp-rule-entry-manage' }, row));
          });
          if (!result.entries || !result.entries.length)
            rulesRows.appendChild(
              E(
                'p',
                { class: 'rp-help' },
                _('没有符合条件的规则。可清空搜索，或选择其他分类、内容范围。')
              )
            );
          rulesPageInfo.textContent =
            _('共 ') +
            result.total +
            _(' 条 · 第 ') +
            result.page +
            '/' +
            Math.max(1, result.pages) +
            _(' 页 · 每页最多 100 条');
        })
        .catch(function (e) {
          if (isCurrentView() && serial === rulesQuerySerial) {
            rulesRows.replaceChildren(
              E('p', { role: 'alert' }, _('规则内容读取失败：') + e.message)
            );
            rulesPageInfo.textContent = _('查询未完成，可重新搜索或刷新');
          }
        })
        .finally(function () {
          if (serial === rulesQuerySerial) {
            rulesQueryBusy = false;
            rulesRows.setAttribute('aria-busy', 'false');
            rulesControls();
          }
        });
    }
    function submitRuleMutation(action, payload, confirm) {
      if (rulesMutationBusy || !isCurrentView()) return;
      rulesMutationBusy = true;
      rulesOperation._infoError = false;
      rulesInfoSerial++;
      rulesQuerySerial++;
      rulesInfoBusy = false;
      rulesQueryBusy = false;
      rulesControls();
      confirm.disabled = true;
      rulesOperation.textContent = _('正在提交规则变更…');
      ui.hideModal();
      return submitInput('rules-input', action, payload)
        .then(function (result) {
          if (isCurrentView()) {
            rulesOperation.textContent = result.message || _('规则已保存，正在确认实际生效状态。');
            if (action === 'rules-edit' && payload.action === 'add') rulesAddInput.value = '';
          }
        })
        .catch(function (e) {
          if (isCurrentView()) rulesOperation.textContent = _('规则变更未完成：') + e.message;
        })
        .finally(function () {
          rulesMutationBusy = false;
          rulesControls();
          if (isCurrentView()) return readRulesInfo(true);
        });
    }
    function confirmPersonalChange(action, value, bound) {
      if (ruleSection !== 'manage' || rulesMutationBusy || !rulesCatalog) return;
      var current = rulesCatalog.current || {},
        kind = (bound && bound.kind) || rulesKind.value,
        payload = {
          kind: kind,
          action: action,
          value: value,
          revision: (bound && bound.revision) || current.revision,
          overrides_revision:
            (bound && bound.overrides_revision) || rulesOverrideRevision(rulesCatalog)
        };
      var title = {
          add: _('确认个人新增'),
          exclude: _('确认长期排除'),
          restore: _('确认撤销排除'),
          remove_added: _('确认删除个人新增')
        }[action],
        message =
          action === 'exclude'
            ? _(
                '此条目将从本分类的实际生效规则中排除。排除记录会长期保存，订阅更新不会把它重新加入；可在“个人排除”中撤销。'
              )
            : action === 'add'
              ? _('此条目将作为个人规则加入本分类，长期保留，不会被订阅更新覆盖。')
              : action === 'restore'
                ? _('撤销个人排除后，此条目是否生效取决于当前订阅或个人新增内容。')
                : _('仅删除个人新增记录；如果当前订阅也包含此条目，它仍会按订阅生效。');
      var confirm = E(
        'button',
        {
          class: 'btn cbi-button-action',
          click: function () {
            return submitRuleMutation('rules-edit', payload, confirm);
          }
        },
        _('确认变更')
      );
      showImokModal(title, [
        E('p', {}, (ruleNames[kind] || kind) + ' · ' + value),
        E('p', {}, message),
        E(
          'p',
          { class: 'rp-help' },
          _(
            '本分类覆盖只影响使用该分类的分流模式。若要始终直连或代理，请到“自定义分流”。规则变更保存后仍需确认运行中生效。'
          )
        ),
        E('div', { class: 'rp-summary-actions' }, [
          confirm,
          E('button', { class: 'btn', click: ui.hideModal }, _('取消'))
        ])
      ]);
    }
    function confirmRuleChange(action) {
      if (rulesMutationBusy || !rulesCatalog) return;
      var info = rulesCatalog,
        current = info.current || {},
        target = action === 'accept' ? info.candidate : info.previous;
      if (!target) return;
      var payload = { revision: current.revision, confirm: true };
      payload[action === 'accept' ? 'candidate_revision' : 'previous_revision'] = target.revision;
      var message =
        action === 'accept'
          ? _('此候选因规则数量异常下降而被暂缓。确认后以此候选替换已保存订阅，个人覆盖继续保留。')
          : _(
              '确认恢复上一版已保存订阅？个人覆盖、来源和更新频率设置保持当前值；后续自动更新仍按保存的计划执行。'
            );
      var confirm = E(
        'button',
        {
          class: 'btn cbi-button-action',
          click: function () {
            return submitRuleMutation(
              action === 'accept' ? 'rules-accept' : 'rules-rollback',
              payload,
              confirm
            );
          }
        },
        action === 'accept' ? _('确认应用此候选') : _('确认恢复此版本')
      );
      showImokModal(action === 'accept' ? _('确认应用异常候选') : _('确认恢复上一版规则'), [
        E('p', {}, message),
        E(
          'p',
          {},
          _('当前订阅版本：') +
            rulesShort(current.revision) +
            ' → ' +
            _('目标版本：') +
            rulesShort(target.revision)
        ),
        E(
          'p',
          { class: 'rp-help' },
          _(
            '规则变更可能影响受管设备的分流；保存成功后仍需确认运行状态。版本已变化或候选已过期时，操作会拒绝执行。'
          )
        ),
        E('div', { class: 'rp-summary-actions' }, [
          confirm,
          E('button', { class: 'btn', click: ui.hideModal }, _('取消'))
        ])
      ]);
    }
    rulesKind.addEventListener('change', function () {
      return readRulesPage(1);
    });
    rulesView.addEventListener('change', function () {
      return readRulesPage(1);
    });
    rulesSearch.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        rulesSearchApplied = rulesSearch.value.trim();
        return readRulesPage(1);
      }
    });
    rulesControls();
    var rulesNav = E(
      'nav',
      { class: 'rp-section-nav', 'aria-label': _('规则与更新页面') },
      Object.keys(ruleSections).map(function (key) {
        return E(
          'a',
          {
            class: 'btn',
            href: base + '/rules/' + ruleSections[key],
            'aria-current': key === page ? 'page' : null
          },
          ruleSectionNames[ruleSections[key]]
        );
      })
    );
    var rulesStatusCard = E(
      'section',
      { class: 'cbi-section', 'aria-label': _('规则版本与执行计划') },
      [
        E('h3', {}, _('规则版本与执行计划')),
        E('div', { class: 'imok-settings-pair' }, [
          panel(_('更新计划'), _('自动更新使用已保存的来源与间隔。'), [rulesPlan]),
          panel(_('保存版本与运行状态'), null, [rulesVersion])
        ]),
        E('section', { class: 'rp-rule-summary imok-rule-source-summary' }, [
          E('h4', {}, _('当前下载版本来源与校验摘要')),
          E('a', { class: 'btn', href: base + '/rules/sources' }, _('编辑来源与频率')),
          rulesSources
        ]),
        E('section', { class: 'rp-rule-summary' }, [
          E('h4', {}, _('本次订阅更新变化')),
          rulesChanges
        ]),
        rulesCandidate,
        E('div', { class: 'rp-summary-actions' }, [
          rulesRefresh,
          rulesRollback,
          rulesImmediateUpdate
        ])
      ].concat(ruleSection === 'overview' ? [rulesOperation] : [])
    );
    var rulesBrowser = E(
      'section',
      { class: 'cbi-section', 'aria-label': _('规则内容浏览') },
      [
        E('p', { class: 'rp-help' }, [
          _(
            '个人新增和排除不会被订阅更新覆盖。本分类覆盖只影响使用该分类的分流模式；始终直连/代理请到'
          ),
          E('a', { href: base + '/rules/custom' }, _('自定义分流')),
          _('。常规“导出配置”不包含个人新增和排除；升级会保留，迁移需另备份。')
        ]),
        panel(_('规则版本与个人覆盖'), null, [
          rulesPersonalSummary,
          E('div', { class: 'rp-summary-actions' }, [rulesRefreshButton()])
        ]),
        E('div', { class: 'imok-rule-tools' }, [
          E('div', { class: 'imok-rule-filter' }, [
            E('div', { class: 'rp-summary-actions rp-rule-controls' }, [
              E('label', {}, [_('规则分类 '), rulesKind]),
              E('label', {}, [_('内容范围 '), rulesView])
            ]),
            E('div', { class: 'rp-summary-actions rp-rule-controls' }, [
              E('label', {}, [_('搜索内容 '), rulesSearch]),
              rulesSearchButton
            ])
          ]),
          panel(_('添加个人规则'), _('仅对当前所选分类生效，订阅更新会保留。'), [
            E('div', { class: 'rp-summary-actions rp-rule-controls' }, [
              E('label', {}, [_('个人新增 '), rulesAddInput]),
              rulesAddButton
            ])
          ])
        ]),
        rulesRows,
        E('div', { class: 'rp-summary-actions' }, [rulesPrevious, rulesPageInfo, rulesNext])
      ].concat(ruleSection === 'manage' ? [rulesOperation] : [])
    );
    var rulesSourcesIntro = E(
      'section',
      { class: 'cbi-section', 'aria-label': _('来源与频率说明') },
      [
        E('p', { class: 'rp-help' }, [
          _('选择预设或编辑 HTTPS 地址后保存；自动更新按计划执行，立即下载请到'),
          E('a', { href: base + '/rules/overview' }, _('更新概况')),
          _('。镜像可能有缓存延迟，不是独立规则；来源选择不改变线路入口或出口。')
        ]),
        rulesRefreshButton()
      ].concat(ruleSection === 'sources' ? [rulesOperation] : [])
    );
    var rulesCustomIntro = E('section', { class: 'cbi-section' }, [
      E(
        'p',
        { class: 'rp-help' },
        _('始终直连和代理规则独立保存，订阅更新不会清空；修改后保存并应用。')
      )
    ]);
    return {
      legacyCard: rulesCard,
      names: ruleNames,
      nav: rulesNav,
      statusCard: rulesStatusCard,
      sourcesIntro: rulesSourcesIntro,
      browser: rulesBrowser,
      customIntro: rulesCustomIntro,
      read: readRulesInfo,
      refreshSourcePickers: function () {
        if (rulesCatalog) updateSourcePickers(rulesCatalog);
      },
      reload: function () {
        rulesInfoAt = 0;
        return readRulesInfo(true);
      }
    };
  }
});

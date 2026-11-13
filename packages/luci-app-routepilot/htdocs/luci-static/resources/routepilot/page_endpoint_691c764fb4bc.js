'use strict';
'require baseclass';

return baseclass.extend({
  render: function (centerGroup, panel, endpointDetails, endpointCard) {
    return centerGroup('endpoint', _('主线路入口'), '', [
      E('div', { class: 'imok-settings-pair imok-entry-layout' }, [
        panel(
          _('当前入口与最近结果'),
          _('这里是客户端读取结果；编辑项保留在右侧，不会因读取而提交。'),
          [endpointDetails],
          'imok-readonly-panel'
        ),
        endpointCard
      ])
    ]);
  },
  create: function ({
    boundRequest,
    command,
    feedback,
    fs,
    notify,
    showImokModal,
    ui,
    when,
    getStatus
  }) {
    var endpointLoaded = false,
      endpointBusy = false,
      endpointDirty = false,
      endpointView = null,
      endpointTracking = null,
      recoveryTracking = null;
    var endpointMode = E('select', { class: 'cbi-input-select' }, [
      E('option', { value: 'auto' }, _('默认：官方自动')),
      E('option', { value: 'candidate' }, _('固定：从官方候选选择')),
      E('option', { value: 'custom' }, _('手写：自定义 IPv4 入口'))
    ]);
    var endpointAddress = E('input', {
      class: 'cbi-input-text',
      type: 'text',
      placeholder: 'IPv4:端口'
    });
    var endpointHours = E(
      'select',
      { class: 'cbi-input-select' },
      [0, 6, 12, 24, 72, 168].map(function (h) {
        return E('option', { value: String(h) }, h ? String(h) + _(' 小时') : _('关闭定期读取'));
      })
    );
    var endpointCandidates = E('select', { class: 'cbi-input-select' }, [
      E('option', { value: '' }, _('读取后选择候选'))
    ]);
    var endpointResult = E('p', { 'aria-live': 'polite' }, _('正在读取入口设置…'));
    var endpointDetails = E('div', {}),
      endpointModeHelp = E('p', { class: 'rp-help', 'aria-live': 'polite' });
    var endpointCandidateField = E('label', { class: 'rp-cloud-field' }, [
      E('span', {}, _('固定候选入口（尚未逐项测速）')),
      endpointCandidates
    ]);
    var endpointCustomField = E('label', { class: 'rp-cloud-field' }, [
      E('span', {}, _('手写 IPv4:端口（自定义未经检测）')),
      endpointAddress
    ]);
    function updateEndpointMode() {
      var mode = endpointMode.value;
      endpointCandidateField.hidden = mode !== 'candidate';
      endpointCandidates.disabled = mode !== 'candidate';
      endpointCustomField.hidden = mode !== 'custom';
      endpointAddress.disabled = mode !== 'custom';
      endpointModeHelp.textContent =
        mode === 'candidate'
          ? _(
              '选择官方客户端本地候选中的一个 IPv4 入口。应用并检测后固定使用；失败将尝试恢复并验证原入口。定期同步候选不会自动更换固定入口。'
            )
          : mode === 'custom'
            ? _(
                '填写公网 IPv4:端口，例如 162.159.197.2:8443。应用并检测后固定使用；自定义地址不保证可用，失败将尝试恢复并验证原入口。'
              )
            : _(
                '无需选择或填写 IP。应用并检测后清除现有固定入口，由官方客户端管理入口；失败将尝试恢复并验证原设置。'
              );
      endpointModeHelp.textContent += _(
        ' 仅切换选项不会生效；点击“应用并检测”会短暂重连，核对隧道连接和原预期出口，不代表测速或手机端验收。'
      );
    }
    [endpointMode, endpointAddress, endpointHours].forEach(function (el) {
      el.addEventListener('change', function () {
        endpointDirty = true;
        updateEndpointMode();
      });
    });
    endpointAddress.addEventListener('input', function () {
      endpointDirty = true;
    });
    endpointCandidates.addEventListener('change', function () {
      endpointDirty = true;
      updateEndpointMode();
    });
    function showEndpoint(v) {
      endpointView = v;
      var restoreFields = !endpointLoaded || !endpointDirty,
        preferred = restoreFields
          ? v.settings.mode === 'manual'
            ? v.settings.address
            : v.settings.mode === 'preserve' && v.override_observed
              ? v.configured_override
              : ''
          : endpointCandidates.value;
      if (restoreFields) {
        endpointMode.value = preferred
          ? ((v.candidates && v.candidates.entries) || []).indexOf(preferred) >= 0
            ? 'candidate'
            : 'custom'
          : 'auto';
        endpointAddress.value = preferred || '';
        endpointHours.value = String(v.settings.hours);
      }
      endpointLoaded = true;
      updateEndpointMode();
      var c = v.candidates || {},
        r = v.collection || {},
        a = v.application || {},
        available = v.capability && v.capability.supported;
      endpointCandidates.replaceChildren(E('option', { value: '' }, _('选择候选入口')));
      (c.entries || []).forEach(function (addr) {
        endpointCandidates.appendChild(
          E(
            'option',
            { value: addr, disabled: addr.charAt(0) === '[' ? true : null },
            addr + (addr.charAt(0) === '[' ? _(' · 当前环境不支持 IPv6') : '')
          )
        );
      });
      endpointCandidates.value = (c.entries || []).indexOf(preferred) >= 0 ? preferred : '';
      endpointDetails.replaceChildren(
        E(
          'p',
          {},
          _('客户端配置的固定入口：') +
            (v.override_observed
              ? v.configured_override || _('未固定，交给官方客户端')
              : _('暂不能确认'))
        ),
        E('p', {}, _('配置入口不等于实际远端观测；自定义地址未经检测。')),
        E(
          'p',
          {},
          _('最近读取：') +
            (r.checked_at ? when(r.checked_at) : _('尚未读取')) +
            ' · ' +
            (r.message || _('默认每24小时读取客户端本地候选'))
        ),
        E(
          'p',
          {},
          _('最近成功：') +
            (c.collected_at ? when(c.collected_at) : _('无')) +
            ' · ' +
            _('源配置时间：') +
            (c.source_mtime ? when(c.source_mtime) : _('未知'))
        ),
        E(
          'p',
          {},
          available
            ? _('当前客户端支持入口 set/reset。')
            : _('客户端能力尚未确认，暂不能应用入口。')
        )
      );
      var message = v.settings.staged
        ? _('导入的入口偏好尚未应用，请核对后单独保存。')
        : v.pending
          ? getStatus() && getStatus().state === 'disabled'
            ? _('设置已保存，等待主线路开启后由后台应用。')
            : _('设置已保存，等待后台处理本次请求…')
          : a.state === 'applied'
            ? _('入口设置已确认。') +
              (a.connected ? _(' 已完成连接检测；') : '') +
              (a.exit_verified ? _('原预期出口验证通过。') : _('请另行检测实际出口。'))
            : a.state === 'rejected'
              ? _('应用前校验未通过；当前连接未改变，请核对能力和原出口基线。')
              : a.state === 'verifying'
                ? _('正在连接并验证原出口…')
                : a.state === 'rolling_back'
                  ? _('应用未通过，正在恢复并验证原入口…')
                  : a.state === 'unknown'
                    ? _('恢复结果未知，可能直连；请人工检查，不会继续自动切换。')
                    : a.state === 'failed'
                      ? _('入口应用失败；') +
                        (a.rollback_confirmed
                          ? _('已恢复原入口设置。')
                          : _('恢复未确认，请手动检查。'))
                      : a.state === 'applying'
                        ? _('入口事务正在处理或结果未确认；请读取状态，必要时人工核对。')
                        : _('保持现有入口。');
      if (endpointTracking) {
        if (v.settings.revision !== endpointTracking.revision) {
          message = _('本次请求已被另一个设置请求替代，请核对当前结果。');
          endpointTracking = null;
        } else if (
          a.revision === endpointTracking.revision &&
          ['applied', 'preserved', 'failed', 'rejected', 'unknown'].indexOf(a.state) >= 0
        )
          endpointTracking = null;
        else if (Date.now() - endpointTracking.started > 120000) {
          message = _('等待超过120秒，结果尚未确认；请读取当前状态，勿将保存成功当成已应用。');
          endpointTracking = null;
        }
      }
      var recovery = v.recovery_block || {};
      if (recovery.new_request_block) {
        var detail = recovery.reason || _('请读取当前状态，核对恢复结果。');
        if (recovery.terminal) {
          message =
            _('入口请求受阻，未保存或未应用：') +
            (recovery.code ? recovery.code + ' · ' : '') +
            detail;
          endpointTracking = null;
        } else message += ' · ' + _('入口请求受阻。') + ' ' + detail;
      }
      endpointSave.disabled = endpointBusy || !!recovery.new_request_block;
      if (v.warnings && v.warnings.length) message += ' · ' + v.warnings.join('；');
      if (v.warning && v.warning.message && v.pending) message += ' · ' + v.warning.message;
      if (
        recoveryTracking &&
        (!v.recovery_result || v.recovery_result.request_id !== recoveryTracking.id)
      )
        message += ' · ' + _('本次受控恢复已提交，等待后台验证结果');
      else if (v.recovery_result && v.recovery_result.message) {
        message +=
          ' · ' +
          _('上次受控恢复：') +
          when(v.recovery_result.time) +
          ' · ' +
          v.recovery_result.message;
        if (recoveryTracking && v.recovery_result.request_id === recoveryTracking.id)
          recoveryTracking = null;
      }
      endpointRecover.disabled = endpointBusy || !(v.recovery_offer && v.recovery_offer.available);
      endpointRecover.hidden = !recovery.startup_pause;
      endpointResult.textContent = message;
    }
    function endpointAction(action, recoveryPayload) {
      if (
        endpointBusy ||
        (action === 'endpoint-save' &&
          endpointView &&
          endpointView.recovery_block &&
          endpointView.recovery_block.new_request_block)
      )
        return Promise.resolve();
      endpointBusy = true;
      endpointRead.disabled = true;
      endpointRefresh.disabled = true;
      endpointSave.disabled = true;
      endpointSchedule.disabled = true;
      endpointRecover.disabled = true;
      endpointResult.textContent =
        action === 'endpoint-save' ? _('正在提交，应用可能短暂重连…') : _('正在读取…');
      var entryRequest,
        auxMessage = null,
        entryPayload = {
          mode: endpointMode.value === 'auto' ? 'auto' : 'manual',
          address:
            endpointMode.value === 'candidate'
              ? endpointCandidates.value
              : endpointMode.value === 'custom'
                ? endpointAddress.value.trim()
                : '',
          hours: Number(endpointHours.value),
          recovery: false
        };
      return Promise.resolve()
        .then(function () {
          if (['endpoint-save', 'endpoint-schedule', 'endpoint-recover'].indexOf(action) >= 0) {
            if (
              action === 'endpoint-save' &&
              entryPayload.mode === 'manual' &&
              !entryPayload.address
            )
              throw Error(_('请先选择候选入口或填写 IPv4:端口'));
            entryRequest = boundRequest(
              action === 'endpoint-schedule'
                ? { hours: Number(endpointHours.value) }
                : action === 'endpoint-recover'
                  ? recoveryPayload
                  : entryPayload
            );
            return fs.write(
              '/var/run/routepilot/' +
                (action === 'endpoint-schedule'
                  ? 'endpoint-schedule-input'
                  : action === 'endpoint-recover'
                    ? 'endpoint-recover-input'
                    : 'endpoint-input'),
              entryRequest.text,
              384
            );
          }
        })
        .then(function () {
          return command(action, entryRequest && entryRequest.token);
        })
        .then(function (raw) {
          if (action === 'endpoint-info') return raw;
          if (entryRequest) {
            var saved = JSON.parse(raw);
            if (saved.request_id !== entryRequest.id) throw Error(_('未收到本次入口请求确认'));
            if (action === 'endpoint-save') {
              endpointDirty = false;
              endpointTracking = { revision: saved.settings.revision, started: Date.now() };
            }
            if (action === 'endpoint-recover')
              recoveryTracking = { id: entryRequest.id, started: Date.now() };
            if (action === 'endpoint-schedule') auxMessage = saved.message;
          }
          return command('endpoint-info');
        })
        .then(function (raw) {
          showEndpoint(JSON.parse(raw));
          if (auxMessage) endpointResult.textContent += ' · ' + auxMessage;
        })
        .catch(function (e) {
          var failed = _('未完成：') + e.message;
          endpointResult.textContent = failed;
          if (action === 'endpoint-save')
            return command('endpoint-info')
              .then(function (raw) {
                showEndpoint(JSON.parse(raw));
                if (!(endpointView.recovery_block && endpointView.recovery_block.new_request_block))
                  endpointResult.textContent = failed;
              })
              .catch(function () {
                endpointResult.textContent = failed;
              });
        })
        .finally(function () {
          endpointBusy = false;
          endpointRead.disabled = false;
          endpointRefresh.disabled = false;
          endpointSchedule.disabled = false;
          endpointRecover.disabled = !(
            endpointView &&
            endpointView.recovery_offer &&
            endpointView.recovery_offer.available
          );
          endpointSave.disabled = !!(
            endpointView &&
            endpointView.recovery_block &&
            endpointView.recovery_block.new_request_block
          );
        });
    }
    function pollEndpoint() {
      if (endpointTracking && Date.now() - endpointTracking.started > 120000) {
        endpointTracking = null;
        endpointResult.textContent = _(
          '等待超过120秒，结果尚未确认；请读取当前状态，勿将保存成功当成已应用。'
        );
        return Promise.resolve();
      }
      if (recoveryTracking && Date.now() - recoveryTracking.started > 120000) {
        recoveryTracking = null;
        endpointResult.textContent = _('恢复请求等待超时，结果尚未确认；请读取当前状态。');
      }
      if ((endpointTracking || recoveryTracking) && !endpointBusy)
        return endpointAction('endpoint-info');
      return Promise.resolve();
    }
    var endpointRead = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return endpointAction('endpoint-info');
        }
      },
      _('读取当前设置 / 应用结果')
    );
    var endpointRefresh = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return endpointAction('endpoint-refresh');
        }
      },
      _('读取客户端最新本地候选（不切换）')
    );
    var endpointSave = E(
      'button',
      {
        class: 'btn cbi-button-action',
        click: function () {
          return endpointAction('endpoint-save');
        }
      },
      _('应用并检测（可能重连）')
    );
    var endpointSchedule = E(
      'button',
      {
        class: 'btn',
        click: function () {
          return endpointAction('endpoint-schedule');
        }
      },
      _('只保存同步间隔（不重连）')
    );
    var endpointRecover = E(
      'button',
      {
        class: 'btn',
        hidden: true,
        click: function () {
          var offer = endpointView && endpointView.recovery_offer;
          if (!offer || !offer.available) return;
          var confirm = E('input', { class: 'cbi-input-text', placeholder: 'RESTORE' });
          showImokModal(_('受控恢复原入口'), [
            E(
              'p',
              {},
              _(
                '仅恢复原事务入口并验证原出口，可能短暂直连。证据或验证不一致时仍保持阻断。总开关关闭时也会临时启动客户端验证，完成后仍保持关闭，不开启设备接管。请输入 RESTORE 确认。'
              )
            ),
            confirm,
            E(
              'button',
              {
                class: 'btn',
                click: function () {
                  if (confirm.value !== 'RESTORE') {
                    notify(_('请输入 RESTORE'), true);
                    return;
                  }
                  ui.hideModal();
                  return endpointAction('endpoint-recover', {
                    confirm: 'RESTORE',
                    revision: offer.revision,
                    context: offer.context
                  });
                }
              },
              _('确认恢复并验证')
            ),
            E(
              'button',
              {
                class: 'btn',
                click: function () {
                  ui.hideModal();
                }
              },
              _('取消')
            )
          ]);
        }
      },
      _('受控恢复原入口并验证')
    );
    var endpointQualityPending = 0;
    var endpointQualityStatus = E('p', { class: 'rp-help', role: 'status', 'aria-live': 'polite' });
    var endpointQualityRows = E('div', { class: 'rp-help' });
    var endpointQualityButton = E(
      'button',
      {
        class: 'btn',
        disabled: true,
        click: function () {
          endpointQualityPending = Date.now();
          endpointQualityButton.disabled = true;
          endpointQualityStatus.textContent = _('正在测量入口握手…');
          return command('endpoint-quality').catch(function (error) {
            endpointQualityPending = 0;
            endpointQualityButton.disabled = false;
            endpointQualityStatus.textContent = error.message;
          });
        }
      },
      _('测量已有候选')
    );
    var endpointQualityRendered = '';
    function updateEndpointQuality(status, result) {
      if (endpointQualityPending && Date.now() - endpointQualityPending > 32000) {
        endpointQualityPending = 0;
        endpointQualityStatus.textContent = _('入口测量超时，请重试。');
      }
      var running =
        feedback.fresh(status, Date.now()) &&
        status.state === 'running' &&
        status.backend === 'masque' &&
        status.protocol === 'MASQUE';
      endpointQualityButton.disabled = !running || Boolean(endpointQualityPending);
      if (
        !result ||
        !result.finished_at ||
        (endpointQualityPending && Number(result.finished_at) * 1000 < endpointQualityPending - 500)
      ) {
        if (!running && !endpointQualityPending)
          endpointQualityStatus.textContent = _('入口测量需要运行中的 MASQUE 主线路。');
        return;
      }
      endpointQualityPending = 0;
      endpointQualityButton.disabled = !running;
      var key = JSON.stringify(result);
      if (key === endpointQualityRendered) return;
      endpointQualityRendered = key;
      if (result.state !== 'done') {
        endpointQualityStatus.textContent =
          result.state === 'stale'
            ? _('线路或候选已变化，请重新测量。')
            : _('入口测量未完成，请重试。');
        endpointQualityRows.replaceChildren();
        return;
      }
      endpointQualityStatus.textContent = [
        result.transport === 'quic' ? _('QUIC 握手') : _('TCP/TLS 握手，当前 curl 不支持 HTTP/3'),
        when(result.finished_at)
      ].join(' · ');
      endpointQualityRows.replaceChildren(
        E(
          'ul',
          {},
          (result.rows || []).map(function (row) {
            return E('li', {}, [
              row.address +
                ' · ' +
                (row.median_ms == null ? _('无有效握手') : row.median_ms + ' ms'),
              ' · ' + _('握手失败：') + row.failed + '/' + row.attempts,
              row.address === result.recommended ? ' · ' + _('推荐') : ''
            ]);
          })
        )
      );
    }
    var endpointCard = E('section', { class: 'rp-guide-step rp-endpoint-card' }, [
      E('h3', { class: 'rp-guide-heading' }, _('入口选择与候选同步')),
      E(
        'p',
        { class: 'rp-help' },
        _('候选来自客户端本地配置。测量不会切换入口，应用入口可能短暂重连。')
      ),
      E('label', { class: 'rp-cloud-field' }, [
        E('span', {}, _('待应用入口方式（当前配置见上方）')),
        endpointMode
      ]),
      endpointModeHelp,
      endpointCandidateField,
      endpointCustomField,
      E('label', { class: 'rp-cloud-field' }, [
        E('span', {}, _('官方客户端本地候选同步间隔')),
        endpointHours
      ]),
      E('div', { class: 'rp-summary-actions' }, [
        endpointRead,
        endpointRefresh,
        endpointQualityButton,
        endpointSchedule,
        endpointSave,
        endpointRecover
      ]),
      endpointResult,
      endpointQualityStatus,
      endpointQualityRows
    ]);
    updateEndpointMode();
    return {
      details: endpointDetails,
      card: endpointCard,
      poll: pollEndpoint,
      updateQuality: updateEndpointQuality,
      initialize: function () {
        if (!endpointLoaded && !endpointBusy) return endpointAction('endpoint-info');
      }
    };
  }
});

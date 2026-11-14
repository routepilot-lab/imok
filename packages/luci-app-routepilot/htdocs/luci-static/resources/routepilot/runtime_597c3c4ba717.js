'use strict';
'require baseclass';
'require fs';
'require ui';
'require poll';
'require request';
'require rpc';

var progressLabels = {
  queued: '等待维护任务开始',
  check_version: '检查官方版本',
  download_base: '下载 Debian 基础环境',
  verify_base: '校验并解包 Debian 基础环境',
  verify_sources: '校验官方软件源签名',
  download_warp: '下载 Cloudflare WARP 程序',
  download_packages: '下载 WARP 和依赖',
  verify_packages: '校验 WARP 和依赖',
  ready: '已下载并校验，等待确认安装',
  prepare_install: '准备独立安装副本',
  install_packages: '安装 WARP 和依赖',
  verify_install: '验证 WARP 运行环境',
  commit: '提交 WARP 运行环境',
  installed: 'WARP 已安装'
};

var downloadComponents = [
  { id: 'debian', label: 'Debian 基础环境', phase: 'download_base' },
  { id: 'warp', label: 'Cloudflare WARP 程序', phase: 'download_warp' },
  { id: 'dependencies', label: 'Debian 依赖', phase: 'download_packages' }
];

function mebibytes(bytes) {
  return (bytes / 1048576).toFixed(1);
}

function elapsedText(seconds) {
  var minutes = Math.floor(seconds / 60);
  return '本阶段已用时 ' + (minutes ? minutes + ' 分 ' : '') + (seconds % 60) + ' 秒';
}

function updateCheckError(message, installed) {
  var retry = installed
    ? '现有 WARP 安装不受影响，可稍后点「检查更新」重试。'
    : '可稍后点「检查更新」重试。';
  var legacy = /^(安装或校验命令失败|维护命令超时|维护命令无法在隔离环境中启动)/;
  return message && !legacy.test(message) ? [message, retry].join(' ') : retry;
}

function validStatus(value) {
  if (
    !value ||
    value.schema !== 1 ||
    typeof value.runtime_exists !== 'boolean' ||
    typeof value.update_available !== 'boolean' ||
    typeof value.recovery_required !== 'boolean' ||
    typeof value.installed !== 'boolean' ||
    typeof value.supported !== 'boolean' ||
    !value.settings ||
    ![0, 24, 72, 168].includes(value.settings.hours) ||
    !value.check ||
    typeof value.check !== 'object' ||
    !value.job ||
    typeof value.job !== 'object' ||
    !Array.isArray(value.backups)
  )
    throw Error('WARP 维护状态格式异常，请刷新；未推断为已安装或最新版本');
  if (
    (value.installed &&
      typeof value.local_version !== 'string' &&
      !(value.local_version == null && value.job.state === 'running')) ||
    (value.latest_version != null && typeof value.latest_version !== 'string') ||
    Array.isArray(value.job) ||
    Array.isArray(value.check) ||
    (value.job.message != null && typeof value.job.message !== 'string') ||
    (value.job.phase != null && typeof value.job.phase !== 'string') ||
    (value.check.message != null && typeof value.check.message !== 'string') ||
    value.backups.some(function (b) {
      return (
        !b ||
        typeof b.id !== 'string' ||
        !/^[a-f0-9]{32}$/.test(b.id) ||
        !Number.isFinite(b.created_at) ||
        (b.version != null && typeof b.version !== 'string')
      );
    })
  )
    throw Error('WARP 维护状态字段异常，请刷新核对');
  if (
    value.environment != null &&
    (!value.environment ||
      Array.isArray(value.environment) ||
      typeof value.environment.can_prepare !== 'boolean' ||
      !Array.isArray(value.environment.issues) ||
      value.environment.issues.some(function (v) {
        return typeof v !== 'string';
      }))
  )
    throw Error('环境检测结果格式异常，请刷新核对');
  if (
    value.online != null &&
    (!value.online ||
      Array.isArray(value.online) ||
      !['idle', 'preparing', 'ready', 'expired', 'installing', 'installed', 'failed'].includes(
        value.online.state
      ) ||
      (value.online.message != null && typeof value.online.message !== 'string') ||
      (value.online.state === 'ready' && typeof value.online.version !== 'string'))
  )
    throw Error('自动准备状态格式异常，请刷新核对');
  return value;
}
function readStatus() {
  return fs
    .exec('/usr/libexec/routepilot-runtime-status', [])
    .catch(function () {
      throw Error('无法连接路由器维护接口，请检查管理连接和登录权限');
    })
    .then(function (r) {
      if (!r || typeof r.stdout !== 'string') throw Error('路由器维护响应无效，请刷新重试');
      var d;
      try {
        d = JSON.parse(r.stdout);
      } catch (_) {
        throw Error('未取得有效的 WARP 维护状态');
      }
      if (r.code || (d && d.error)) throw Error((d && d.error) || '没有读取 WARP 维护状态的权限');
      return validStatus(d);
    });
}
return baseclass.extend({
  load: function () {
    return readStatus().catch(function (e) {
      return { load_error: e.message };
    });
  },
  render: function (initial, stylesheet, bindRequest, when) {
    var self = this,
      data = initial,
      working = false,
      reading = false,
      readSerial = 0,
      prepared = false,
      shaValue = '',
      selectedHours = null,
      selectedBackup = '',
      uploaded = false,
      modalActive = false,
      awaitingJob = false,
      lastJobView = '';
    var root = E('div', { class: 'rp-layout', 'data-page': 'runtime' }),
      body = E('div', { class: 'rp-page-content' }),
      jobNotice = E('div', {}),
      notice = E('p', { class: 'imok-runtime-notice', role: 'status', 'aria-live': 'polite' });
    root.appendChild(E('link', { rel: 'stylesheet', href: stylesheet }));
    root.appendChild(
      E('div', { class: 'rp-header' }, [
        E('span', { class: 'rp-brand' }, 'imOK'),
        E('h2', {}, 'WARP 安装与更新'),
        E(
          'p',
          { class: 'rp-caption' },
          '先在路由器安装 WARP，再登录账户与选择出口。定期检查只提示新版，安装和升级由你确认。'
        )
      ])
    );
    root.appendChild(notice);
    root.appendChild(jobNotice);
    root.appendChild(body);
    function timeLabel(t) {
      return Number.isFinite(t) && t > 0 ? when(t) : '尚未检查';
    }
    function button(label, action, primary, disabled) {
      return E(
        'button',
        {
          class: 'btn cbi-button' + (primary ? ' cbi-button-apply' : ''),
          type: 'button',
          disabled: disabled ? true : null,
          click: function () {
            if (working) return;
            return Promise.resolve()
              .then(action)
              .catch(function (e) {
                message(e.message, true);
              });
          }
        },
        label
      );
    }
    function message(text, error) {
      notice.textContent = text;
      notice.setAttribute('data-tone', error ? 'danger' : 'neutral');
      notice.setAttribute('role', error ? 'alert' : 'status');
    }
    function panel(title, children) {
      return E(
        'section',
        { class: 'imok-panel' },
        [E('div', { class: 'imok-panel-heading' }, [E('h3', {}, title)])].concat(children)
      );
    }
    function link(label, route) {
      return E(
        'a',
        { class: 'btn cbi-button', href: L.url('admin/services/routepilot/' + route) },
        label
      );
    }
    function help(text) {
      return E('p', { class: 'rp-help' }, text);
    }
    function drawJob() {
      var job = data.job || {},
        online = data.online || {},
        running = job.state === 'running' || awaitingJob,
        failed = ['failed', 'interrupted'].includes(job.state) && !awaitingJob,
        phase = awaitingJob ? 'queued' : job.phase,
        label = Object.prototype.hasOwnProperty.call(progressLabels, phase)
          ? progressLabels[phase]
          : '',
        detail = awaitingJob ? '' : job.message,
        checkFailure = failed && phase === 'check_version';
      if (checkFailure && data.installed && data.check.state === 'failed') {
        lastJobView = '';
        jobNotice.replaceChildren();
        return;
      }
      if (checkFailure) detail = updateCheckError(detail, data.installed);
      if (!data.installed && phase === 'download_packages') label = '下载 Debian 依赖';
      if (running && phase === 'install_packages') {
        if (job.install_step === 'unpack') label = '解包 WARP 和依赖';
        if (job.install_step === 'configure') label = '配置 WARP 和依赖';
      }
      if (!running && !failed) {
        if (online.state === 'ready') label = progressLabels.ready;
        else if (phase === 'ready') {
          failed = true;
          label = '安装文件需要重新准备';
          detail = online.message;
        } else if (phase === 'installed' && !data.installed) {
          label = '安装结果尚待核对';
          detail = '';
        }
      }
      var installing = running && (phase === 'prepare_install' || phase === 'install_packages'),
        hasInstallPercent =
          running &&
          phase === 'install_packages' &&
          Number.isSafeInteger(job.install_percent) &&
          job.install_percent >= 0 &&
          job.install_percent <= 100,
        elapsed =
          installing && Number.isSafeInteger(job.elapsed_seconds) && job.elapsed_seconds >= 0
            ? elapsedText(job.elapsed_seconds)
            : '',
        hasBytes =
          running &&
          !awaitingJob &&
          !installing &&
          Number.isSafeInteger(job.bytes_done) &&
          job.bytes_done >= 0,
        hasTotal =
          hasBytes &&
          Number.isSafeInteger(job.bytes_total) &&
          job.bytes_total > 0 &&
          job.bytes_done <= job.bytes_total,
        state = failed ? 'error' : running ? 'busy' : job.state || 'unknown',
        jobView = JSON.stringify([
          state,
          label,
          detail,
          hasBytes && job.bytes_done,
          hasTotal && job.bytes_total,
          hasInstallPercent && job.install_percent,
          elapsed
        ]);
      if (jobView === lastJobView) return;
      lastJobView = jobView;
      if (!label && !detail && !running && !failed) {
        jobNotice.replaceChildren();
        return;
      }
      var children = [];
      if (failed) {
        var failureTitle = phase === 'queued' ? '维护准备未通过' : '维护任务未完成';
        if (checkFailure) failureTitle = '更新检查失败';
        else if (label && phase !== 'queued') failureTitle = '未完成 · ' + label;
        children.push(E('strong', {}, failureTitle));
      } else if (label || running) {
        children.push(E('strong', {}, label || '维护进行中'));
      }
      if (running) {
        var attrs = { 'aria-label': 'WARP 安装与下载进度' };
        if (hasTotal) {
          attrs.max = job.bytes_total;
          attrs.value = job.bytes_done;
        } else if (hasInstallPercent) {
          attrs.max = 100;
          attrs.value = job.install_percent;
        }
        children.push(E('progress', attrs));
        if (hasInstallPercent || elapsed)
          children.push(
            E(
              'small',
              {},
              [hasInstallPercent ? '安装进度 ' + job.install_percent + '%' : '', elapsed]
                .filter(Boolean)
                .join(' · ')
            )
          );
        if (hasBytes)
          children.push(
            E(
              'small',
              {},
              hasTotal
                ? mebibytes(job.bytes_done) +
                    ' / ' +
                    mebibytes(job.bytes_total) +
                    ' MiB · ' +
                    Math.floor((100 * job.bytes_done) / job.bytes_total) +
                    '%'
                : '已下载 ' + mebibytes(job.bytes_done) + ' MiB'
            )
          );
      }
      if (detail && detail !== label) children.push(E('small', {}, detail));
      jobNotice.replaceChildren(
        E(
          'section',
          {
            class: 'rp-operation-feedback',
            'data-state': state,
            role: failed ? 'alert' : 'status'
          },
          children
        )
      );
    }
    function waitForJob() {
      awaitingJob = true;
      draw();
      return refresh();
    }
    function prepareOnline(refreshComponent) {
      if (working || awaitingJob || data.job.state === 'running') return;
      message('正在准备安装文件。文件校验通过后，由你确认安装。');
      return maintenanceRequest(
        'prepare-online',
        refreshComponent ? { refresh: refreshComponent } : {}
      ).then(function () {
        message('');
        return waitForJob();
      });
    }
    function componentDownloads(online, disabled) {
      return E(
        'ul',
        { class: 'imok-runtime-steps', 'aria-label': '安装文件下载状态' },
        downloadComponents.map(function (item) {
          var saved = (online.components || {})[item.id],
            cached = saved === 'cached',
            ready = saved === 'ready' || online.state === 'ready',
            active = data.job.phase === item.phase && data.job.state === 'running',
            failed = data.job.phase === item.phase && data.job.state === 'failed',
            tone = active ? 'neutral' : failed ? 'bad' : ready ? 'good' : 'neutral',
            status = active
              ? '下载中'
              : failed
                ? '下载失败'
                : ready
                  ? '已校验'
                  : cached
                    ? '已缓存，使用前校验'
                    : saved === 'missing'
                      ? '待下载'
                      : '待检查',
            row = [E('span', {}, item.label), E('span', { class: 'rp-status-badge' }, status)];
          if ((cached || ready) && !active) {
            var retry = button(
              '重新下载',
              function () {
                return prepareOnline(item.id);
              },
              false,
              disabled
            );
            retry.setAttribute('aria-label', '重新下载 ' + item.label);
            row.push(retry);
          }
          return E('li', { class: 'rp-summary-actions', 'data-tone': tone }, row);
        })
      );
    }
    function ensurePrepared() {
      if (prepared) return Promise.resolve();
      return fs.exec('/usr/libexec/routepilot-runtime', ['prepare']).then(function (r) {
        if (r.code) throw Error('无法准备维护目录，请检查管理员权限和存储空间');
        prepared = true;
      });
    }
    function maintenanceRequest(action, payload) {
      if (working) return Promise.reject(Error('已有操作正在提交，请稍候'));
      working = true;
      readSerial++;
      var r = bindRequest(payload || {});
      return ensurePrepared()
        .then(function () {
          return fs.write(
            '/var/run/routepilot/requests/runtime-input-' + r.id + '.json',
            r.text,
            384
          );
        })
        .then(function () {
          return fs.exec('/usr/libexec/routepilot-runtime', [action, r.token]);
        })
        .then(function (result) {
          var d;
          try {
            d = JSON.parse(result.stdout);
          } catch (_) {
            throw Error('维护响应无法识别，请刷新确认，勿重复提交');
          }
          if (result.code || (d && d.error)) throw Error((d && d.error) || '维护请求未完成');
          if (!d || typeof d !== 'object' || d.request_id !== r.id)
            throw Error('本次维护请求未收到对应确认，请刷新核对');
          return d;
        })
        .finally(function () {
          working = false;
        });
    }
    function refresh() {
      if (reading || working) return Promise.resolve();
      reading = true;
      var id = ++readSerial;
      return self
        .load()
        .then(function (d) {
          if (id !== readSerial || !root.isConnected) return;
          if (d.load_error && (awaitingJob || (data.job && data.job.state === 'running'))) {
            message('暂时无法读取最新进度，正在重试。下方显示上次状态。', true);
            return;
          }
          if (awaitingJob || (data.job && data.job.state === 'running')) message('');
          awaitingJob = false;
          data = d;
          draw();
        })
        .finally(function () {
          reading = false;
        });
    }
    function modalActions(buttons) {
      return E('div', { class: 'right imok-runtime-modal-actions' }, buttons);
    }
    function showFirstInstall() {
      if (working || awaitingJob || modalActive || data.job.state === 'running') return;
      var error = E('p', { role: 'alert' });
      var cancel = button('取消', function () {
        modalActive = false;
        ui.hideModal();
      });
      var confirm = button(
        '开始下载并安装',
        function () {
          if (!modalActive || working || awaitingJob) return;
          error.textContent = '';
          confirm.disabled = true;
          cancel.disabled = true;
          return maintenanceRequest('first-install', { confirmation: 'CONFIRM' })
            .then(function (result) {
              if (result.submitted !== true) throw Error('未取得安装请求确认，请刷新维护状态。');
              modalActive = false;
              ui.hideModal();
              message('');
              return waitForJob();
            })
            .catch(function (e) {
              error.textContent = e.message;
              confirm.disabled = false;
              cancel.disabled = false;
            });
        },
        true
      );
      modalActive = true;
      ui.showModal(
        '确认首次安装 WARP',
        [
          help(
            '将自动下载、校验并安装路由器所需的 WARP 运行环境。已有可用的缓存文件会先校验再复用。'
          ),
          help(
            '请使用本地或独立管理连接，并先关闭 imOK 总开关、结束登录准备。安装完成后请登录账户。'
          ),
          error,
          modalActions([cancel, confirm])
        ],
        'imok-dialog',
        'imok-runtime-dialog'
      );
    }
    function showPlan(operation, backupID) {
      if (operation === 'install' && !/^[a-f0-9]{64}$/i.test(shaValue.trim()))
        throw Error('请先上传可信的未注册运行环境包，并填写对应的 64 位 SHA-256 校验码');
      return maintenanceRequest('plan', {
        operation: operation,
        sha256: shaValue.trim().toLowerCase(),
        backup_id: backupID || ''
      })
        .catch(function (e) {
          if (operation !== 'online-install') throw e;
          return refresh().then(function () {
            throw e;
          });
        })
        .then(function (r) {
          if (!r.plan || typeof r.plan.id !== 'string') throw Error('未获得有效的维护确认计划');
          var plan = r.plan,
            error = E('p', { role: 'alert' }),
            ack = E('input', { type: 'checkbox' }),
            submitting = false;
          var title = ['install', 'online-install'].includes(operation)
            ? '确认安装到路由器'
            : operation === 'upgrade'
              ? '确认备份并升级 WARP'
              : operation === 'recover'
                ? '确认核对维护阻断'
                : '确认回滚 WARP';
          var cancel = button('取消', function () {
            if (submitting) return;
            modalActive = false;
            ui.hideModal();
          });
          var confirm = button(
            ['install', 'online-install'].includes(operation)
              ? '确认安装'
              : operation === 'upgrade'
                ? '备份并升级'
                : operation === 'recover'
                  ? '核对并恢复'
                  : '确认回滚',
            function () {
              if (submitting || !ack.checked) return;
              submitting = true;
              confirm.disabled = true;
              cancel.disabled = true;
              ack.disabled = true;
              return maintenanceRequest('execute', { plan_id: plan.id, confirmation: 'CONFIRM' })
                .then(function () {
                  modalActive = false;
                  ui.hideModal();
                  message('');
                  return waitForJob();
                })
                .catch(function (e) {
                  submitting = false;
                  confirm.disabled = !ack.checked;
                  cancel.disabled = false;
                  ack.disabled = false;
                  error.textContent = e.message;
                });
            },
            true,
            true
          );
          ack.addEventListener('change', function () {
            confirm.disabled = !ack.checked || submitting;
          });
          modalActive = true;
          ui.showModal(
            title,
            [
              help(
                '操作对象：这台路由器内的 WARP 运行环境。' +
                  (plan.version ? '目标版本：' + plan.version + '。' : '')
              ),
              help(
                '请使用本地或独立的管理连接，并先关闭 imOK 总开关、结束登录准备。' +
                  (['install', 'online-install'].includes(operation)
                    ? '首次安装不会覆盖已有 WARP 环境。'
                    : operation === 'upgrade'
                      ? '升级会保留原环境，方便回滚。'
                      : operation === 'rollback'
                        ? '将使用所选恢复副本，并保留当前账户配置。'
                        : '将核对上次维护记录，不会自动开启隧道。') +
                  '成功后仍需由你手动开启并检查联网。'
              ),
              E('label', { class: 'imok-runtime-ack' }, [
                ack,
                '我已确认管理连接可用，并同意执行本次操作。'
              ]),
              error,
              modalActions([cancel, confirm])
            ],
            'imok-dialog',
            'imok-runtime-dialog'
          );
        });
    }
    function upload() {
      working = true;
      return ensurePrepared()
        .then(function () {
          return new Promise(function (resolve) {
            modalActive = true;
            var file = E('input', {
                type: 'file',
                accept: '.tgz,.tar.gz',
                'aria-label': '选择未注册的运行环境包'
              }),
              error = E('p', { role: 'alert' }),
              uploading = false;
            var progress = E(
              'div',
              {
                class: 'imok-runtime-upload-progress',
                role: 'progressbar',
                'aria-label': '运行环境上传进度',
                'aria-valuemin': 0,
                'aria-valuemax': 100,
                'aria-valuenow': 0
              },
              '等待选择文件'
            );
            var cancel = E(
              'button',
              {
                class: 'btn cbi-button',
                type: 'button',
                click: function () {
                  if (uploading) return;
                  modalActive = false;
                  working = false;
                  ui.hideModal();
                  resolve({ cancelled: true });
                }
              },
              '取消'
            );
            var submit = E(
              'button',
              {
                class: 'btn cbi-button cbi-button-apply',
                type: 'button',
                click: function () {
                  if (uploading) return;
                  var selected = file.files && file.files[0];
                  if (!selected || selected.size <= 0 || selected.size > 256 * 1024 * 1024) {
                    error.textContent = '请选择不超过 256 MB 的未注册运行环境 .tgz 包。';
                    return;
                  }
                  uploading = true;
                  cancel.disabled = true;
                  submit.disabled = true;
                  file.disabled = true;
                  error.textContent = '';
                  var form = new FormData();
                  form.append('sessionid', rpc.getSessionID());
                  form.append('filename', '/opt/routepilot/runtime-uploads/runtime.tgz');
                  form.append('filedata', selected);
                  request
                    .post(L.env.cgi_base + '/cgi-upload', form, {
                      timeout: 0,
                      progress: function (ev) {
                        var value = ev.total
                          ? Math.min(100, Math.round((ev.loaded / ev.total) * 100))
                          : 0;
                        progress.setAttribute('aria-valuenow', String(value));
                        progress.textContent = '正在上传 ' + value + '%';
                      }
                    })
                    .then(function (response) {
                      var reply = response.json();
                      if (!reply || typeof reply !== 'object' || reply.failure)
                        throw Error('upload');
                      modalActive = false;
                      working = false;
                      uploaded = true;
                      ui.hideModal();
                      message(
                        '运行环境包已上传，尚未安装。填写可信来源提供的校验码后，再点击校验并安装。'
                      );
                      draw();
                      resolve({ uploaded: true });
                    })
                    .catch(function () {
                      uploading = false;
                      cancel.disabled = false;
                      submit.disabled = false;
                      file.disabled = false;
                      error.textContent = '文件上传未完成，请检查管理连接、路由器存储空间与权限。';
                    });
                }
              },
              '上传文件'
            );
            ui.showModal(
              '上传路由器 WARP 运行环境',
              [
                help('只接受可信来源的未注册 .tgz 运行环境包；上传成功后仍需校验并确认安装。'),
                file,
                progress,
                error,
                modalActions([cancel, submit])
              ],
              'imok-dialog',
              'imok-runtime-dialog'
            );
          });
        })
        .finally(function () {
          working = false;
        });
    }

    function draw() {
      body.replaceChildren();
      drawJob();
      if (data.load_error) {
        body.appendChild(
          panel('状态暂时无法读取', [
            E('p', { role: 'alert' }, data.load_error),
            button('重新读取状态', refresh)
          ])
        );
        return;
      }
      var busy = working || awaitingJob || data.job.state === 'running',
        disabled = busy || !data.supported || data.recovery_required;
      var env = data.environment || {
          can_prepare: false,
          issues: ['环境检测尚未完成，请刷新本机状态']
        },
        online = data.online || { state: 'idle' };
      var known = data.installed,
        check = data.check,
        checkFailed = check.state === 'failed',
        checking = check.state === 'checking',
        latest = checkFailed ? null : data.latest_version;
      var rows = E('dl', { class: 'imok-runtime-facts' }, [
        E('div', {}, [
          E('dt', {}, '路由器内 WARP'),
          E(
            'dd',
            {},
            known
              ? data.local_version || '维护中，版本暂未确认'
              : data.runtime_exists
                ? '检测到运行文件，状态待核对'
                : '尚未安装'
          )
        ]),
        E('div', {}, [E('dt', {}, '官方可用版本'), E('dd', {}, latest || '尚未取得有效结果')]),
        E('div', {}, [E('dt', {}, '最近成功检查'), E('dd', {}, timeLabel(check.checked_at))]),
        E('div', {}, [
          E('dt', {}, '下次计划检查'),
          E(
            'dd',
            {},
            data.settings.hours
              ? data.next_check_at
                ? timeLabel(data.next_check_at)
                : '等待首次计划检查'
              : '已关闭定期检查'
          )
        ])
      ]);
      if (data.cli_version_verified === false)
        rows.appendChild(
          E('div', {}, [
            E('dt', {}, '客户端兼容性'),
            E('dd', { role: 'status' }, '此版本的 warp-cli 输出尚未验证。')
          ])
        );
      var badgeText = checkFailed
        ? '更新检查失败'
        : !known
          ? '先安装 WARP'
          : checking
            ? '正在检查'
            : data.update_available
              ? '发现可用新版本'
              : latest
                ? '已与官方目录核对'
                : '等待检查官方版本';
      var version = panel('安装与版本状态', [
        E(
          'span',
          {
            class: 'rp-status-badge',
            'data-tone': checkFailed
              ? 'danger'
              : data.update_available
                ? 'warning'
                : known
                  ? 'neutral'
                  : 'warning'
          },
          badgeText
        ),
        rows,
        E('div', { class: 'rp-summary-actions' }, [
          button(
            '检查更新',
            function () {
              message('正在提交版本检查…');
              return maintenanceRequest('check', {}).then(function () {
                message('');
                return refresh();
              });
            },
            false,
            busy
          ),
          button('刷新本机状态', refresh, false, busy)
        ]),
        E('details', { class: 'rp-stat-details' }, [
          E('summary', {}, '版本检查详情'),
          help('检查更新会读取官方版本目录。检查失败时，请重试以获取最新结果。')
        ])
      ]);
      var frequency = E(
        'select',
        {
          'aria-label': '自动检查频率',
          change: function (ev) {
            selectedHours = Number(ev.target.value);
          }
        },
        [
          [24, '每天检查一次'],
          [72, '每三天检查一次'],
          [168, '每周检查一次'],
          [0, '关闭定期检查']
        ].map(function (v) {
          return E('option', { value: String(v[0]) }, v[1]);
        })
      );
      frequency.value = String(selectedHours == null ? data.settings.hours : selectedHours);
      var updates = panel('检查与升级', [
        E('label', { class: 'imok-runtime-field' }, ['检查频率', frequency]),
        E('div', { class: 'rp-summary-actions' }, [
          button(
            '保存检查频率',
            function () {
              return maintenanceRequest('settings', { hours: Number(frequency.value) }).then(
                function () {
                  selectedHours = null;
                  message('检查频率已保存；没有执行安装或升级。');
                  return refresh();
                }
              );
            },
            false,
            busy
          ),
          button(
            '备份并升级',
            function () {
              return showPlan('upgrade');
            },
            true,
            disabled || checkFailed || !data.update_available
          )
        ]),
        help(
          '发现新版后会在这里提示。升级前再次核对版本、空间和占用，在副本中验证官方签名及安装包，再保留原环境并替换。'
        ),
        help('升级、安装和回滚都不会自动开启主线路。')
      ]);
      if (!data.supported)
        body.appendChild(
          E(
            'div',
            { class: 'imok-runtime-notice', 'data-tone': 'warning', role: 'alert' },
            '当前平台不在安装支持范围。仅 ARM64 OpenWrt 24.10 开放维护操作，其他平台可以查看说明与版本信息。'
          )
        );
      if (data.recovery_required)
        body.appendChild(
          panel('上次维护需要核对', [
            E(
              'p',
              { role: 'alert' },
              '检测到未完成的环境替换，已阻止重新启动。请优先选择恢复副本回滚；没有可用副本时，可核对尚未提交的维护记录。'
            ),
            button(
              '核对未完成维护',
              function () {
                return showPlan('recover');
              },
              false,
              busy
            )
          ])
        );
      if (checkFailed)
        version.appendChild(
          E(
            'div',
            { class: 'imok-runtime-notice', 'data-tone': 'danger', role: 'alert' },
            updateCheckError(check.message, known)
          )
        );
      var statusGrid = E('div', { class: 'imok-runtime-grid' }, [version, updates]);
      var sha = E('input', {
        class: 'cbi-input-text',
        type: 'text',
        value: shaValue,
        placeholder: '粘贴可信来源提供的 64 位 SHA-256',
        maxlength: 64,
        autocomplete: 'off',
        spellcheck: false,
        input: function (ev) {
          shaValue = ev.target.value;
        }
      });
      var installBody = [
        help(
          '这是安装到路由器，不是在手机或电脑安装 WARP。插件本身已安装；还需要独立的 Debian 12 ARM64 WARP 运行环境。'
        ),
        E('ol', { class: 'imok-runtime-steps' }, [
          E(
            'li',
            {},
            '从可信来源准备未注册的运行环境 .tgz 包，以及发布者提供的 SHA-256。普通 Linux .deb 和 imOK 插件 IPK 不能在这里作为运行环境包导入。'
          ),
          E('li', {}, '上传文件并核对校验码，确认后安装到路由器；已有环境不会被首次安装覆盖。'),
          E('li', {}, '安装成功后，进入账户与协议登录，再选择出口和接管范围，最后手动开启。')
        ]),
        E('div', { class: 'rp-summary-actions' }, [
          E(
            'button',
            {
              class: 'btn cbi-button',
              type: 'button',
              disabled: disabled || known ? true : null,
              click: function (ev) {
                if (working) return;
                return upload(ev).catch(function (e) {
                  message(e.message, true);
                });
              }
            },
            uploaded ? '重新上传运行环境包' : '上传运行环境包'
          )
        ]),
        E('label', { class: 'imok-runtime-field' }, ['文件校验码（SHA-256）', sha]),
        E('div', { class: 'rp-summary-actions' }, [
          button(
            '校验并安装到路由器',
            function () {
              return showPlan('install');
            },
            true,
            disabled || data.runtime_exists
          )
        ]),
        help(
          '运行环境包需要自行从可信来源取得，本插件不附带第三方 WARP 程序、账户或密钥。遇到不兼容、空间不足或校验不符，会停止并保留原文件。'
        )
      ];
      var ready = online.state === 'ready',
        selfTestFailed = data.job.state === 'failed' && data.job.phase === 'verify_install',
        cachedFiles = downloadComponents.every(function (item) {
          return (online.components || {})[item.id] === 'cached';
        }),
        legacyFailure = !data.job.state && online.state === 'failed';
      var prepareDisabled = disabled || data.runtime_exists || !env.can_prepare;
      var setupBody = [
        E('dl', { class: 'imok-runtime-facts' }, [
          E('div', {}, [
            E('dt', {}, '系统与处理器'),
            E('dd', {}, (env.system || '系统待识别') + ' · ' + (env.architecture || '处理器待识别'))
          ]),
          E('div', {}, [
            E('dt', {}, '路由器可用存储'),
            E(
              'dd',
              {},
              Number.isFinite(env.free_bytes)
                ? (env.free_bytes / 1073741824).toFixed(1) + ' GB'
                : '待检测'
            )
          ])
        ])
      ];
      var setupMessage = '',
        setupTone = 'neutral';
      if (!busy) {
        if (ready) {
          setupMessage = selfTestFailed
            ? '安装文件已保留，可直接重试安装。待安装版本：' + online.version
            : '文件已自动下载并校验，尚未安装。待安装版本：' + online.version;
          setupTone = 'success';
        } else if (selfTestFailed && cachedFiles) {
          setupMessage = '三项文件已缓存，重试时会先校验并复用。';
        } else if (legacyFailure) {
          setupMessage = online.message || '自动准备失败，请检查网络与空间后重试。';
          setupTone = 'danger';
        } else if (online.state === 'expired') {
          setupMessage = online.message || '准备文件已过期，请重新自动下载。';
        } else if (!env.can_prepare) {
          setupMessage = env.issues.join('；') || '当前环境尚不能自动安装。';
        } else if (!data.job.state) {
          setupMessage = '环境检查通过，可以自动下载并安装 WARP。';
        }
      }
      if (setupMessage)
        setupBody.push(
          E(
            'p',
            { class: 'imok-runtime-notice', 'data-tone': setupTone, role: 'status' },
            setupMessage
          )
        );
      setupBody.push(
        E('div', { class: 'rp-summary-actions' }, [
          button(
            ready
              ? selfTestFailed
                ? '重试安装到路由器'
                : '确认安装到路由器'
              : selfTestFailed && cachedFiles
                ? '用缓存重试安装'
                : '自动下载并安装 WARP',
            ready
              ? function () {
                  return showPlan('online-install');
                }
              : showFirstInstall,
            true,
            disabled || data.runtime_exists || (!ready && !env.can_prepare)
          )
        ]),
        help(
          '确认一次后会自动下载、校验并安装。下载失败时可直接重试，已校验的文件会优先复用。安装自检通过后，再到「账户与协议」登录。'
        ),
        E('details', { class: 'imok-panel' }, [
          E('summary', {}, '高级：查看下载文件与分步安装'),
          help('可先只下载和校验文件，再另行确认安装。'),
          componentDownloads(online, prepareDisabled),
          !ready
            ? E('div', { class: 'rp-summary-actions' }, [
                button(
                  selfTestFailed && cachedFiles ? '用缓存重新准备' : '自动下载并准备安装',
                  function () {
                    return prepareOnline();
                  },
                  false,
                  prepareDisabled
                )
              ])
            : null
        ])
      );
      if (!known) {
        body.appendChild(panel('首次使用：安装路由器 WARP', setupBody));
        body.appendChild(
          E(
            'details',
            { class: 'imok-panel' },
            [E('summary', {}, '高级：离线导入运行环境')].concat(installBody)
          )
        );
        body.appendChild(
          E('details', { class: 'imok-panel' }, [
            E('summary', {}, '版本检查与定期计划'),
            statusGrid
          ])
        );
      } else {
        if (!data.recovery_required)
          body.appendChild(
            E('div', { class: 'rp-summary-actions' }, [
              link('下一步：登录 Cloudflare 账户', 'cloudflare/account')
            ])
          );
        body.appendChild(statusGrid);
        body.appendChild(
          E(
            'details',
            { class: 'imok-panel' },
            [E('summary', {}, '高级：首次安装与离线导入说明')].concat(installBody)
          )
        );
      }

      var history = data.backups || [],
        choices = E(
          'select',
          {
            'aria-label': '选择 WARP 恢复副本',
            change: function (ev) {
              selectedBackup = ev.target.value;
            }
          },
          history.map(function (b) {
            return E(
              'option',
              { value: b.id },
              (b.version || '版本未确认') + ' · ' + timeLabel(b.created_at)
            );
          })
        );
      if (
        !history.some(function (b) {
          return b.id === selectedBackup;
        })
      )
        selectedBackup = history.length ? history[0].id : '';
      choices.value = selectedBackup;
      body.appendChild(
        panel('恢复副本与回滚', [
          E('div', { class: 'imok-runtime-history-controls' }, [
            history.length
              ? E('label', { class: 'imok-runtime-field' }, ['选择恢复副本', choices])
              : help('还没有 WARP 恢复副本。升级提交时会保留原环境，不会自动删除已有副本。'),
            button(
              '使用此副本回滚',
              function () {
                return showPlan('rollback', choices.value);
              },
              false,
              busy || !data.supported || !history.length
            )
          ]),
          help(
            '回滚前会验证副本，并保留当前账户配置。普通“备份迁移”的设置导出不包含完整 WARP 运行环境和注册凭据。恢复副本存储在路由器私有目录，请勿公开分享。'
          )
        ])
      );
      body.appendChild(
        E('details', { class: 'imok-panel' }, [
          E('summary', {}, '软件来源与适用范围'),
          help(
            '支持范围：ARM64 OpenWrt 24.10；运行环境为 Debian 12 bookworm。官方稳定源：https://pkg.cloudflareclient.com/。'
          ),
          help(
            '目录检查通过不等于软件已安装、隧道已连接或网站访问成功。公钥变化、依赖不满足或程序自检失败时，不提交升级。'
          ),
          E(
            'a',
            {
              href: 'https://pkg.cloudflareclient.com/',
              target: '_blank',
              rel: 'noreferrer noopener'
            },
            '查看 Cloudflare 官方软件源说明'
          )
        ])
      );
    }
    draw();
    poll.add(function () {
      if (
        !root.isConnected ||
        working ||
        reading ||
        modalActive ||
        data.load_error ||
        (!awaitingJob && data.job.state !== 'running' && data.check.state !== 'checking')
      )
        return;
      return refresh().catch(function (e) {
        message(e.message, true);
      });
    }, 2);
    return root;
  }
});

'use strict';
'require baseclass';

return baseclass.extend({
  render: function (panel, command, notify, boundRequest, fs, uci) {
    function backup() {
      return command('settings-export')
        .then(function (raw) {
          var b = new Blob([raw], { type: 'application/json' }),
            u = URL.createObjectURL(b);
          E('a', { href: u, download: 'routepilot-settings-v3.json' }).click();
          URL.revokeObjectURL(u);
        })
        .catch(function (e) {
          notify([_('配置导出失败。'), e.message, _('请核对提示后重试。')].join(' '), true);
        });
    }
    function restore(file) {
      if (!file || file.size > 262144) {
        notify(_('备份文件过大或无效'), true);
        return Promise.resolve();
      }
      var importRequest;
      return file
        .text()
        .then(function (raw) {
          var b = JSON.parse(raw);
          if (['routepilot-masque-v2', 'routepilot-masque-v3'].indexOf(b.format) < 0)
            throw Error(_('备份格式无效'));
          importRequest = boundRequest(b);
          return fs.write('/var/run/routepilot/settings-import-input', importRequest.text, 384);
        })
        .then(function () {
          return command('settings-import', importRequest.token);
        })
        .then(function (raw) {
          var result = JSON.parse(raw);
          if (!result.imported || result.request_id !== importRequest.id)
            throw Error(_('未收到本次导入确认'));
          return uci.unload('routepilot');
        })
        .then(function () {
          return uci.load('routepilot');
        })
        .then(function () {
          notify(
            _(
              '已导入并保持关闭。请刷新页面；入口偏好需核对后单独保存应用，登录和备用凭据需另行配置。'
            )
          );
        })
        .catch(function (e) {
          notify(
            [_('导入结果未确认。'), e.message, _('请刷新页面核对配置后再重试。')].join(' '),
            true
          );
        });
    }
    return E('div', { class: 'imok-settings-pair imok-backup-layout' }, [
      panel(_('导出配置'), _('下载 v3 配置备份。'), [
        E('button', { class: 'btn', click: backup }, _('导出配置')),
        E('details', { class: 'rp-help' }, [
          E('summary', {}, _('备份范围')),
          E(
            'p',
            {},
            _(
              '包含账户组织名称、协议、出口、接管范围、设备策略、DNS、规则来源与更新间隔、自定义分流和主线路入口偏好。'
            )
          ),
          E(
            'p',
            {},
            _(
              '请单独保留个人规则新增和排除、WARP 更新检查频率、运行环境、下载规则和历史统计，以及路由器身份、API 令牌、备用节点凭据、客户端独立身份及私密订阅凭据。'
            )
          )
        ])
      ]),
      panel(_('导入与恢复'), _('选择文件即导入，完成后分流关闭。'), [
        E('label', { class: 'imok-import-field' }, [
          _('导入配置文件：'),
          E('input', {
            type: 'file',
            accept: '.json',
            change: function (ev) {
              restore(ev.target.files[0]);
            }
          })
        ]),
        E('p', {}, _('导入后请核对入口偏好，再启用分流。')),
        E(
          'p',
          { class: 'rp-help' },
          _(
            '支持 v2/v3 JSON，文件上限 256 KiB。导出前会检查可导入性，超过上限会明确报错，不截断设置。'
          )
        )
      ])
    ]);
  }
});

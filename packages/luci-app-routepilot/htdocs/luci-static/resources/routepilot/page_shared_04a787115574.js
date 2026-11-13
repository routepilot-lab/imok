'use strict';
'require baseclass';

return baseclass.extend({
  readRuntimeFiles: function (runtimeFiles, runtimeSlots, applyRuntimeFiles, fs) {
    return Promise.all(
      runtimeFiles.map(function (file, index) {
        return runtimeSlots.indexOf(index) >= 0
          ? fs
              .read('/var/run/routepilot/' + file)
              .then(JSON.parse)
              .catch(function () {
                return null;
              })
          : Promise.resolve(null);
      })
    ).then(applyRuntimeFiles);
  },
  panel: function (title, description, children, extra) {
    return E('section', { class: 'imok-panel' + (extra ? ' ' + extra : '') }, [
      E(
        'div',
        { class: 'imok-panel-heading' },
        [E('h3', {}, title), description ? E('p', { class: 'rp-help' }, description) : null].filter(
          function (item) {
            return item != null;
          }
        )
      ),
      E('div', { class: 'imok-panel-body' }, children)
    ]);
  },
  helpDetails: function (description) {
    return E('details', { class: 'rp-help' }, [
      E('summary', {}, _('详情')),
      E('p', {}, description)
    ]);
  },
  command: function (fs, action, requestId) {
    return fs
      .exec('/usr/libexec/routepilot', requestId ? [action, requestId] : [action])
      .then(function (r) {
        if (r.code) throw new Error(r.stderr || _('操作未完成'));
        return r.stdout;
      });
  },
  bytes: function (n) {
    if (!Number.isFinite(n) || n < 0) return '—';
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'],
      i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return (i ? n.toFixed(n < 10 ? 2 : 1) : String(Math.round(n))) + ' ' + units[i];
  },
  when: function (ts, fallback) {
    var date = new Date(Number(ts) * 1000);
    if (!ts || !Number.isFinite(date.getTime())) return fallback || _('暂无成功记录');
    function pad(n) {
      return String(n).padStart(2, '0');
    }
    return (
      [date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join('-') +
      ' ' +
      [date.getHours(), date.getMinutes(), date.getSeconds()].map(pad).join(':')
    );
  },
  decorateFields: function (root) {
    if (!root.querySelectorAll) return root;
    Array.prototype.forEach.call(root.querySelectorAll('.cbi-value'), function (row) {
      row.classList.add('imok-form-row');
      if (
        row.querySelector('.cbi-value-field input[type="checkbox"]') &&
        !row.querySelector('.cbi-dropdown')
      )
        row.classList.add('imok-form-check');
      var description = row.querySelector('.cbi-value-description');
      if (
        !description ||
        description.closest('details') ||
        description.textContent.trim().length <= 30
      )
        return;
      var details = E('details', { class: 'rp-help imok-field-help' }, E('summary', {}, _('详情')));
      description.replaceWith(details);
      details.appendChild(description);
    });
    return root;
  },
  statusBar: function (tone, children, extra) {
    return E(
      'div',
      { class: 'rp-layout-status' + (extra ? ' ' + extra : ''), 'data-tone': tone },
      children
    );
  }
});

// Run with ego-browser nodejs against the loopback-only preview server.
const fs = await import('node:fs/promises');
const task = await taskSpace('imOK layout audit');
const page = task.page('p1');
const origin = 'http://127.0.0.1:8767';
const { evidence_directory: folder } = await (await fetch(origin + '/meta.json')).json();
const routes = [
  'overview',
  'runtime',
  'cloudflare/account',
  'cloudflare/endpoint',
  'cloudflare/routing',
  'cloudflare/devices',
  'cloudflare/dns',
  'cloudflare/workers',
  'rules/overview',
  'rules/sources',
  'rules/manage',
  'rules/custom',
  'clients',
  'backup'
];
const report = [];
await fs.mkdir(folder, { recursive: true });

try {
  for (const route of routes) {
    for (const width of [1440, 768, 375]) {
      await page.cdp('Emulation.setDeviceMetricsOverride', {
        width,
        height: 950,
        deviceScaleFactor: 1,
        mobile: width < 768
      });
      await page.goto(origin + '/cgi-bin/luci/admin/services/routepilot/' + route);
      await page.waitForFunction(() => window.previewReady || window.previewError, undefined, {
        timeout: 10000
      });
      const metrics = await page.evaluate(async () => {
        if (window.previewError) throw Error(window.previewError);
        await document.fonts.ready;
        const root = document.querySelector('.rp-layout');
        const visible = (el) => el.getClientRects().length > 0;
        const query = (selector) => [...root.querySelectorAll(selector)].filter(visible);
        const wrapped = query('button.btn, a.btn')
          .filter((el) => {
            const label = [...el.childNodes].find(
              (node) => node.nodeType === 3 && node.textContent.trim()
            );
            if (!label) return false;
            const range = document.createRange();
            range.selectNode(label);
            return (
              new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => r.top))
                .size > 1
            );
          })
          .map((el) => el.textContent.trim());
        const strayText = query('.rp-page-content, .cbi-map, .rp-center-body').flatMap((el) =>
          [...el.childNodes]
            .filter((n) => n.nodeType === 3 || n.nodeName === 'P')
            .map((n) => n.textContent.trim())
            .filter(Boolean)
        );
        const primary = [
          ...document.querySelectorAll(
            '.imok-page-actions .cbi-button-apply, .rp-layout .cbi-button-action, .rp-layout .cbi-button-apply'
          )
        ]
          .filter(visible)
          .filter((el) => getComputedStyle(el).backgroundColor === 'rgb(37, 99, 235)')
          .map((el) => el.innerText.trim());
        const cardRows = query(
          '.imok-settings-pair, .imok-route-layout, .imok-overview-main'
        ).flatMap((el) => {
          const groups = new Map();
          for (const child of [...el.children].filter(visible)) {
            const rect = child.getBoundingClientRect();
            const top = Math.round(rect.top);
            const heights = groups.get(top) || [];
            heights.push(rect.height);
            groups.set(top, heights);
          }
          return [...groups.values()].filter((heights) => heights.length > 1);
        });
        return {
          height: Math.round(root.getBoundingClientRect().height),
          overflow: document.documentElement.scrollWidth > innerWidth,
          wrapped,
          strayText,
          primary,
          unevenCards: cardRows.filter((row) => Math.max(...row) - Math.min(...row) > 1),
          unmarkedForms: query('.cbi-value:not(.imok-form-row)').length,
          legacyTime: /\b(?:\d{1,2}\/\d{1,2}\/\d{4}|\d{1,2}:\d{2}:\d{2}\s*[AP]M)\b/.test(
            root.innerText
          )
        };
      });
      report.push({ route, width, ...metrics });
    }
  }
  await fs.writeFile(folder + '/layout-report.json', JSON.stringify(report, null, 2));
  const issues = report.filter(
    (r) =>
      r.overflow ||
      r.wrapped.length ||
      r.strayText.length ||
      r.primary.length > 1 ||
      r.unevenCards.length ||
      r.unmarkedForms ||
      r.legacyTime ||
      (r.route === 'cloudflare/dns' && r.width === 1440 && r.height > 1600)
  );
  console.log({ cases: report.length, issues });
  if (issues.length) throw Error('Layout audit found regressions');
} finally {
  await task.finish({ keep: [] });
}

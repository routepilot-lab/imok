const fs = require('node:fs');
const path = require('node:path');

module.exports = function loadPages(packageRoot, view, Base, createElement, translate) {
  const moduleDirectory = path.join(packageRoot, 'htdocs/luci-static/resources/routepilot');
  const imports = view.matchAll(/'require routepilot\.(page_[a-z]+_[a-f0-9]{12}) as (page[A-Z][A-Za-z]*)';/g);
  return Object.fromEntries(Array.from(imports, ([, moduleName, alias]) => {
    const source = fs.readFileSync(path.join(moduleDirectory, moduleName + '.js'), 'utf8');
    const Page = new Function('baseclass', 'E', '_', source)(Base, createElement, translate);
    return [alias, new Page()];
  }));
};

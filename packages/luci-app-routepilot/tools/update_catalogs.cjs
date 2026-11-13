// Extract literal LuCI messages and keep existing translations across updates.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const acorn = require('acorn');
const root = path.resolve(__dirname, '..');

function collect(directory, messages) {
  for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
    const filename = path.join(directory, item.name);
    if (item.isDirectory()) {
      collect(filename, messages);
    } else if (item.name.endsWith('.js')) {
      const tree = acorn.parse(fs.readFileSync(filename, 'utf8'), {
        ecmaVersion: 'latest',
        allowReturnOutsideFunction: true
      });
      function visit(node) {
        if (!node || typeof node !== 'object') return;
        if (
          node.type === 'CallExpression' &&
          node.callee.name === '_' &&
          typeof node.arguments[0]?.value === 'string'
        ) {
          messages.add(node.arguments[0].value);
        }
        Object.values(node).forEach(visit);
      }
      visit(tree);
    }
  }
}

function translations(filename) {
  if (!fs.existsSync(filename)) return new Map();
  return new Map(
    Array.from(
      fs.readFileSync(filename, 'utf8').matchAll(/^msgid (".*")\nmsgstr (".*")$/gm),
      ([, id, value]) => [JSON.parse(id), JSON.parse(value)]
    ).filter(([id, value]) => id && value)
  );
}

const menu = JSON.parse(
  fs.readFileSync(path.join(root, 'root/usr/share/luci/menu.d/luci-app-routepilot.json'), 'utf8')
);
const messages = new Set(
  Object.values(menu)
    .map((entry) => entry.title)
    .filter(Boolean)
);
collect(path.join(root, 'htdocs'), messages);
const quote = JSON.stringify;
const ordered = [...messages].filter(Boolean).sort();
const destinations = [
  ['templates/routepilot.pot', ''],
  ['zh_Hans/routepilot.po', 'zh_Hans'],
  ['en/routepilot.po', 'en']
];
for (const [relative, language] of destinations) {
  const filename = path.join(root, 'po', relative);
  const existing = translations(filename);
  const header = [
    '# imOK LuCI messages.',
    'msgid ""',
    'msgstr ""',
    quote('Content-Type: text/plain; charset=UTF-8\n'),
    ...(language ? [quote('Language: ' + language + '\n')] : [])
  ];
  const entries = ordered.map((message) =>
    ['msgid ' + quote(message), 'msgstr ' + quote(existing.get(message) || '')].join('\n')
  );
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, header.join('\n') + '\n\n' + entries.join('\n\n') + '\n');
}

if (process.argv[2] === '--compile') {
  const executable = process.argv[3] || 'po2lmo';
  const entries = new Map();
  for (const [message, value] of translations(path.join(root, 'po/en/routepilot.po'))) {
    // LuCI folds ASCII whitespace before looking up a message.
    const key = message.replace(/[\t\n\v\f\r ]+/g, ' ').replace(/^ | $/g, '');
    if (entries.has(key) && entries.get(key) !== value)
      throw new Error('Conflicting translations for ' + quote(key));
    entries.set(key, value);
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'imok-luci-'));
  try {
    const input = path.join(directory, 'en.po');
    fs.writeFileSync(
      input,
      [...entries]
        .map(([key, value]) => 'msgid ' + quote(key) + '\nmsgstr ' + quote(value))
        .join('\n\n') + '\n'
    );
    const destination = path.join(root, 'root/usr/lib/lua/luci/i18n/routepilot.en.lmo');
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    execFileSync(executable, [input, destination]);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

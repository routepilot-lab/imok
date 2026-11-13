export default [
  {
    files: ['packages/luci-app-routepilot/htdocs/**/*.js', 'packages/luci-app-routepilot/tests/*.cjs'],
    languageOptions: { sourceType: 'script', parserOptions: { ecmaFeatures: { globalReturn: true } } },
    rules: {
      'no-debugger': 'error',
      'no-dupe-keys': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
    },
  },
];

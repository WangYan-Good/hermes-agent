import { builtinModules } from 'node:module'

import shared from '../../eslint.config.shared.mjs'

export default [
  ...shared,
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: [...new Set(builtinModules.map(name => name.replace(/^node:/, '')))],
        patterns: [{
          group: ['@/*', '@hermes/desktop', '@hermes/desktop/**', '**/desktop/**', '**/web/**', 'electron', 'electron/**', 'node-pty', 'node-pty/**', 'node:*'],
          message: 'Chat UI must remain host-neutral; host state and native APIs belong to the consumer.'
        }]
      }],
      'no-restricted-syntax': ['error', {
        selector: 'MemberExpression[property.name="hermesDesktop"], MemberExpression[property.value="hermesDesktop"]',
        message: 'The Desktop bridge belongs to the host, not shared chat UI.'
      }, {
        selector: 'ImportExpression:not([source.value="@streamdown/code"]):not([source.value="./shiki-block"]), CallExpression[callee.name="require"]',
        message: 'Use static imports so the chat UI portability boundary can be checked.'
      }]
    }
  }
]

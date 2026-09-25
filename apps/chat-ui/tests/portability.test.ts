// @vitest-environment node
import { readdirSync, readFileSync } from 'node:fs'
import { builtinModules } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { expect, it } from 'vitest'

const root = join(dirname(fileURLToPath(import.meta.url)), '../src')
const builtins = new Set(builtinModules.map(name => name.replace(/^node:/, '')))
function violations(code: string, name: string) {
  const found: string[] = []
  const tree = ts.createSourceFile(name, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  function inspect(node: ts.Node) {
    if (ts.isStringLiteralLike(node)) {
      const parent = node.parent
      if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent) || ts.isCallExpression(parent)) {
        if (builtins.has(node.text.replace(/^node:/, '')) || /^(node:|electron|@\/)|(?:^|\/)desktop(?:\/|$)|(?:^|\/)web(?:\/|$)/.test(node.text)) found.push(node.text)
      }
    }
    if ((ts.isIdentifier(node) || ts.isStringLiteralLike(node)) && node.text === 'hermesDesktop') found.push('hermesDesktop')
    ts.forEachChild(node, inspect)
  }
  inspect(tree)
  return found
}
function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => entry.isDirectory() ? files(join(dir, entry.name)) : /\.(ts|tsx)$/.test(entry.name) ? [join(dir, entry.name)] : [])
}
it('keeps renderer modules independent of hosts and native APIs', () => {
  for (const file of files(root)) expect(violations(readFileSync(file, 'utf8'), file), file).toEqual([])
})
it('detects static, lazy and bridge boundary mutations', () => {
  for (const code of ["import fs from 'node:fs'", "export * from '../../desktop/src/store/session'", "import('electron')", "window['hermesDesktop'].open()", "import x from '@/store/session'", "import('/web/routes')"]) expect(violations(code, 'mutant.tsx').length).toBeGreaterThan(0)
})

/**
 * Build an installable tarball of the mail plugin.
 *
 * DSH installs plugins from npm packages: a directory with `package.json` whose
 * `dsh.bundle.patch` points at a Cordis patch, plus everything the plugin needs
 * at runtime. Our repository keeps the Python Core beside the plugin, so this
 * script stages a self-contained copy (`<pkg>/mail-core`) and packs it.
 *
 * Usage:
 *   node scripts/build-package.mjs [--out <dir>]
 *
 * Output: <out>/dsh-mail-plugin-<version>.tgz  (default out: ./dist)
 */

import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)))
const pluginDir = join(repoRoot, 'dsh-mail-plugin')
const coreDir = join(repoRoot, 'mail-core')
const stageRoot = join(repoRoot, '.tmp', 'package-stage')
const stagePkg = join(stageRoot, 'dsh-mail-plugin')

const outArg = process.argv.indexOf('--out')
const outDir = resolve(outArg >= 0 ? process.argv[outArg + 1] : join(repoRoot, 'dist'))

/** Python files that ship with the Core; tests and caches never do. */
function copyCore(from, to) {
  cpSync(from, to, {
    recursive: true,
    filter(source) {
      const name = source.split(/[\\/]/).pop() ?? ''
      if (name === 'tests' || name === '__pycache__') return false
      if (name.endsWith('.pyc') || name.endsWith('.pyo')) return false
      if (name === 'mail.db' || name.startsWith('mail.db-')) return false
      return true
    },
  })
}

function main() {
  if (!existsSync(join(pluginDir, 'lib', 'index.js'))) {
    console.error(`plugin entry not found: ${pluginDir}`)
    return 1
  }
  if (!existsSync(join(coreDir, 'mail_core', '__main__.py'))) {
    console.error(`python core not found: ${coreDir}`)
    return 1
  }

  rmSync(stageRoot, { recursive: true, force: true })
  mkdirSync(stagePkg, { recursive: true })

  for (const entry of ['lib', 'locale', 'icon.svg', 'cordis.patch.yml']) {
    cpSync(join(pluginDir, entry), join(stagePkg, entry), { recursive: true })
  }
  copyCore(coreDir, join(stagePkg, 'mail-core'))

  const manifest = JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8'))
  delete manifest.private
  manifest.files = ['lib', 'locale', 'icon.svg', 'cordis.patch.yml', 'mail-core']
  writeFileSync(join(stagePkg, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')

  mkdirSync(outDir, { recursive: true })

  // pnpm is bundled with the DSH runtime; fall back to npm if it is on PATH.
  const pnpmBin = join(process.env.DSH_HOME || join(process.env.USERPROFILE || '', '.dsh'), 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'pnpm', 'bin', 'pnpm.cjs')
  const packers = existsSync(pnpmBin) ? [[process.execPath, [pnpmBin, 'pack', '--pack-destination', outDir]]] : [['npm', ['pack', '--pack-destination', outDir]]]

  for (const [command, args] of packers) {
    const result = spawnSync(command, args, { cwd: stagePkg, stdio: 'inherit' })
    if (result.status === 0) {
      console.log(`packed into ${outDir}`)
      return 0
    }
    console.error(`${command} pack failed with status ${result.status}`)
  }
  return 1
}

process.exit(main())

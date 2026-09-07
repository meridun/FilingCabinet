#!/usr/bin/env node
/**
 * ESLint error-count ratchet (per-rule baseline gate) — reference implementation.
 *
 * A lane gate of "run `<LINT_CMD>` clean" is unenforceable on any adopter with
 * a lint backlog: a worker cannot tell its own breakage from inherited errors,
 * so the gate is either ignored or blocks everything. This guard grandfathers
 * the existing errors **per rule id** and fails only on growth — counts may
 * hold or shrink, never grow — so the backlog burns down opportunistically
 * (per file, when touched) while new code cannot add to it.
 *
 * Comparison walks the **union** of baseline and current rule ids, so a rule id
 * that is not in the baseline at all (a newly introduced error class) also
 * fails, not just growth of an already-red rule.
 *
 * Determinism across hosts is the whole value of the gate:
 *   - counting runs through the ESLint Node API (no shell-quoting / output-size
 *     differences between a Windows dev host and Linux CI);
 *   - `cache: false` always — a stale `.eslintcache` written by an interactive
 *     `eslint --cache` run would yield phantom counts;
 *   - rules that resolve imports (e.g. `n/no-missing-import`) need an installed
 *     tree, so only run `--update` from a clean, fully installed checkout.
 *
 * Zero dependencies of its own: ESLint is resolved from the adopting repo
 * (`eslint` must be a devDependency there). This template repo has no ESLint
 * config, so the script is exercised here through its pure helpers only; the
 * full-tree assertion is deliberately a CI step in the adopter, not a unit test
 * (a repo-wide lint can take tens of seconds).
 *
 *   node sdlc/tools/check-lint-baseline.mjs              # check (CI + lane gate)
 *   node sdlc/tools/check-lint-baseline.mjs --update     # re-baseline after a burn-down
 *   node sdlc/tools/check-lint-baseline.mjs --baseline <path>   # non-default baseline file
 *
 * Exit codes: 0 within baseline · 1 growth detected · 2 ESLint itself failed to
 * load or run (not a baseline verdict).
 */

import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';
import { fileURLToPath } from 'url';

/** Repo root, resolved from this script's location (sdlc/tools/ → ../..). */
export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

/** Default committed baseline: sibling of this script so it travels with `sdlc/tools/`. */
export const BASELINE_PATH = path.join(REPO_ROOT, 'sdlc', 'tools', 'lint-baseline.json');

/**
 * Bucket key for severity-2 messages with no rule id — parse errors and config
 * failures. Baselined at 0 (absent), so a syntax error fails the gate.
 */
export const FATAL_KEY = '(parse-error)';

/**
 * Load the adopting repo's ESLint class. Resolved relative to `cwd` (not this
 * file) so a copied `sdlc/tools/` finds the project's own devDependency.
 * @param {string} [cwd=REPO_ROOT]
 * @returns {Promise<typeof import('eslint').ESLint>}
 */
export async function loadESLint(cwd = REPO_ROOT) {
  const require = createRequire(path.join(cwd, 'package.json'));
  const resolved = require.resolve('eslint');
  const mod = await import(pathToFileUrl(resolved));
  return mod.ESLint ?? mod.default?.ESLint;
}

function pathToFileUrl(p) {
  // Windows absolute paths need the file:// scheme for dynamic import().
  return new URL(`file://${p.startsWith('/') ? '' : '/'}${p.replace(/\\/g, '/')}`).href;
}

/**
 * Lint the whole repo through the ESLint Node API.
 * @param {string} [cwd=REPO_ROOT] - directory to lint
 * @param {object} [options={}] - extra ESLint constructor options (tests use
 *   `overrideConfigFile`/`overrideConfig` to lint a temp dir)
 * @param {object} [ESLintClass] - injected class (tests); default: loaded from `cwd`
 * @returns {Promise<Array<object>>} ESLint LintResult entries
 */
export async function lintRepo(cwd = REPO_ROOT, options = {}, ESLintClass = null) {
  const ESLint = ESLintClass ?? (await loadESLint(cwd));
  const eslint = new ESLint({ cwd, cache: false, ...options });
  return eslint.lintFiles(['.']);
}

/**
 * Tally error-severity messages per rule id.
 *
 * Warnings (severity 1) and `suppressedMessages` are excluded — warnings are
 * policy, not a gate, and suppressed messages are already opted out by a
 * disable comment.
 * @param {Array<object>} results - ESLint LintResult entries
 * @returns {Record<string, number>} rule id → error count
 */
export function aggregateCounts(results) {
  const counts = {};
  for (const result of results ?? []) {
    for (const message of result?.messages ?? []) {
      if (message?.severity !== 2) continue;
      const key = message.ruleId ?? FATAL_KEY;
      counts[key] = (counts[key] ?? 0) + 1;
    }
  }
  return counts;
}

/**
 * Compare current per-rule counts against a baseline. A rule above baseline is a
 * violation; a rule below is a (passing) ratchet opportunity. Iterates the union
 * of both key sets, so a rule id absent from the baseline but present now is a
 * violation with `base: 0`.
 * @param {Record<string, number>} counts
 * @param {Record<string, number>} baseline
 * @returns {{ok:boolean, violations:Array<{rule:string,base:number,cur:number}>, decreases:Array<{rule:string,base:number,cur:number}>}}
 */
export function compareToBaseline(counts, baseline) {
  const violations = [];
  const decreases = [];
  const rules = new Set([...Object.keys(baseline ?? {}), ...Object.keys(counts ?? {})]);
  for (const rule of [...rules].sort()) {
    const cur = counts?.[rule] ?? 0;
    const base = baseline?.[rule] ?? 0;
    if (cur > base) violations.push({ rule, base, cur });
    else if (cur < base) decreases.push({ rule, base, cur });
  }
  return { ok: violations.length === 0, violations, decreases };
}

/**
 * Read a committed baseline, or `{}` when absent (every rule then baselines at
 * 0, i.e. any error fails).
 * @param {string} [file=BASELINE_PATH]
 * @returns {Record<string, number>}
 */
export function readBaseline(file = BASELINE_PATH) {
  if (!fs.existsSync(file)) return {};
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

/**
 * Write a baseline with sorted keys (stable diffs) and a trailing newline.
 * @param {Record<string, number>} counts
 * @param {string} [file=BASELINE_PATH]
 * @returns {void}
 */
export function writeBaseline(counts, file = BASELINE_PATH) {
  const sorted = {};
  for (const rule of Object.keys(counts).sort()) sorted[rule] = counts[rule];
  fs.writeFileSync(file, `${JSON.stringify(sorted, null, 2)}\n`);
}

/** Total of all per-rule counts. */
export function total(counts) {
  return Object.values(counts ?? {}).reduce((sum, n) => sum + n, 0);
}

/**
 * Render the check outcome as log lines + exit code. Pure, so the CLI surface is
 * testable without ESLint.
 * @param {Record<string, number>} counts
 * @param {Record<string, number>} baseline
 * @param {string} [updateHint] - the command that re-baselines
 * @returns {{code:number, lines:string[]}}
 */
export function reportCheck(counts, baseline, updateHint = 'node sdlc/tools/check-lint-baseline.mjs --update') {
  const { ok, violations, decreases } = compareToBaseline(counts, baseline);
  const lines = [];
  if (!ok) {
    lines.push('lint-baseline: FAIL — new ESLint errors detected.');
    lines.push('Fix them in the files you touched; the backlog is grandfathered, growth is not.');
    for (const v of violations) lines.push(`  ${v.rule}: ${v.cur} > baseline ${v.base} (+${v.cur - v.base})`);
    lines.push(`Only for an intentional re-baseline (burn-down): ${updateHint}`);
    return { code: 1, lines };
  }
  if (decreases.length > 0) {
    lines.push('lint-baseline: OK — and some rules shrank. Lock in the ratchet with:');
    lines.push(`  ${updateHint}`);
    for (const d of decreases) lines.push(`  ${d.rule}: ${d.cur} < baseline ${d.base} (-${d.base - d.cur})`);
    return { code: 0, lines };
  }
  lines.push(
    `lint-baseline: OK — ${total(counts)} errors across ${Object.keys(counts).length} rules, within baseline.`,
  );
  return { code: 0, lines };
}

/** Parse argv: `--update`, `--baseline <path>`. */
export function parseArgs(argv) {
  const out = { update: false, baseline: BASELINE_PATH };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--update') out.update = true;
    else if (argv[i] === '--baseline' && argv[i + 1]) out.baseline = path.resolve(argv[++i]);
  }
  return out;
}

/** CLI entry: check (default) or re-baseline (--update). */
async function main() {
  const { update, baseline: baselineFile } = parseArgs(process.argv.slice(2));
  let counts;
  try {
    counts = aggregateCounts(await lintRepo(process.cwd()));
  } catch (err) {
    console.error(`lint-baseline: could not run ESLint — ${String(err.message).split('\n')[0]}`);
    console.error('(eslint must be installed in this repo; run from the repo root.)');
    process.exit(2);
  }

  if (update) {
    writeBaseline(counts, baselineFile);
    console.log(
      `lint-baseline: updated → ${total(counts)} errors across ${Object.keys(counts).length} rules (${baselineFile}).`,
    );
    return;
  }

  const { code, lines } = reportCheck(counts, readBaseline(baselineFile));
  for (const line of lines) (code ? console.error : console.log)(line);
  // A CI gate has to fail the process; a throw would print a stack instead of
  // the per-rule diff above.
  if (code) process.exit(code);
}

// Run only when invoked as a script (not when imported by tests).
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}

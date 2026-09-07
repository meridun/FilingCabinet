// Tests for sdlc/tools/check-lint-baseline.mjs — zero-dependency, node's built-in runner:
//   node --test "test/*.test.mjs"
// The "committed baseline passes on the live tree" assertion is deliberately
// CI's job in an adopting repo, not a unit test here: this template has no
// ESLint config, and a repo-wide lint is too slow for a unit suite anyway.

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import {
  FATAL_KEY,
  BASELINE_PATH,
  aggregateCounts,
  compareToBaseline,
  readBaseline,
  writeBaseline,
  reportCheck,
  parseArgs,
  lintRepo,
  total,
} from '../sdlc/tools/check-lint-baseline.mjs';

describe('check-lint-baseline: aggregateCounts', () => {
  it('counts error-severity messages per rule id', () => {
    const counts = aggregateCounts([
      { messages: [{ severity: 2, ruleId: 'no-undef' }, { severity: 2, ruleId: 'no-unused-vars' }] },
    ]);
    assert.deepEqual(counts, { 'no-undef': 1, 'no-unused-vars': 1 });
  });

  it('ignores warning-severity messages', () => {
    const counts = aggregateCounts([
      { messages: [{ severity: 1, ruleId: 'no-undef' }, { severity: 2, ruleId: 'no-undef' }] },
    ]);
    assert.deepEqual(counts, { 'no-undef': 1 });
  });

  it('sums the same rule across multiple result entries', () => {
    const counts = aggregateCounts([
      { messages: [{ severity: 2, ruleId: 'no-undef' }] },
      { messages: [{ severity: 2, ruleId: 'no-undef' }, { severity: 2, ruleId: 'no-undef' }] },
    ]);
    assert.deepEqual(counts, { 'no-undef': 3 });
  });

  it('buckets a null ruleId (parse/config failure) under the fatal key', () => {
    const counts = aggregateCounts([{ messages: [{ severity: 2, ruleId: null, fatal: true }] }]);
    assert.deepEqual(counts, { [FATAL_KEY]: 1 });
  });

  it('does not count suppressedMessages', () => {
    const counts = aggregateCounts([
      { messages: [], suppressedMessages: [{ severity: 2, ruleId: 'no-undef' }] },
    ]);
    assert.deepEqual(counts, {});
  });

  it('returns an empty tally for empty or undefined results', () => {
    assert.deepEqual(aggregateCounts([]), {});
    assert.deepEqual(aggregateCounts(undefined), {});
  });
});

describe('check-lint-baseline: compareToBaseline', () => {
  it('passes when every rule is at or below baseline', () => {
    const res = compareToBaseline({ 'no-undef': 5, 'no-unused-vars': 3 }, { 'no-undef': 5, 'no-unused-vars': 3 });
    assert.equal(res.ok, true);
    assert.deepEqual(res.violations, []);
    assert.deepEqual(res.decreases, []);
  });

  it('fails and reports the delta when a baselined rule grows', () => {
    const res = compareToBaseline({ 'no-undef': 6 }, { 'no-undef': 5 });
    assert.equal(res.ok, false);
    assert.deepEqual(res.violations, [{ rule: 'no-undef', base: 5, cur: 6 }]);
  });

  it('fails on a rule id absent from the baseline (newly introduced class)', () => {
    const res = compareToBaseline({ 'no-undef': 5, 'no-dupe-keys': 1 }, { 'no-undef': 5 });
    assert.equal(res.ok, false);
    assert.deepEqual(res.violations, [{ rule: 'no-dupe-keys', base: 0, cur: 1 }]);
  });

  it('reports a shrunk rule as a decrease and still passes', () => {
    const res = compareToBaseline({ 'no-undef': 3 }, { 'no-undef': 5 });
    assert.equal(res.ok, true);
    assert.deepEqual(res.decreases, [{ rule: 'no-undef', base: 5, cur: 3 }]);
  });

  it('treats a rule that disappeared as a decrease to zero', () => {
    const res = compareToBaseline({}, { 'no-undef': 2 });
    assert.equal(res.ok, true);
    assert.deepEqual(res.decreases, [{ rule: 'no-undef', base: 2, cur: 0 }]);
  });

  it('tolerates undefined inputs', () => {
    assert.equal(compareToBaseline(undefined, undefined).ok, true);
  });
});

describe('check-lint-baseline: readBaseline / writeBaseline', () => {
  it('round-trips with sorted keys and a trailing newline', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lint-baseline-'));
    const file = path.join(dir, 'lint-baseline.json');
    try {
      writeBaseline({ 'z-rule': 2, 'a-rule': 1 }, file);
      const text = fs.readFileSync(file, 'utf8');
      assert.equal(text.endsWith('\n'), true);
      assert.deepEqual(Object.keys(JSON.parse(text)), ['a-rule', 'z-rule']);
      assert.deepEqual(readBaseline(file), { 'a-rule': 1, 'z-rule': 2 });
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('reads {} for a missing file (every rule baselines at 0)', () => {
    assert.deepEqual(readBaseline(path.join(os.tmpdir(), 'no-such-baseline-xyz.json')), {});
  });

  it('the committed baseline parses as a flat rule-id → count map', () => {
    const baseline = readBaseline(BASELINE_PATH);
    assert.equal(typeof baseline, 'object');
    for (const [rule, count] of Object.entries(baseline)) {
      assert.equal(typeof rule, 'string');
      assert.equal(typeof count, 'number');
    }
  });
});

describe('check-lint-baseline: reportCheck / parseArgs / total', () => {
  it('FAIL exits 1 and lists each grown rule with its delta', () => {
    const { code, lines } = reportCheck({ 'no-undef': 7, 'eqeqeq': 1 }, { 'no-undef': 5 }, 'npm run lint:baseline -- --update');
    assert.equal(code, 1);
    assert.ok(lines[0].startsWith('lint-baseline: FAIL'));
    assert.ok(lines.includes('  eqeqeq: 1 > baseline 0 (+1)'));
    assert.ok(lines.includes('  no-undef: 7 > baseline 5 (+2)'));
    assert.ok(lines.at(-1).includes('npm run lint:baseline -- --update'));
  });

  it('OK with decreases exits 0 and nudges toward --update', () => {
    const { code, lines } = reportCheck({ 'no-undef': 3 }, { 'no-undef': 5 });
    assert.equal(code, 0);
    assert.ok(lines[0].includes('some rules shrank'));
    assert.ok(lines.includes('  no-undef: 3 < baseline 5 (-2)'));
  });

  it('OK within baseline exits 0 with a totals line', () => {
    const { code, lines } = reportCheck({ 'no-undef': 5 }, { 'no-undef': 5 });
    assert.equal(code, 0);
    assert.deepEqual(lines, ['lint-baseline: OK — 5 errors across 1 rules, within baseline.']);
  });

  it('parseArgs handles --update and --baseline <path>', () => {
    assert.deepEqual(parseArgs([]), { update: false, baseline: BASELINE_PATH });
    assert.equal(parseArgs(['--update']).update, true);
    assert.equal(parseArgs(['--baseline', 'x/y.json']).baseline, path.resolve('x/y.json'));
  });

  it('total sums counts and tolerates undefined', () => {
    assert.equal(total({ a: 2, b: 3 }), 5);
    assert.equal(total(undefined), 0);
  });
});

describe('check-lint-baseline: lintRepo', () => {
  it('drives an injected ESLint class with cache:false and lints "."', async () => {
    const seen = {};
    class FakeESLint {
      constructor(opts) {
        seen.opts = opts;
      }
      async lintFiles(patterns) {
        seen.patterns = patterns;
        return [{ messages: [{ severity: 2, ruleId: 'no-undef' }] }];
      }
    }
    const results = await lintRepo('/x/repo', { overrideConfigFile: true }, FakeESLint);
    assert.deepEqual(aggregateCounts(results), { 'no-undef': 1 });
    assert.equal(seen.opts.cache, false);
    assert.equal(seen.opts.cwd, '/x/repo');
    assert.equal(seen.opts.overrideConfigFile, true);
    assert.deepEqual(seen.patterns, ['.']);
  });

  it('rejects (not crashes) when ESLint cannot be resolved from cwd', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lint-baseline-noeslint-'));
    try {
      await assert.rejects(() => lintRepo(dir));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

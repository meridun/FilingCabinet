import test from 'node:test';
import assert from 'node:assert/strict';
import { offenders } from '../scripts/check-docs.mjs';

test('flags document formats, databases, data dirs, and config.toml', () => {
  const bad = offenders(['a/scan.PDF', 'x.jpg', 'index.db', 'data/foo.txt', 'inbox/x.md', 'config.toml']);
  assert.equal(bad.length, 6);
});

test('allows framework files', () => {
  assert.deepEqual(
    offenders(['docs/diagram.png', 'config.example.toml', 'filingcabinet/cli.py', 'migrations/001_init.sql']),
    [],
  );
});

#!/usr/bin/env node
// Guard: no user documents or databases may be tracked in this repo (framework only).
// .gitignore is the first line of defence; this is the CI backstop for anything
// force-added or matching a pattern .gitignore missed.
//
//   node scripts/check-docs.mjs      # exit 1 on any tracked offender
//   import { offenders } from './check-docs.mjs'

import { execFileSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

export const BLOCKED_EXT = new Set([
  '.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp', '.bmp', '.gif',
  '.db', '.sqlite', '.sqlite3',
]);
export const BLOCKED_DIRS = ['data/', 'inbox/', 'documents/', 'snapshots/', 'exports/', 'plans/'];
export const ALLOW = [/^docs\/.*\.png$/];

export function offenders(paths) {
  return paths.filter((p) => {
    if (ALLOW.some((re) => re.test(p))) return false;
    if (p === 'config.toml') return true;
    if (BLOCKED_DIRS.some((d) => p.startsWith(d))) return true;
    const ext = path.posix.extname(p).toLowerCase();
    return BLOCKED_EXT.has(ext);
  });
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const tracked = execFileSync('git', ['ls-files'], { encoding: 'utf8' }).split('\n').filter(Boolean);
  const bad = offenders(tracked);
  if (bad.length) {
    console.error('check-docs: tracked files that look like user documents or databases:');
    for (const b of bad) console.error(`  ${b}`);
    process.exit(1);
  }
  console.log(`check-docs: ok (${tracked.length} tracked files)`);
}

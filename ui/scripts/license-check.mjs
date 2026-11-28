// Every package the UI ships with (dependencies and their type peers, not devDependencies) must carry a
// license that allows commercial use. Reads package-lock.json, so it needs no install; when node_modules
// is there, each installed package.json must agree with the lockfile. An optional folder (relative to the
// repository root) checks another package, such as the deck builder in scripts/.
//   node ui/scripts/license-check.mjs [scripts]
import {existsSync, readFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const ui = join(dirname(fileURLToPath(import.meta.url)), '..');
const root = process.argv[2] ? resolve(ui, '..', process.argv[2]) : ui;
const ALLOWED = new Set(['MIT', 'ISC', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0', '0BSD', 'CC0-1.0', 'BlueOak-1.0.0', 'Unlicense', 'Zlib']);

/** SPDX expression: OR needs one allowed side, AND needs both. */
function allowed(expr) {
	const e = String(expr ?? '').replace(/^\((.*)\)$/, '$1').trim();
	if (/ OR /i.test(e)) return e.split(/ OR /i).some(allowed);
	if (/ AND /i.test(e)) return e.split(/ AND /i).every(allowed);
	return ALLOWED.has(e);
}

const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
const shipped = Object.entries(lock.packages).filter(([path, p]) => path && !p.dev);
if (!shipped.length) {
	console.error(`license-check: ${join(root, 'package-lock.json')} lists no shipped packages`);
	process.exit(1);
}
const bad = [];
for (const [path, p] of shipped) {
	const name = path.replace(/^.*node_modules\//, '');
	if (!allowed(p.license)) bad.push(`${name}@${p.version}: ${p.license ?? 'no license'}`);
	const installed = join(root, path, 'package.json');
	if (existsSync(installed)) {
		const pkg = JSON.parse(readFileSync(installed, 'utf8'));
		const license = typeof pkg.license === 'object' ? pkg.license?.type : pkg.license;
		if (license !== p.license) bad.push(`${name}: installed license ${license} differs from the lockfile (${p.license})`);
	}
}
if (bad.length) {
	console.error(`license-check: ${bad.length} problem(s):\n  ${bad.join('\n  ')}`);
	process.exit(1);
}
const kinds = [...new Set(shipped.map(([, p]) => p.license))].join(', ');
console.log(`ok licenses: ${shipped.length} shipped packages, all ${kinds}`);

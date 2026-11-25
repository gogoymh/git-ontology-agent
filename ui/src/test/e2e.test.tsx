// The UI driving the real Python engine (`python -m goa serve`, offline): type, watch it merge, read the log.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {render} from 'ink-testing-library';
import {App} from '../app.js';
import {listConnections, saveDefault, spawnEngine} from '../engine.js';
import {KEY, frameWith, type} from './helpers.js';

process.env['GOA_UI_LOG'] = '/dev/null';
for (const k of Object.keys(process.env)) if (k.startsWith('GOA_') && k !== 'GOA_UI_LOG' && k !== 'GOA_PYTHON') delete process.env[k];
process.env['GOA_ENV_FILE'] = '/dev/null'; // a developer's .env must not pick a provider here

test('a real engine answers through the UI: started, final plan, independent request, git log', {timeout: 60_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-e2e-'));
	process.env['GOA_CONFIG'] = join(dir, 'config');
	const ui = render(
		<App
			boot={{llm: 'offline', decision: 'local', lang: 'en'}}
			deps={{listConnections, saveDefault, start: (l, d) => spawnEngine(['--llm', l, '--decision', d, '--lang', 'en', '--repo', join(dir, 'conv')], dir)}}
		/>,
	);
	try {
		let f = await frameWith(ui.lastFrame, 'Git Ontology Agent', 20_000);
		assert.match(f, /offline · judge local\/heuristic/);
		await type(ui.stdin, 'Plan my Busan business trip next week', KEY.enter);
		f = await frameWith(ui.lastFrame, 'Here is your plan', 20_000);
		assert.match(f, /R1 › Plan my Busan business trip next week/);
		assert.match(f, /◐ Got it, I've started on “Busan trip plan”/);
		assert.match(f, /● Here is your plan: Tue 08:00 KTX \(Seoul→Busan\)/);
		assert.match(f, /final · main@[0-9a-f]{7}/);
		await type(ui.stdin, "What time is tomorrow's meeting?", KEY.enter);
		f = await frameWith(ui.lastFrame, /Tomorrow's meeting at 2:00 PM/, 20_000);
		assert.match(f, /R2 › What time is tomorrow's meeting\?/);
		await type(ui.stdin, '/log', KEY.enter);
		f = await frameWith(ui.lastFrame, /Merge req\/2/, 20_000);
		assert.match(f, /Merge req\/1: Busan trip plan/);
	} finally {
		ui.unmount();
		await new Promise(r => setTimeout(r, 300)); // the engine reads "quit" and exits
		rmSync(dir, {recursive: true, force: true});
	}
});

test('the real `goa auth --json` lists sign-ins, API keys and offline without secrets', {timeout: 60_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-auth-'));
	process.env['GOA_CONFIG'] = join(dir, 'config');
	try {
		const info = await listConnections();
		const ids = info.connections.map(c => c.id);
		assert.deepEqual(ids.slice(0, 2), ['claude-code', 'codex']);
		assert.ok(ids.includes('openrouter') && ids.at(-1) === 'offline');
		assert.ok(!/@|sk-|key-[a-z0-9]{8}/i.test(JSON.stringify(info)), 'no e-mail or key in the listing');
		await saveDefault('offline');
		const again = await listConnections();
		assert.deepEqual([again.default.llm, again.default.source], ['offline', 'saved']);
	} finally {
		rmSync(dir, {recursive: true, force: true});
	}
});

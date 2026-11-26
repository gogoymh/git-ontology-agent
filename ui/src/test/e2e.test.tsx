// The UI driving the real Python engine (`python -m goa serve`, offline): type, watch it merge, read the log.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync, spawn} from 'node:child_process';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createInterface} from 'node:readline';
import {render} from 'ink-testing-library';
import {App} from '../app.js';
import type {Engine, EngineEvent} from '../engine.js';
import {listConnections, python, saveDefault, spawnEngine} from '../engine.js';
import {KEY, auth, frameWith, type} from './helpers.js';

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

/** Spawn a real `goa.serve.Server` whose TripSkill pauses briefly per tick so /connect can fire mid-work. */
function spawnSlowEngine(repo: string, cwd: string): Engine {
	const py = python();
	const code = `
import sys, time
from goa.agent import Agent
from goa.serve import serve
from goa.skills import CalendarSkill, TripSkill

class SlowTrip(TripSkill):
    def next_step(self, state, req, at):
        time.sleep(0.4)
        return super().next_step(state, req, at)

raise SystemExit(serve(lambda on_answer, on_step: Agent(
    sys.argv[1], llm="offline", decision="local", lang="en",
    skills=[SlowTrip("en"), CalendarSkill("en")],
    on_answer=on_answer, on_step=on_step,
)))
`;
	const child = spawn(py.cmd, ['-c', code, repo], {cwd, env: py.env, stdio: ['pipe', 'pipe', 'pipe']});
	const listeners = new Set<(e: EngineEvent) => void>();
	const emit = (e: EngineEvent) => {
		for (const fn of listeners) fn(e);
	};
	let exited = false;
	let resolveDone!: () => void;
	const done = new Promise<void>(r => {
		resolveDone = r;
	});
	createInterface({input: child.stdout}).on('line', line => {
		if (!line.trim()) return;
		try {
			emit(JSON.parse(line) as EngineEvent);
		} catch {}
	});
	child.on('exit', code => {
		if (exited) return;
		exited = true;
		emit({type: 'exit', code, stderr: ''});
		resolveDone();
	});
	return {
		on(fn) {
			listeners.add(fn);
			return () => listeners.delete(fn);
		},
		send(cmd) {
			if (!exited && child.stdin.writable) child.stdin.write(JSON.stringify(cmd) + '\n');
		},
		stop() {
			if (exited) return Promise.resolve();
			this.send({type: 'quit'});
			child.stdin.end();
			return done;
		},
	};
}

test('resumes an open req branch after UI restart and shows the final answer', {timeout: 60_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-resume-'));
	const repo = join(dir, 'conv');
	process.env['GOA_CONFIG'] = join(dir, 'config');
	const py = python();
	execFileSync(
		py.cmd,
		[
			'-c',
			'import sys; from goa import Agent; a = Agent(sys.argv[1], lang="en"); a.submit("Plan my Busan business trip next week"); a.tick(); a.tick()',
			repo,
		],
		{cwd: dir, env: py.env},
	);
	const ui = render(
		<App
			boot={{llm: 'offline', decision: 'local', lang: 'en'}}
			deps={{listConnections, saveDefault, start: (l, d) => spawnEngine(['--llm', l, '--decision', d, '--lang', 'en', '--repo', repo], dir)}}
		/>,
	);
	try {
		const f = await frameWith(ui.lastFrame, 'Here is your plan', 20_000);
		assert.match(f, /● Here is your plan: Tue 08:00 KTX \(Seoul→Busan\)/);
		assert.match(f, /final · main@[0-9a-f]{7}/);
		await type(ui.stdin, '/log', KEY.enter);
		const logged = await frameWith(ui.lastFrame, /Merge req\/1: Busan trip plan/, 20_000);
		assert.match(logged, /Merge req\/1: Busan trip plan/);
	} finally {
		ui.unmount();
		await new Promise(r => setTimeout(r, 300));
		rmSync(dir, {recursive: true, force: true});
	}
});

test('resumes an open req branch across /connect model switch without racing', {timeout: 60_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-switch-'));
	const repo = join(dir, 'conv');
	process.env['GOA_CONFIG'] = join(dir, 'config');
	let starts = 0;
	const ui = render(
		<App
			boot={{llm: 'offline', decision: 'local', lang: 'en'}}
			deps={{
				listConnections: async () => auth('saved'),
				saveDefault: async () => {},
				start: l => {
					starts += 1;
					if (starts === 1) return spawnSlowEngine(repo, dir);
					return spawnEngine(['--mock', '--llm', l, '--decision', `llm:${l}`, '--lang', 'en', '--repo', repo], dir);
				},
			}}
		/>,
	);
	try {
		await frameWith(ui.lastFrame, 'Git Ontology Agent', 20_000);
		await type(ui.stdin, 'Plan my Busan business trip next week', KEY.enter);
		await frameWith(ui.lastFrame, 'req/1 Busan trip plan', 20_000);
		await type(ui.stdin, '/connect', KEY.enter);
		await frameWith(ui.lastFrame, 'Connect a model', 10_000);
		await type(ui.stdin, KEY.enter); // switch from offline to claude-code (first ready option)
		const f = await frameWith(ui.lastFrame, 'Here is your plan', 20_000);
		assert.equal(starts, 2);
		assert.match(f, /Connected\s+claude-code/);
		assert.match(f, /● Here is your plan: Tue 08:00 KTX \(Seoul→Busan\)/);
		assert.match(f, /final · main@[0-9a-f]{7}/);
		assert.doesNotMatch(f, /✕/);
		assert.equal(f.split('R1 › Plan my Busan business trip next week').length - 1, 1, `history duplicated after /connect:\n${f}`);
	} finally {
		ui.unmount();
		await new Promise(r => setTimeout(r, 300));
		rmSync(dir, {recursive: true, force: true});
	}
});

test('restores previous turns in order after UI restart and appends resumed and new turns', {timeout: 120_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-history-'));
	const repo = join(dir, 'conv');
	process.env['GOA_CONFIG'] = join(dir, 'config');
	const py = python();
	execFileSync(
		py.cmd,
		[
			'-c',
			[
				'import sys',
				'from goa import Agent',
				'a = Agent(sys.argv[1], lang="en")',
				'a.submit("What time is tomorrow\'s meeting?")',
				'a.run()',
				'a.submit("Plan my Busan business trip next week")',
				'a.tick()',
				'a.tick()',
				'a.submit("Oh, keep the budget under 500,000 won")',
			].join('; '),
			repo,
		],
		{cwd: dir, env: py.env},
	);
	const ui = render(
		<App
			boot={{llm: 'offline', decision: 'local', lang: 'en'}}
			deps={{listConnections, saveDefault, start: (l, d) => spawnEngine(['--llm', l, '--decision', d, '--lang', 'en', '--repo', repo], dir)}}
		/>,
	);
	try {
		let f = await frameWith(ui.lastFrame, 'Here is your plan', 45_000);
		const order = [
			f.indexOf("R1 › What time is tomorrow's meeting?"),
			f.indexOf("Here is what I found: Tomorrow's meeting at 2:00 PM."),
			f.indexOf('R2 › Plan my Busan business trip next week'),
			f.indexOf('R3 › Oh, keep the budget under 500,000 won'),
			f.indexOf('Here is your plan: Tue 08:00 KTX (Seoul→Busan)'),
		];
		assert.ok(order.every(i => i >= 0), `missing restored turns:\n${f}`);
		for (let i = 1; i < order.length; i++) assert.ok(order[i - 1]! < order[i]!, `restored turns out of order at ${i}:\n${f}`);
		assert.match(f, /within the ₩500,000 budget/);

		await type(ui.stdin, 'Plan my Daegu business trip next week', KEY.enter);
		f = await frameWith(ui.lastFrame, /R4 › Plan my Daegu business trip next week/, 45_000);
		assert.ok(f.indexOf('Here is your plan: Tue 08:00 KTX (Seoul→Busan)') < f.indexOf('R4 › Plan my Daegu business trip next week'));
	} finally {
		ui.unmount();
		await new Promise(r => setTimeout(r, 300));
		rmSync(dir, {recursive: true, force: true});
	}
});

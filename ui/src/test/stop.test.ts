// stop() must always finish: a stuck engine gets SIGTERM, then SIGKILL, and stop() gives up after a deadline (GOA-27).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chmodSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {STOP_MS, escalateStop, spawnEngine} from '../engine.js';

process.env['GOA_UI_LOG'] = '/dev/null';

const alive = (pid: number) => {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
};

test('stop() timeline defaults: SIGTERM 1.5s after quit, SIGKILL 1.5s later, give up 3s after SIGTERM', () => {
	assert.deepEqual(STOP_MS, {term: 1500, kill: 1500, giveUp: 3000});
});

test('an engine that ignores quit and SIGTERM is SIGKILLed and stop() resolves', {timeout: 10_000}, async () => {
	const dir = mkdtempSync(join(tmpdir(), 'goa-ui-stop-'));
	const fake = join(dir, 'fake-python');
	writeFileSync(
		fake,
		`#!/bin/sh\nexec python3 -c "import os,signal,time,json;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(json.dumps({'type':'log','text':str(os.getpid())}),flush=True);time.sleep(60)"\n`,
	);
	chmodSync(fake, 0o755);
	const old = process.env['GOA_PYTHON'];
	process.env['GOA_PYTHON'] = fake;
	try {
		const e = spawnEngine([], dir, {term: 100, kill: 200, giveUp: 2000});
		const pid = await new Promise<number>(r => e.on(ev => ev.type === 'log' && r(Number(ev.text))));
		const t0 = Date.now();
		await e.stop();
		const took = Date.now() - t0;
		assert.ok(took >= 300 && took < 1500, `stop() took ${took}ms; expected SIGKILL at ~300ms, before the give-up`);
		assert.equal(alive(pid), false, `fake engine ${pid} is still running`);
	} finally {
		if (old === undefined) delete process.env['GOA_PYTHON'];
		else process.env['GOA_PYTHON'] = old;
		rmSync(dir, {recursive: true, force: true});
	}
});

test('stop() is released even when the process survives SIGKILL', {timeout: 5000}, async () => {
	const sent: string[] = [];
	const t0 = Date.now();
	await new Promise<void>(release => escalateStop(sig => sent.push(sig), () => false, release, {term: 50, kill: 50, giveUp: 150}));
	assert.deepEqual(sent, ['SIGTERM', 'SIGKILL']);
	assert.ok(Date.now() - t0 >= 190, 'released before the give-up deadline');
});

test('an engine that exits on quit gets no signals', async () => {
	const sent: string[] = [];
	let gone = false;
	escalateStop(sig => sent.push(sig), () => gone, () => {}, {term: 30, kill: 30, giveUp: 60});
	gone = true;
	await new Promise(r => setTimeout(r, 150));
	assert.deepEqual(sent, []);
});

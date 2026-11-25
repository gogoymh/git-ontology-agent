// The connection picker: sign-ins and API keys side by side, arrows and enter, remembered after.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {render} from 'ink-testing-library';
import {App} from '../app.js';
import {FakeEngine, KEY, READY, auth, frameWith, type} from './helpers.js';

process.env['GOA_UI_LOG'] = '/dev/null';

function setup(source: 'auto' | 'saved' | 'env' = 'auto') {
	const engine = new FakeEngine();
	const calls = {started: [] as string[][], saved: [] as string[], listed: 0};
	const ui = render(
		<App
			boot={{}}
			deps={{
				listConnections: async () => (calls.listed++, auth(source)),
				saveDefault: async spec => void calls.saved.push(spec),
				start: (l, d) => (calls.started.push([l, d]), engine),
			}}
		/>,
	);
	return {ui, engine, calls};
}

test('lists sign-ins and API keys, picks with arrows and enter, and saves the choice', async () => {
	const {ui, calls} = setup();
	const f = await frameWith(ui.lastFrame, 'Connect a model');
	assert.match(f, /Sign in[\s\S]*❯ ● Claude\s+Claude Max subscription · Claude Code/);
	assert.match(f, /● Codex\s+ChatGPT sign-in · Codex CLI/);
	assert.match(f, /API key[\s\S]*● OpenRouter\s+OPENROUTER_API_KEY/);
	assert.match(f, /○ Anthropic\s+set ANTHROPIC_API_KEY/);
	assert.match(f, /goa login claude/);
	await type(ui.stdin, KEY.down);
	await frameWith(ui.lastFrame, /❯ ● Codex/);
	await type(ui.stdin, KEY.enter);
	await frameWith(ui.lastFrame, 'Starting the engine');
	assert.deepEqual(calls.started, [['codex', 'auto']]);
	assert.deepEqual(calls.saved, ['codex']);
	ui.unmount();
});

test('skips rows that are not ready', async () => {
	const {ui, calls} = setup();
	await frameWith(ui.lastFrame, 'Connect a model');
	await type(ui.stdin, KEY.down, KEY.down, KEY.down); // claude -> codex -> openrouter -> offline (anthropic is skipped)
	await frameWith(ui.lastFrame, /❯ ● Offline/);
	await type(ui.stdin, KEY.enter);
	await frameWith(ui.lastFrame, 'Starting the engine');
	assert.deepEqual(calls.started, [['offline', 'auto']]);
	ui.unmount();
});

test('asks nothing when a default is saved, and /connect asks again', async () => {
	const {ui, engine, calls} = setup('saved');
	await frameWith(ui.lastFrame, 'Starting the engine');
	assert.deepEqual(calls.started, [['auto', 'auto']]);
	engine.emit(READY);
	await frameWith(ui.lastFrame, 'Git Ontology Agent');
	await type(ui.stdin, '/connect', KEY.enter);
	await frameWith(ui.lastFrame, 'Connect a model');
	await type(ui.stdin, '\u001B'); // esc: back to the conversation, same engine
	await frameWith(ui.lastFrame, 'Talk any time');
	assert.equal(engine.stopped, false);
	assert.equal(calls.listed, 2);
	ui.unmount();
});

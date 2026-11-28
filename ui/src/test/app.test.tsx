// The conversation screen against a scripted engine: header, turns, live branches, the prompt.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {render} from 'ink-testing-library';
import {App} from '../app.js';
import {FakeEngine, KEY, READY, auth, frameWith, type} from './helpers.js';

process.env['GOA_UI_LOG'] = '/dev/null';

test('draws the header, user turns, interim and final answers, and the open branch', async () => {
	const engine = new FakeEngine();
	const started: string[][] = [];
	const ui = render(
		<App
			boot={{llm: 'claude-code'}}
			deps={{listConnections: async () => auth(), saveDefault: async () => {}, start: (l, d) => (started.push([l, d]), engine)}}
		/>,
	);
	await frameWith(ui.lastFrame, 'Starting the engine');
	assert.deepEqual(started, [['claude-code', 'auto']]);
	engine.emit(READY);
	let f = await frameWith(ui.lastFrame, 'Git Ontology Agent');
	assert.match(f, /claude-code · sonnet · judge llm\/claude-code/);
	assert.match(f, /Talk any time, even while it works/);

	await type(ui.stdin, 'Plan my Busan trip', KEY.enter);
	assert.deepEqual(engine.sent, [{type: 'submit', text: 'Plan my Busan trip'}]);
	await frameWith(ui.lastFrame, 'Reading your message');
	engine.emit({type: 'user', n: 1, text: 'Plan my Busan trip'});
	engine.emit({type: 'answer', kind: 'interim', text: "Got it, I've started.", request: 'req.1', relation: '', relation_name: '', source: 'req/1@3d2aaef', commit: 'a'});
	engine.emit({type: 'submitted'});
	engine.emit({type: 'tasks', open: [{n: 1, title: 'Busan trip plan', branch: 'req/1'}]});
	engine.emit({type: 'step', branch: 'req/1', subject: 'hotel.candidates: find (3 hotels)'});
	f = await frameWith(ui.lastFrame, 'hotel.candidates: find');
	assert.match(f, /R1 › Plan my Busan trip/);
	assert.match(f, /◐ Got it, I've started\./);
	assert.match(f, /interim · req\/1@3d2aaef/);
	assert.match(f, /req\/1 Busan trip plan/);
	assert.match(f, /1 open request/);
	assert.doesNotMatch(f, /Reading your message/);

	engine.emit({type: 'answer', kind: 'final', text: 'Here is your plan: Hotel A.', request: 'req.1', relation: '', relation_name: '', source: 'main@7cf5561', commit: 'b'});
	engine.emit({type: 'tasks', open: []});
	f = await frameWith(ui.lastFrame, 'Here is your plan');
	assert.match(f, /● Here is your plan: Hotel A\./);
	assert.match(f, /final · main@7cf5561/);
	assert.match(f, /idle/);
	ui.unmount();
	assert.equal(engine.stopped, true);
});

test('edits with the cursor, recalls history, and runs slash commands', async () => {
	const engine = new FakeEngine();
	const ui = render(<App boot={{llm: 'offline'}} deps={{listConnections: async () => auth(), saveDefault: async () => {}, start: () => engine}} />);
	engine.emit({...READY, llm: 'offline'} as typeof READY);
	await frameWith(ui.lastFrame, 'Git Ontology Agent');
	await type(ui.stdin, 'helo', '\u001B[D', 'l', KEY.enter); // left arrow, insert: "hello"
	await type(ui.stdin, KEY.up, KEY.enter); // history: the same message again
	assert.deepEqual(engine.sent, [{type: 'submit', text: 'hello'}, {type: 'submit', text: 'hello'}]);
	await type(ui.stdin, 'fast\rtwo lines\rtail'); // Enter inside a typed chunk still sends
	assert.deepEqual(engine.sent.slice(2), [{type: 'submit', text: 'fast'}, {type: 'submit', text: 'two lines'}]);
	await frameWith(ui.lastFrame, '› tail');
	await type(ui.stdin, '\u0015', '/log', KEY.enter); // ctrl+u clears the line first
	assert.deepEqual(engine.sent.at(-1), {type: 'log'});
	engine.emit({type: 'log', text: '* 5e8059d (main) Merge req/1'});
	await frameWith(ui.lastFrame, 'Merge req/1');
	await type(ui.stdin, '/help', KEY.enter);
	await frameWith(ui.lastFrame, '/connect');
	await type(ui.stdin, '/nope', KEY.enter);
	await frameWith(ui.lastFrame, 'Unknown command /nope');
	engine.emit({type: 'error', message: 'Claude Code: Not logged in'});
	await frameWith(ui.lastFrame, '✕ Claude Code: Not logged in');
	ui.unmount();
});

test('speaks Korean when the engine does', async () => {
	const engine = new FakeEngine();
	const ui = render(<App boot={{llm: 'offline', lang: 'ko'}} deps={{listConnections: async () => auth(), saveDefault: async () => {}, start: () => engine}} />);
	await frameWith(ui.lastFrame, '엔진을 시작하는 중');
	engine.emit({...READY, lang: 'ko'} as typeof READY);
	const f = await frameWith(ui.lastFrame, '요청마다 브랜치, 답마다 머지');
	assert.match(f, /작업 중에도 언제든 말을 걸 수 있습니다/);
	ui.unmount();
});

test('restores previous conversation history in order and skips duplicates on /connect', async () => {
	const engines = [new FakeEngine(), new FakeEngine()];
	let starts = 0;
	const ui = render(
		<App
			boot={{llm: 'offline'}}
			deps={{
				listConnections: async () => auth('saved'),
				saveDefault: async () => {},
				start: () => engines[starts++]!,
			}}
		/>,
	);
	const history = {
		type: 'history' as const,
		items: [
			{type: 'user' as const, n: 1, text: 'Plan my Busan trip'},
			{type: 'answer' as const, kind: 'interim' as const, text: "Got it, I've started.", request: 'req.1', relation: '', relation_name: '', source: 'req/1@1111111', commit: 'a1'},
			{type: 'answer' as const, kind: 'final' as const, text: 'Here is your plan: Hotel A.', request: 'req.1', relation: '', relation_name: '', source: 'main@2222222', commit: 'a2'},
			{type: 'user' as const, n: 2, text: "What time is tomorrow's meeting?"},
			{type: 'answer' as const, kind: 'interim' as const, text: "Checking tomorrow's meeting.", request: 'req.2', relation: '', relation_name: '', source: 'req/2@3333333', commit: 'a3'},
		],
	};
	engines[0]!.emit({...READY, llm: 'offline'} as typeof READY);
	engines[0]!.emit(history);
	let f = await frameWith(ui.lastFrame, "Checking tomorrow's meeting.");
	const order = [
		f.indexOf('R1 › Plan my Busan trip'),
		f.indexOf("Got it, I've started."),
		f.indexOf('Here is your plan: Hotel A.'),
		f.indexOf("R2 › What time is tomorrow's meeting?"),
		f.indexOf("Checking tomorrow's meeting."),
	];
	assert.ok(order.every(i => i >= 0), `missing history lines:\n${f}`);
	for (let i = 1; i < order.length; i++) assert.ok(order[i - 1]! < order[i]!, `out of order at ${i}:\n${f}`);

	engines[0]!.emit({type: 'answer', kind: 'final', text: 'Here is what I found: 2:00 PM.', request: 'req.2', relation: '', relation_name: '', source: 'main@4444444', commit: 'a4'});
	f = await frameWith(ui.lastFrame, 'Here is what I found: 2:00 PM.');
	assert.ok(f.indexOf("Checking tomorrow's meeting.") < f.indexOf('Here is what I found: 2:00 PM.'));

	await type(ui.stdin, '/connect', KEY.enter);
	await frameWith(ui.lastFrame, 'Connect a model');
	await type(ui.stdin, KEY.enter);
	engines[1]!.emit(READY);
	engines[1]!.emit(history);
	f = await frameWith(ui.lastFrame, 'Connected');
	assert.equal(f.split('R1 › Plan my Busan trip').length - 1, 1, `history duplicated on /connect:\n${f}`);
	ui.unmount();
});

const turn = (n: number, text: string, commit: string) => [
	{type: 'user' as const, n, text},
	{type: 'answer' as const, kind: 'interim' as const, text: `On it: ${text}`, request: `req.${n}`, relation: '', relation_name: '', source: `req/${n}@1111111`, commit},
];

test('history fills prompt recall after a restart and keeps it across /connect', async () => {
	const engines = [new FakeEngine(), new FakeEngine()];
	let starts = 0;
	const ui = render(
		<App boot={{llm: 'offline'}} deps={{listConnections: async () => auth('saved'), saveDefault: async () => {}, start: () => engines[starts++]!}} />,
	);
	engines[0]!.emit({...READY, llm: 'offline'} as typeof READY);
	engines[0]!.emit({type: 'history', items: [...turn(1, 'Plan my Busan trip', 'c1'), ...turn(2, 'Budget under 500k', 'c2')], more: false});
	await frameWith(ui.lastFrame, 'On it: Budget under 500k');
	await type(ui.stdin, KEY.up);
	await frameWith(ui.lastFrame, '› Budget under 500k');
	await type(ui.stdin, KEY.up);
	await frameWith(ui.lastFrame, '› Plan my Busan trip');
	await type(ui.stdin, KEY.enter);
	assert.deepEqual(engines[0]!.sent, [{type: 'submit', text: 'Plan my Busan trip'}]);

	await type(ui.stdin, '/connect', KEY.enter);
	await frameWith(ui.lastFrame, 'Connect a model');
	await type(ui.stdin, KEY.enter);
	engines[1]!.emit(READY);
	await frameWith(ui.lastFrame, 'Connected');
	await type(ui.stdin, KEY.up, KEY.up, KEY.up); // /connect, Plan my Busan trip, Budget under 500k
	await frameWith(ui.lastFrame, '› Budget under 500k');
	ui.unmount();
});

test('history command loads older turns once and says when none are left', async () => {
	const engine = new FakeEngine();
	const ui = render(<App boot={{llm: 'offline'}} deps={{listConnections: async () => auth(), saveDefault: async () => {}, start: () => engine}} />);
	engine.emit({...READY, llm: 'offline'} as typeof READY);
	engine.emit({type: 'history', items: turn(3, 'Newest request', 'c3'), more: true});
	await frameWith(ui.lastFrame, 'On it: Newest request');

	await type(ui.stdin, '/history 2', KEY.enter);
	assert.deepEqual(engine.sent.at(-1), {type: 'history', limit: 2});
	const page = {type: 'history' as const, items: [...turn(1, 'Oldest request', 'c1'), ...turn(2, 'Middle request', 'c2')], more: false, older: true};
	engine.emit(page);
	let f = await frameWith(ui.lastFrame, 'start of the conversation');
	assert.ok(f.indexOf('earlier turns') < f.indexOf('R1 › Oldest request'), f);
	assert.ok(f.indexOf('R1 › Oldest request') < f.indexOf('R2 › Middle request'), f);
	await type(ui.stdin, KEY.up, KEY.up, KEY.up); // /history 2, Newest request, Middle request
	await frameWith(ui.lastFrame, '› Middle request');
	await type(ui.stdin, '\u0015');

	engine.emit(page); // the same page again (a repeated command) draws nothing twice
	await type(ui.stdin, '/history', KEY.enter);
	assert.deepEqual(engine.sent.at(-1), {type: 'history', limit: 50});
	engine.emit({type: 'history', items: [], more: false, older: true});
	f = await frameWith(ui.lastFrame, 'No older turns.');
	assert.equal(f.split('R1 › Oldest request').length - 1, 1, `older page drawn twice:\n${f}`);

	await type(ui.stdin, '/history x', KEY.enter);
	await frameWith(ui.lastFrame, 'Usage: /history [N]');
	assert.equal(engine.sent.filter(c => c.type === 'history').length, 2);
	ui.unmount();
});

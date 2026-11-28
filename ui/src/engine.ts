// The Python engine as a child process: `python -m goa serve`, JSON lines both ways.
import {spawn, execFile} from 'node:child_process';
import {existsSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {log} from './log.js';

export type Ready = {type: 'ready'; version: string; llm: string; model: string; decision: string; lang: string; repo: string};
export type AnswerEvent = {
	type: 'answer';
	kind: 'interim' | 'final';
	text: string;
	request: string;
	relation: string;
	relation_name: string;
	source: string;
	commit: string;
};
export type UserEvent = {type: 'user'; n: number | null; text: string};
export type HistoryItem = UserEvent | AnswerEvent;
/** `older` marks a page asked for with a history command; `more` says whether older turns remain. */
export type HistoryEvent = {type: 'history'; items: HistoryItem[]; more?: boolean; older?: boolean};
export type OpenTask = {n: number; title: string; branch: string};
export type EngineEvent =
	| Ready
	| HistoryEvent
	| AnswerEvent
	| UserEvent
	| {type: 'step'; branch: string; subject: string}
	| {type: 'tasks'; open: OpenTask[]}
	| {type: 'submitted'}
	| {type: 'log'; text: string}
	| {type: 'error'; message: string}
	| {type: 'exit'; code: number | null; stderr: string};

export type Command = {type: 'submit'; text: string} | {type: 'log'} | {type: 'history'; limit: number} | {type: 'quit'};

export interface Engine {
	on(fn: (e: EngineEvent) => void): () => void;
	send(cmd: Command): void;
	stop(): void | Promise<void>;
}

export type Connection = {id: string; kind: 'sign-in' | 'api-key' | 'offline'; label: string; ready: boolean; detail: string};
export type AuthInfo = {
	connections: Connection[];
	default: {llm: string; decision: string; source: 'env' | 'saved' | 'auto'};
	config: string;
};

const here = dirname(fileURLToPath(import.meta.url));

/** How to run the Python engine: GOA_PYTHON (set by `goa ui`), else python3; a source checkout goes on PYTHONPATH. */
export function python(): {cmd: string; env: NodeJS.ProcessEnv} {
	const env = {...process.env};
	const root = join(here, '..', '..');
	if (existsSync(join(root, 'goa', '__init__.py'))) {
		env['PYTHONPATH'] = [root, env['PYTHONPATH']].filter(Boolean).join(process.platform === 'win32' ? ';' : ':');
	}
	const cmd = env['GOA_PYTHON'] || (process.platform === 'win32' ? 'python' : 'python3');
	return {cmd, env};
}

/** stop() timeline: quit, then SIGTERM after `term`, SIGKILL `kill` later, and give up `giveUp` after SIGTERM. */
export const STOP_MS = {term: 1500, kill: 1500, giveUp: 3000};

/**
 * Escalates until the engine is gone, so a stuck engine can never hold /connect in 'starting'.
 * `release` resolves stop()'s promise even if the process ignores every signal. Returns a cancel.
 */
export function escalateStop(kill: (sig: NodeJS.Signals) => void, exited: () => boolean, release: () => void, ms = STOP_MS): () => void {
	const timers: ReturnType<typeof setTimeout>[] = [];
	const at = (delay: number, fn: () => void) => {
		const t = setTimeout(() => {
			if (!exited()) fn();
		}, delay);
		t.unref();
		timers.push(t);
	};
	at(ms.term, () => {
		log(`engine still running ${ms.term}ms after quit; sending SIGTERM`);
		kill('SIGTERM');
		at(ms.kill, () => {
			log(`engine ignored SIGTERM for ${ms.kill}ms; sending SIGKILL`);
			kill('SIGKILL');
		});
		at(ms.giveUp, () => {
			log(`engine did not exit ${ms.giveUp}ms after SIGTERM; releasing stop() anyway`);
			release();
		});
	});
	return () => {
		for (const t of timers) clearTimeout(t);
	};
}

export function spawnEngine(args: string[], cwd = process.cwd(), stopMs = STOP_MS): Engine {
	const py = python();
	const argv = ['-m', 'goa', 'serve', ...args];
	log(`engine start: ${py.cmd} ${argv.join(' ')}`);
	const child = spawn(py.cmd, argv, {cwd, env: py.env, stdio: ['pipe', 'pipe', 'pipe']});
	const listeners = new Set<(e: EngineEvent) => void>();
	const emit = (e: EngineEvent) => {
		for (const fn of listeners) fn(e);
	};
	let stderr = '';
	child.stderr.setEncoding('utf8');
	child.stderr.on('data', (chunk: string) => {
		stderr = (stderr + chunk).slice(-4000);
		for (const line of chunk.split('\n')) if (line.trim()) log(`engine stderr: ${line}`);
	});
	createInterface({input: child.stdout}).on('line', line => {
		if (!line.trim()) return;
		try {
			const e = JSON.parse(line) as EngineEvent;
			log(`event ${e.type}${e.type === 'answer' ? ` ${e.kind} from ${e.source}` : e.type === 'history' ? ` ${e.items.length} items` : ''}`);
			emit(e);
		} catch {
			log(`engine wrote a non-JSON line: ${line.slice(0, 200)}`);
		}
	});
	let exited = false;
	let resolveDone!: () => void;
	const done = new Promise<void>(r => {
		resolveDone = r;
	});
	let cancelStop: (() => void) | null = null;
	const finish = () => {
		cancelStop?.();
		cancelStop = null;
		resolveDone();
	};
	child.on('error', err => {
		if (exited) return;
		exited = true;
		log(`engine failed to start: ${err.message}`);
		emit({type: 'exit', code: null, stderr: `${py.cmd}: ${err.message}`});
		finish();
	});
	child.on('exit', code => {
		if (exited) return;
		exited = true;
		log(`engine exit ${code}`);
		emit({type: 'exit', code, stderr});
		finish();
	});
	return {
		on(fn) {
			listeners.add(fn);
			return () => listeners.delete(fn);
		},
		send(cmd) {
			if (exited || !child.stdin.writable) return;
			log(`command ${cmd.type}`);
			child.stdin.write(JSON.stringify(cmd) + '\n');
		},
		stop() {
			if (exited) return Promise.resolve();
			this.send({type: 'quit'});
			child.stdin.end();
			cancelStop ??= escalateStop(sig => child.kill(sig), () => exited, resolveDone, stopMs);
			return done;
		},
	};
}

function goa(args: string[]): Promise<string> {
	const py = python();
	return new Promise((resolve, reject) => {
		execFile(py.cmd, ['-m', 'goa', ...args], {env: py.env, timeout: 60_000}, (err, stdout, stderr) => {
			if (err) reject(new Error((stderr || err.message).trim().split('\n').pop()));
			else resolve(stdout);
		});
	});
}

/** `goa auth --json`: every sign-in and API key, and what `auto` would pick. */
export async function listConnections(): Promise<AuthInfo> {
	const info = JSON.parse(await goa(['auth', '--json'])) as AuthInfo;
	log(`connections: ${info.connections.filter(c => c.ready).map(c => c.id).join(', ')}; default ${info.default.llm} (${info.default.source})`);
	return info;
}

/** `goa auth --use <spec>`: remember the choice so the picker does not ask again. */
export async function saveDefault(spec: string): Promise<void> {
	await goa(['auth', '--use', spec, '--json']);
	log(`saved default ${spec}`);
}

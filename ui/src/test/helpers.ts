import type {AuthInfo, Command, Engine, EngineEvent} from '../engine.js';

export const KEY = {enter: '\r', down: '\u001B[B', up: '\u001B[A'};

/** Poll the rendered frame until `want` shows up (Ink renders asynchronously). */
export async function frameWith(lastFrame: () => string | undefined, want: string | RegExp, ms = 5000): Promise<string> {
	const end = Date.now() + ms;
	for (;;) {
		const f = lastFrame() ?? '';
		if (typeof want === 'string' ? f.includes(want) : want.test(f)) return f;
		if (Date.now() > end) throw new Error(`timed out waiting for ${String(want)}; last frame:\n${f}`);
		await new Promise(r => setTimeout(r, 20));
	}
}

/** Type into the app like a person: a beat before each write, so a component that just appeared is listening. */
export async function type(stdin: {write: (s: string) => void}, ...chunks: string[]): Promise<void> {
	for (const c of chunks) {
		await new Promise(r => setTimeout(r, 30));
		stdin.write(c);
	}
	await new Promise(r => setTimeout(r, 30));
}

export class FakeEngine implements Engine {
	sent: Command[] = [];
	stopped = false;
	private fns = new Set<(e: EngineEvent) => void>();
	on(fn: (e: EngineEvent) => void) {
		this.fns.add(fn);
		return () => this.fns.delete(fn);
	}
	send(cmd: Command) {
		this.sent.push(cmd);
	}
	stop() {
		this.stopped = true;
	}
	emit(e: EngineEvent) {
		for (const fn of this.fns) fn(e);
	}
}

export const READY: EngineEvent = {
	type: 'ready',
	version: '0.1.0',
	llm: 'claude-code',
	model: 'sonnet',
	decision: 'llm/claude-code',
	lang: 'en',
	repo: '/tmp/x/.goa',
};

export function auth(source: AuthInfo['default']['source'] = 'auto'): AuthInfo {
	return {
		connections: [
			{id: 'claude-code', kind: 'sign-in', label: 'Claude', ready: true, detail: 'Claude Max subscription · Claude Code'},
			{id: 'codex', kind: 'sign-in', label: 'Codex', ready: true, detail: 'ChatGPT sign-in · Codex CLI'},
			{id: 'openrouter', kind: 'api-key', label: 'OpenRouter', ready: true, detail: 'OPENROUTER_API_KEY'},
			{id: 'anthropic', kind: 'api-key', label: 'Anthropic', ready: false, detail: 'set ANTHROPIC_API_KEY'},
			{id: 'offline', kind: 'offline', label: 'Offline', ready: true, detail: 'built-in demo skills, no network'},
		],
		default: {llm: 'claude-code', decision: 'llm:claude-code', source},
		config: '/tmp/goa/env',
	};
}

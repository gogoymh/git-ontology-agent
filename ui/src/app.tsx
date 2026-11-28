import {useEffect, useRef, useState} from 'react';
import {Box, Static, Text, useApp} from 'ink';
import {AnswerLine, Header, Note, Picker, Prompt, Spinner, UserLine, theme} from './components.js';
import type {AnswerEvent, AuthInfo, Connection, Engine, EngineEvent, OpenTask, Ready} from './engine.js';
import {detectLang, strings} from './i18n.js';
import {log} from './log.js';

export type Deps = {
	listConnections(): Promise<AuthInfo>;
	saveDefault(spec: string): Promise<void>;
	start(llm: string, decision: string): Engine;
};
export type Boot = {llm?: string; decision?: string; lang?: string};

type Item =
	| {id: number; kind: 'header'; ready: Ready; first: boolean}
	| {id: number; kind: 'user'; n: number | null; text: string}
	| {id: number; kind: 'answer'; a: AnswerEvent}
	| {id: number; kind: 'note'; text: string; color?: string};
type NewItem = Item extends infer I ? (I extends {id: number} ? Omit<I, 'id'> : never) : never;
type Phase = 'checking' | 'pick' | 'starting' | 'ready' | 'stopped';

export function App({boot, deps}: {boot: Boot; deps: Deps}) {
	const {exit} = useApp();
	const [phase, setPhase] = useState<Phase>('checking');
	const [items, setItems] = useState<Item[]>([]);
	const [auth, setAuth] = useState<AuthInfo | null>(null);
	const [ready, setReady] = useState<Ready | null>(null);
	const [tasks, setTasks] = useState<OpenTask[]>([]);
	const [steps, setSteps] = useState<Record<string, string>>({});
	const [reading, setReading] = useState(0);
	const engine = useRef<Engine | null>(null);
	const connSeq = useRef(0);
	const ids = useRef(0);
	const headers = useRef(0);
	const shown = useRef(new Set<string>()); // answers commits on screen: history never draws one twice
	const [recall, setRecall] = useState<string[]>([]); // prompt ↑↓, newest first
	const t = strings(ready?.lang ?? detectLang(boot.lang));

	const add = (...items: NewItem[]) =>
		setItems(xs => [...xs, ...items.map(i => ({...i, id: ids.current++}) as Item)]);

	const handle = (e: EngineEvent) => {
		switch (e.type) {
			case 'ready':
				setReady(e);
				add({kind: 'header', ready: e, first: headers.current++ === 0});
				setPhase('ready');
				break;
			case 'history': {
				// a user turn is drawn with the answer after it, so both are skipped when that answer is on screen
				const fresh: NewItem[] = [];
				let users: NewItem[] = [];
				for (const item of e.items) {
					if (item.type === 'user') users.push({kind: 'user', n: item.n, text: item.text});
					else {
						if (!shown.current.has(item.commit)) {
							shown.current.add(item.commit);
							fresh.push(...users, {kind: 'answer', a: item});
						}
						users = [];
					}
				}
				const asked = e.items.flatMap(i => (i.type === 'user' ? [i.text] : [])).reverse();
				setRecall(r => uniq(e.older ? [...r, ...asked] : [...r, ...asked.filter(x => !r.includes(x))]));
				log(`history ${e.older ? 'page' : 'start'}: ${e.items.length} items, ${fresh.length} new, more=${e.more ?? '?'}`);
				if (e.older) {
					if (fresh.length) add({kind: 'note', text: t.earlier}, ...fresh, {kind: 'note', text: t.earlierEnd(!!e.more)});
					else add({kind: 'note', text: t.noOlder});
				} else if (fresh.length) add(...fresh);
				break;
			}
			case 'user':
				add({kind: 'user', n: e.n, text: e.text});
				break;
			case 'answer':
				shown.current.add(e.commit);
				add({kind: 'answer', a: e});
				break;
			case 'step':
				setSteps(s => ({...s, [e.branch]: e.subject}));
				break;
			case 'tasks':
				setTasks(e.open);
				break;
			case 'submitted':
				setReading(r => Math.max(0, r - 1));
				break;
			case 'log':
				add({kind: 'note', text: e.text});
				break;
			case 'error':
				add({kind: 'note', text: `✕ ${e.message}`, color: theme.error});
				setReading(0);
				break;
			case 'exit': {
				const tail = e.stderr.trim().split('\n').slice(-3).join('\n');
				add({kind: 'note', text: [strings(detectLang(boot.lang)).exited(e.code), tail].filter(Boolean).join('\n'), color: theme.error});
				setPhase('stopped');
				setTasks([]);
				setReading(0);
				break;
			}
		}
	};

	const connect = (llm: string, decision: string) => {
		const seq = ++connSeq.current;
		const old = engine.current;
		engine.current = null;
		const stopping = old?.stop();
		log(`connect llm=${llm} decision=${decision}`);
		setPhase('starting');
		setTasks([]);
		setSteps({});
		setReading(0);
		const launch = () => {
			if (connSeq.current !== seq) return;
			const e = deps.start(llm, decision);
			engine.current = e;
			e.on(ev => {
				if (engine.current === e) handle(ev);
			});
		};
		if (stopping && typeof stopping.then === 'function') {
			void stopping.then(launch);
		} else {
			launch();
		}
	};

	const choose = async (always: boolean) => {
		const back = phase;
		setPhase('checking');
		let info: AuthInfo;
		try {
			info = await deps.listConnections();
		} catch (err) {
			log(`listing connections failed: ${(err as Error).message}`);
			add({kind: 'note', text: `✕ ${(err as Error).message}`, color: theme.error});
			if (!always) connect('auto', boot.decision ?? 'auto');
			else setPhase(back);
			return;
		}
		if (!always && info.default.source !== 'auto') {
			connect('auto', boot.decision ?? 'auto'); // GOA_LLM or a saved choice: no question to ask
			return;
		}
		setAuth(info);
		setPhase('pick');
	};

	useEffect(() => {
		if (boot.llm) connect(boot.llm, boot.decision ?? 'auto');
		else void choose(false);
		return () => {
			++connSeq.current;
			void engine.current?.stop();
		};
	}, []);

	const pick = (c: Connection) => {
		deps.saveDefault(c.id).catch(err => log(`saving default failed: ${(err as Error).message}`));
		connect(c.id, boot.decision ?? 'auto');
	};

	const submit = (raw: string) => {
		const text = raw.trim();
		setRecall(r => uniq([raw, ...r]));
		if (text.startsWith('/')) {
			const [head, arg] = text.split(/\s+/);
			const cmd = head!.toLowerCase();
			log(`slash ${cmd}`);
			if (cmd === '/help') add({kind: 'note', text: t.help.join('\n')});
			else if (cmd === '/log') engine.current?.send({type: 'log'});
			else if (cmd === '/history') {
				const n = arg === undefined ? 50 : Number(arg);
				if (Number.isInteger(n) && n > 0) engine.current?.send({type: 'history', limit: n});
				else add({kind: 'note', text: t.historyUsage, color: theme.error});
			}
			else if (cmd === '/connect') void choose(true);
			else if (cmd === '/quit' || cmd === '/exit') exit();
			else add({kind: 'note', text: t.unknown(cmd), color: theme.error});
			return;
		}
		setReading(r => r + 1);
		engine.current?.send({type: 'submit', text});
	};

	return (
		<>
			<Static items={items}>
				{item => {
					switch (item.kind) {
						case 'header':
							return <Header key={item.id} ready={item.ready} t={t} first={item.first} />;
						case 'user':
							return <UserLine key={item.id} n={item.n} text={item.text} />;
						case 'answer':
							return <AnswerLine key={item.id} a={item.a} t={t} />;
						default:
							return <Note key={item.id} text={item.text} color={item.color} />;
					}
				}}
			</Static>
			<Box flexDirection="column" marginTop={1}>
				{phase === 'checking' && <Busy text={t.checking} />}
				{phase === 'starting' && <Busy text={t.starting} />}
				{phase === 'pick' && auth && (
					<Picker
						connections={auth.connections}
						initial={auth.default.llm}
						t={t}
						onPick={pick}
						onCancel={ready && engine.current ? () => setPhase('ready') : undefined}
					/>
				)}
				{reading > 0 && <Busy text={t.reading} />}
				{tasks.map(task => (
					<Box key={task.branch} paddingLeft={1}>
						<Spinner color={theme.interim} />
						<Text color={theme.accent}> {task.branch} </Text>
						<Box flexShrink={1}>
							<Text wrap="truncate-end">
								{task.title}
								<Text color={theme.muted}>{steps[task.branch] ? ` · ${steps[task.branch]}` : ''}</Text>
							</Text>
						</Box>
					</Box>
				))}
				{phase !== 'pick' && <Prompt active={phase === 'ready'} placeholder={t.placeholder} onSubmit={submit} history={recall} />}
				<Box paddingX={1} justifyContent="space-between">
					<Text color={theme.muted}>{t.hints}</Text>
					<Text color={theme.muted}>
						{ready ? `${ready.llm} · ` : ''}
						{tasks.length ? t.open(tasks.length) : t.idle}
					</Text>
				</Box>
			</Box>
		</>
	);
}

const RECALL = 200;
const uniq = (xs: string[]) => [...new Set(xs)].slice(0, RECALL);

function Busy({text}: {text: string}) {
	return (
		<Box paddingLeft={1}>
			<Spinner />
			<Text color={theme.muted}> {text}</Text>
		</Box>
	);
}

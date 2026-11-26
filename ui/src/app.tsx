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
			case 'history':
				if (headers.current <= 1 && e.items.length > 0) {
					add(
						...e.items.map<NewItem>(item =>
							item.type === 'user' ? {kind: 'user', n: item.n, text: item.text} : {kind: 'answer', a: item},
						),
					);
				}
				break;
			case 'user':
				add({kind: 'user', n: e.n, text: e.text});
				break;
			case 'answer':
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
		if (text.startsWith('/')) {
			const cmd = text.split(/\s+/)[0]!.toLowerCase();
			log(`slash ${cmd}`);
			if (cmd === '/help') add({kind: 'note', text: t.help.join('\n')});
			else if (cmd === '/log') engine.current?.send({type: 'log'});
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
				{phase !== 'pick' && <Prompt active={phase === 'ready'} placeholder={t.placeholder} onSubmit={submit} />}
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

function Busy({text}: {text: string}) {
	return (
		<Box paddingLeft={1}>
			<Spinner />
			<Text color={theme.muted}> {text}</Text>
		</Box>
	);
}

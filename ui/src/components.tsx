import {useState} from 'react';
import {Box, Text, useAnimation, useInput, usePaste} from 'ink';
import type {AnswerEvent, Connection, Ready} from './engine.js';
import type {Strings} from './i18n.js';

// Colors chosen to read on both dark and light terminals.
export const theme = {
	accent: '#7c5cff', // violet: the brand, branches, the prompt
	interim: '#d97706', // amber: work in progress
	final: '#16a34a', // green: merged into main
	error: '#dc2626',
	muted: 'gray',
};

const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export function Spinner({color = theme.accent}: {color?: string}) {
	const {frame} = useAnimation({interval: 80});
	return <Text color={color}>{FRAMES[frame % FRAMES.length]}</Text>;
}

/** A fork and a merge: the whole idea in three lines. */
function Mark() {
	return (
		<Box flexDirection="column" marginRight={2}>
			<Text color={theme.accent}>●━╮</Text>
			<Text>
				<Text color={theme.accent}>┃ </Text>
				<Text color={theme.interim}>◆</Text>
			</Text>
			<Text color={theme.accent}>●━╯</Text>
		</Box>
	);
}

export function Header({ready, t, first}: {ready: Ready; t: Strings; first: boolean}) {
	const model = ready.model && ready.model !== ready.llm ? ` · ${ready.model}` : '';
	if (!first) {
		return (
			<Box marginTop={1}>
				<Text color={theme.accent}>◆ </Text>
				<Text bold>{t.connected}</Text>
				<Text color={theme.muted}>
					{'  '}
					{ready.llm}
					{model} · {t.judge} {ready.decision}
				</Text>
			</Box>
		);
	}
	return (
		<Box borderStyle="round" borderColor={theme.accent} paddingX={1} alignSelf="flex-start">
			<Mark />
			<Box flexDirection="column">
				<Text>
					<Text bold>Git Ontology Agent</Text>
					<Text color={theme.muted}> v{ready.version}</Text>
				</Text>
				<Text color={theme.muted}>{t.tagline}</Text>
				<Text>
					<Text color={theme.accent}>{ready.llm}</Text>
					<Text color={theme.muted}>
						{model} · {t.judge} {ready.decision} · {ready.repo.split('/').slice(-2).join('/')}
					</Text>
				</Text>
			</Box>
		</Box>
	);
}

export function UserLine({n, text}: {n: number | null; text: string}) {
	return (
		<Box marginTop={1}>
			<Text color={theme.accent} bold>
				{n ? `R${n}` : '↳'}
			</Text>
			<Text color={theme.accent}> › </Text>
			<Text bold>{text}</Text>
		</Box>
	);
}

export function AnswerLine({a, t}: {a: AnswerEvent; t: Strings}) {
	const final = a.kind === 'final';
	const color = final ? theme.final : theme.interim;
	const rel = a.relation ? ` · ${a.relation} ${a.relation_name}` : '';
	return (
		<Box flexDirection="column" paddingLeft={3}>
			<Box>
				<Box width={2} flexShrink={0}>
					<Text color={color}>{final ? '●' : '◐'}</Text>
				</Box>
				<Text>{a.text}</Text>
			</Box>
			<Box paddingLeft={2}>
				<Text color={theme.muted}>
					{final ? t.final : t.interim} · {a.source}
					{rel}
				</Text>
			</Box>
		</Box>
	);
}

export function Note({text, color}: {text: string; color?: string}) {
	return (
		<Box paddingLeft={3} marginTop={1}>
			<Text color={color ?? theme.muted}>{text}</Text>
		</Box>
	);
}

/** One line of input with a cursor, history and paste. Enter sends. */
export function Prompt({onSubmit, placeholder, active}: {onSubmit: (text: string) => void; placeholder: string; active: boolean}) {
	const [value, setValue] = useState('');
	const [cursor, setCursor] = useState(0);
	const [history, setHistory] = useState<string[]>([]);
	const [back, setBack] = useState(-1);
	const put = (v: string, c = v.length) => {
		setValue(v);
		setCursor(Math.max(0, Math.min(v.length, c)));
	};
	usePaste(
		text => {
			const clean = text.replace(/\r?\n/g, ' ');
			put(value.slice(0, cursor) + clean + value.slice(cursor), cursor + clean.length);
		},
		{isActive: active},
	);
	const send = (text: string) => {
		if (!text.trim()) return;
		setHistory(h => [text, ...h.filter(x => x !== text)].slice(0, 50));
		setBack(-1);
		onSubmit(text);
	};
	useInput(
		(input, key) => {
			if (key.return) {
				if (!value.trim()) return;
				put('');
				send(value);
			} else if (input && /[\r\n]/.test(input) && !key.ctrl && !key.meta) {
				// fast typing or a terminal without bracketed paste: text and Enter arrive in one chunk
				const lines = input.split(/\r\n|\r|\n/);
				const last = lines.pop()!;
				lines.forEach((line, i) => send(i === 0 ? value.slice(0, cursor) + line + value.slice(cursor) : line));
				put(last);
			} else if (key.backspace || key.delete) {
				if (cursor > 0) put(value.slice(0, cursor - 1) + value.slice(cursor), cursor - 1);
			} else if (key.leftArrow) {
				setCursor(c => Math.max(0, c - 1));
			} else if (key.rightArrow) {
				setCursor(c => Math.min(value.length, c + 1));
			} else if (key.home || (key.ctrl && input === 'a')) {
				setCursor(0);
			} else if (key.end || (key.ctrl && input === 'e')) {
				setCursor(value.length);
			} else if (key.ctrl && input === 'u') {
				put(value.slice(cursor), 0);
			} else if (key.ctrl && input === 'w') {
				const head = value.slice(0, cursor).replace(/\S+\s*$/, '');
				put(head + value.slice(cursor), head.length);
			} else if (key.upArrow || key.downArrow) {
				const i = Math.max(-1, Math.min(history.length - 1, back + (key.upArrow ? 1 : -1)));
				setBack(i);
				put(i < 0 ? '' : history[i]!);
			} else if (key.escape) {
				put('');
			} else if (input && !key.ctrl && !key.meta && !key.tab) {
				const clean = input.replace(/[\r\n]+/g, ' ');
				put(value.slice(0, cursor) + clean + value.slice(cursor), cursor + clean.length);
			}
		},
		{isActive: active},
	);
	const at = value[cursor] ?? ' ';
	return (
		<Box borderStyle="round" borderColor={active ? theme.accent : theme.muted} paddingX={1}>
			<Text color={theme.accent}>› </Text>
			{value ? (
				<Text>
					{value.slice(0, cursor)}
					<Text inverse>{at}</Text>
					{value.slice(cursor + 1)}
				</Text>
			) : (
				<Text>
					<Text inverse> </Text>
					<Text color={theme.muted}>{placeholder}</Text>
				</Text>
			)}
		</Box>
	);
}

/** Sign-ins first, then API keys, then offline. Only ready rows can be picked. */
export function Picker({connections, initial, t, onPick, onCancel}: {
	connections: Connection[];
	initial: string;
	t: Strings;
	onPick: (c: Connection) => void;
	onCancel?: () => void;
}) {
	const ready = connections.filter(c => c.ready);
	const [i, setI] = useState(Math.max(0, ready.findIndex(c => c.id === initial)));
	useInput((_input, key) => {
		if (key.upArrow) setI(x => (x - 1 + ready.length) % ready.length);
		else if (key.downArrow || key.tab) setI(x => (x + 1) % ready.length);
		else if (key.return && ready[i]) onPick(ready[i]!);
		else if (key.escape && onCancel) onCancel();
	});
	const groups: [Connection['kind'], string][] = [
		['sign-in', t.signIn],
		['api-key', t.apiKey],
		['offline', t.offline],
	];
	const chosen = ready[i]?.id;
	return (
		<Box flexDirection="column" borderStyle="round" borderColor={theme.accent} paddingX={1} alignSelf="flex-start">
			<Text bold>
				<Text color={theme.accent}>◆ </Text>
				{t.pickTitle}
			</Text>
			{groups.map(([kind, title]) => (
				<Box key={kind} flexDirection="column" marginTop={1}>
					<Text color={theme.muted}>{title}</Text>
					{connections
						.filter(c => c.kind === kind)
						.map(c => {
							const on = c.id === chosen;
							return (
								<Box key={c.id}>
									<Text color={theme.accent}>{on ? '❯ ' : '  '}</Text>
									<Text color={c.ready ? theme.final : theme.muted}>{c.ready ? '● ' : '○ '}</Text>
									<Box width={12}>
										<Text bold={on} color={c.ready ? undefined : theme.muted}>
											{c.label}
										</Text>
									</Box>
									<Text color={theme.muted}>{c.detail}</Text>
								</Box>
							);
						})}
				</Box>
			))}
			<Box marginTop={1} flexDirection="column">
				<Text color={theme.muted}>{t.signInHelp}</Text>
				<Text color={theme.muted}>{t.pickHint}</Text>
			</Box>
		</Box>
	);
}

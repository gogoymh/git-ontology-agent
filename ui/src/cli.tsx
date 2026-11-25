#!/usr/bin/env node
// goa-ui: the React (Ink) terminal UI for Git Ontology Agent. `goa` (Python) opens it; it runs `goa serve`.
import {render} from 'ink';
import {App, type Boot} from './app.js';
import {listConnections, saveDefault, spawnEngine} from './engine.js';
import {log, logPath} from './log.js';

const USAGE = `goa-ui [--llm provider[:model]] [--decision provider[:model]] [--lang xx] [--repo DIR] [--mock]

  --llm       claude-code, codex, openrouter, anthropic, openai, ... or offline (default: ask once, then remember)
  --decision  decision model (default auto: the same model as --llm)
  --lang      answer language (default: system locale)
  --repo      git repo that stores the conversation (default .goa)
  --mock      simulated providers: no key, no network`;

function parse(argv: string[]): {boot: Boot; engineArgs: string[]} {
	const boot: Boot = {};
	const engineArgs: string[] = [];
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i]!;
		const [flag, inline] = a.startsWith('--') && a.includes('=') ? [a.slice(0, a.indexOf('=')), a.slice(a.indexOf('=') + 1)] : [a, undefined];
		const value = () => inline ?? argv[++i] ?? '';
		if (flag === '--help' || flag === '-h') {
			console.log(USAGE);
			process.exit(0);
		} else if (flag === '--llm') boot.llm = value();
		else if (flag === '--decision') boot.decision = value();
		else if (flag === '--lang') {
			boot.lang = value();
			engineArgs.push('--lang', boot.lang);
		} else if (flag === '--repo') engineArgs.push('--repo', value());
		else if (flag === '--mock') engineArgs.push('--mock');
		else {
			console.error(`goa-ui: unknown option ${a}\n\n${USAGE}`);
			process.exit(2);
		}
	}
	return {boot, engineArgs};
}

const {boot, engineArgs} = parse(process.argv.slice(2));
log(`ui start argv=${JSON.stringify(process.argv.slice(2))} log=${logPath}`);
const app = render(
	<App
		boot={boot}
		deps={{
			listConnections,
			saveDefault,
			start: (llm, decision) => spawnEngine(['--llm', llm, '--decision', decision, ...engineArgs]),
		}}
	/>,
);
await app.waitUntilExit();
log('ui exit');
// let the engine read "quit" and close before this process goes
setTimeout(() => process.exit(0), 200);

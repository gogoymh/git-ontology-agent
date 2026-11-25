// English log lines for every UI event, in one file per session (GOA_UI_LOG, default <tmp>/goa-ui.log).
import {appendFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

export const logPath = process.env['GOA_UI_LOG'] || join(tmpdir(), 'goa-ui.log');
let started = false;

export function log(msg: string): void {
	try {
		const line = `${new Date().toISOString()} ${msg}\n`;
		if (started) {
			appendFileSync(logPath, line);
		} else {
			writeFileSync(logPath, line); // a fresh file per session keeps it small
			started = true;
		}
	} catch {
		// logging must never break the UI
	}
}

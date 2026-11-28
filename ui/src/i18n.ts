// UI strings. The engine writes the answers in any language; these cover the chrome around them.
const en = {
	tagline: 'every request is a branch, every answer is a merge',
	placeholder: 'Talk any time, even while it works',
	hints: 'enter send · /connect · /log · /help · ctrl+c quit',
	starting: 'Starting the engine…',
	checking: 'Checking your sign-ins and API keys…',
	reading: 'Reading your message…',
	open: (n: number) => (n === 1 ? '1 open request' : `${n} open requests`),
	idle: 'idle',
	pickTitle: 'Connect a model',
	pickHint: '↑↓ choose · enter connect · esc cancel',
	signIn: 'Sign in',
	apiKey: 'API key',
	offline: 'Offline',
	signInHelp: 'Not listed? Run goa login claude or goa login codex, then /connect.',
	connected: 'Connected',
	judge: 'judge',
	help: [
		'/connect   pick a sign-in or API key (saved for next time)',
		'/log       the conversation as a git graph',
		'/history N load N older turns (default 50)',
		'/quit      leave (ctrl+c works too)',
		'Send a message any time: follow-ups join the request in progress or run as their own branch.',
	],
	unknown: (c: string) => `Unknown command ${c}. Try /help.`,
	earlier: '── earlier turns ──',
	earlierEnd: (more: boolean): string => (more ? '── /history for more ──' : '── start of the conversation ──'),
	noOlder: 'No older turns.',
	historyUsage: 'Usage: /history [N], N a positive number.',
	exited: (code: number | null) => `The engine stopped (exit ${code ?? '?'}).`,
	final: 'final',
	interim: 'interim',
};

const ko: typeof en = {
	tagline: '요청마다 브랜치, 답마다 머지',
	placeholder: '작업 중에도 언제든 말을 걸 수 있습니다',
	hints: 'enter 보내기 · /connect · /log · /help · ctrl+c 종료',
	starting: '엔진을 시작하는 중…',
	checking: '로그인과 API 키를 확인하는 중…',
	reading: '메시지를 읽는 중…',
	open: (n: number) => `진행 중 요청 ${n}개`,
	idle: '대기 중',
	pickTitle: '연결할 모델',
	pickHint: '↑↓ 고르기 · enter 연결 · esc 취소',
	signIn: '로그인',
	apiKey: 'API 키',
	offline: '오프라인',
	signInHelp: '목록에 없으면 goa login claude 또는 goa login codex 후 /connect.',
	connected: '연결됨',
	judge: '판단',
	help: [
		'/connect   로그인이나 API 키 고르기 (다음에도 기억)',
		'/log       대화를 git 그래프로 보기',
		'/history N 이전 대화 N건 더 보기 (기본 50)',
		'/quit      나가기 (ctrl+c도 됩니다)',
		'작업 중에도 메시지를 보내면, 진행 중 요청에 합쳐지거나 따로 브랜치로 처리됩니다.',
	],
	unknown: (c: string) => `${c}는 없는 명령입니다. /help를 보세요.`,
	earlier: '── 이전 대화 ──',
	earlierEnd: (more: boolean) => (more ? '── 더 보려면 /history ──' : '── 대화의 처음 ──'),
	noOlder: '더 이전 대화가 없습니다.',
	historyUsage: '사용법: /history [N], N은 양의 정수.',
	exited: (code: number | null) => `엔진이 멈췄습니다 (exit ${code ?? '?'}).`,
	final: '최종',
	interim: '중간',
};

export type Strings = typeof en;

/** Same order as the engine: GOA_LANG, then the system locale. */
export function detectLang(explicit = ''): string {
	for (const v of [explicit, process.env['GOA_LANG'], process.env['LC_ALL'], process.env['LANG']]) {
		if (v) return v.split('.')[0]!.split('_')[0]!.split('-')[0]!.toLowerCase() || 'en';
	}
	try {
		return Intl.DateTimeFormat().resolvedOptions().locale.split('-')[0]!.toLowerCase();
	} catch {
		return 'en';
	}
}

export function strings(lang: string): Strings {
	return lang === 'ko' ? ko : en;
}

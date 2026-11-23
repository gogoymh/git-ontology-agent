// Builds docs/goa-intro-ko.pptx, a 3-slide Korean intro to Git Ontology Agent.
//   npm i pptxgenjs && node scripts/make_deck_ko.js [out.pptx] [firstSlide]
// firstSlide (1-3) moves one slide to the front; used only for Quick Look previews.
const path = require("path");
const pptxgen = require("pptxgenjs");

const ROOT = path.resolve(__dirname, "..");
const OUT = process.argv[2] || path.join(ROOT, "docs", "goa-intro-ko.pptx");
const FIRST = Number(process.argv[3] || 1);

const INK = "1D1D1F", SUB = "6E6E73", BG = "F5F5F7", CARD = "FFFFFF", LINE = "D2D2D7";
const VIOLET = "7C5CFF", VIOLET_BG = "EDE9FE", AMBER = "B45309", AMBER_BG = "FFF4E5", GREY_BG = "EFEFF2";
const FONT = "Apple SD Gothic Neo";

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625 in
pres.title = "Git Ontology Agent 소개";
pres.theme = { headFontFace: FONT, bodyFontFace: FONT };

const T = (s, text, o) => s.addText(text, Object.assign({ isTextBox: true, fontFace: FONT, color: INK, margin: 0, valign: "top" }, o));
const card = (s, x, y, w, h, fill, name) =>
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.12, fill: { color: fill || CARD }, line: { color: fill || CARD }, objectName: name,
    shadow: { type: "outer", color: "000000", opacity: 0.06, blur: 8, offset: 2, angle: 90 } });
const pill = (s, x, y, w, text, fill, color) => {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.28, rectRadius: 0.14, fill: { color: fill }, line: { color: fill } });
  T(s, text, { x, y, w, h: 0.28, fontSize: 10, bold: true, color, align: "center", valign: "middle" });
};
const head = (s, kicker, title) => {
  T(s, kicker, { x: 0.5, y: 0.38, w: 9, h: 0.3, fontSize: 12, bold: true, color: VIOLET });
  T(s, title, { x: 0.5, y: 0.66, w: 9, h: 0.6, fontSize: 26, bold: true });
};

function slide1(s) {
  s.background = { color: BG };
  // banner art (left 860px of the 1914x822 banner)
  const H = 4.4, W = H * 1914 / 822;
  s.addImage({ path: path.join(ROOT, "docs", "assets", "banner.png"), x: 0.2, y: 0.6, w: W, h: H,
    sizing: { type: "crop", x: 0.15, y: 0, w: H * 790 / 822, h: H }, altText: "요청 브랜치가 갈라졌다가 병합되는 모양의 GOA 상징" });
  T(s, "Git Ontology Agent", { x: 4.85, y: 0.55, w: 4.7, h: 0.3, fontSize: 13, bold: true, color: VIOLET });
  T(s, "요청은 브랜치로,\n답변은 병합으로", { x: 4.85, y: 0.9, w: 4.7, h: 1.2, fontSize: 30, bold: true, lineSpacingMultiple: 0.95 });
  T(s, "일하는 중에 새 요청이 와도 멈추지 않는 에이전트입니다.", { x: 4.85, y: 2.15, w: 4.7, h: 0.5, fontSize: 13, color: SUB });

  const rows = [
    ["Git", "요청마다 브랜치를 열어\n여러 요청을 동시에 처리합니다."],
    ["온톨로지", "작업을 의존 관계로 기록해\n영향받는 일만 다시 합니다."],
  ];
  rows.forEach(([k, v], i) => {
    const y = 2.8 + i * 0.78;
    card(s, 4.85, y, 4.65, 0.66, CARD, "why-" + i);
    T(s, k, { x: 5.05, y: y + 0.12, w: 1.05, h: 0.42, fontSize: 14, bold: true, color: VIOLET, valign: "middle" });
    T(s, v, { x: 6.1, y: y + 0.1, w: 3.25, h: 0.46, fontSize: 11, valign: "middle" });
  });
  T(s, "결과: 즉시 중간 답변, 병합 후 최종 답변, 요청별 출처 기록", { x: 4.85, y: 4.45, w: 4.65, h: 0.5, fontSize: 11, color: SUB });
  s.addNotes("GOA는 대화의 요청 하나하나를 Git 브랜치로 다룹니다. 일하는 중에 새 요청이 와도 하던 브랜치는 그대로 두고, 온톨로지가 무엇을 다시 해야 하는지 알려 줍니다.");
}

function slide2(s) {
  s.background = { color: BG };
  head(s, "새 메시지가 오면", "관계를 먼저 판단하고, 영향받는 일만 다시 합니다");
  // left: relation table
  card(s, 0.5, 1.5, 4.35, 3.6, CARD, "relations");
  const rel = [
    ["보완", "\"예산 50만 원 이하로\"", "같은 브랜치에 조건 추가"],
    ["정정", "\"부산 말고 대구\"", "옛 전제를 무효로 하고 다시"],
    ["독립", "\"내일 회의 몇 시야?\"", "새 브랜치에서 따로 처리"],
    ["취소", "\"그만해, 취소야\"", "병합하지 않고 닫기"],
    ["진행", "\"어디까지 했어?\"", "바꾸지 않고 현황만 답변"],
  ];
  T(s, "판단 모델이 다섯 가지 중 하나를 고릅니다", { x: 0.7, y: 1.65, w: 4, h: 0.3, fontSize: 11, bold: true, color: SUB });
  rel.forEach(([k, ex, act], i) => {
    const y = 2.05 + i * 0.6;
    pill(s, 0.7, y + 0.08, 0.62, k, VIOLET_BG, VIOLET);
    T(s, ex, { x: 1.45, y: y, w: 3.3, h: 0.25, fontSize: 11, bold: true });
    T(s, act, { x: 1.45, y: y + 0.26, w: 3.3, h: 0.25, fontSize: 10, color: SUB });
  });

  // right: dependency graph for the budget supplement
  card(s, 5.1, 1.5, 4.4, 3.6, CARD, "graph");
  T(s, "예: 예산이 추가된 경우", { x: 5.3, y: 1.65, w: 4, h: 0.3, fontSize: 11, bold: true, color: SUB });
  const node = (x, y, label, state, fill, color) => {
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 1.25, h: 0.56, rectRadius: 0.1, fill: { color: fill }, line: { color: color, width: 1 } });
    T(s, [{ text: label, options: { bold: true, breakLine: true } }, { text: state, options: { color, fontSize: 9 } }],
      { x, y: y + 0.06, w: 1.25, h: 0.46, fontSize: 10, align: "center", valign: "middle" });
  };
  const arrow = (x1, y1, x2, y2) => s.addShape(pres.shapes.LINE, { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1) || 0.001, h: Math.abs(y2 - y1) || 0.001,
    flipV: y2 < y1, line: { color: "8E8E93", width: 1, endArrowType: "triangle" } });
  // columns
  const c1 = 5.3, c2 = 6.7, c3 = 8.1, r1 = 2.1, r2 = 2.95, r3 = 3.8;
  node(c1, r1, "도시: 부산", "유지", GREY_BG, SUB);
  node(c1, r3, "예산 50만 원", "새로 추가", VIOLET_BG, VIOLET);
  node(c2, r1, "KTX", "유지", GREY_BG, SUB);
  node(c2, r2, "호텔 후보", "다시", AMBER_BG, AMBER);
  node(c3, r2, "호텔 선택", "다시", AMBER_BG, AMBER);
  node(c3, r1, "총 일정", "다시", AMBER_BG, AMBER);
  arrow(c1 + 1.25, r1 + 0.28, c2, r1 + 0.28);          // city -> KTX
  arrow(c1 + 1.25, r1 + 0.4, c2, r2 + 0.18);           // city -> candidates
  arrow(c1 + 1.25, r3 + 0.16, c2, r2 + 0.4);           // budget -> candidates
  arrow(c2 + 1.25, r2 + 0.28, c3, r2 + 0.28);          // candidates -> hotel
  arrow(c3 + 0.62, r2, c3 + 0.62, r1 + 0.56);          // hotel -> total
  arrow(c2 + 1.25, r1 + 0.28, c3, r1 + 0.28);          // KTX -> total
  T(s, "화살표는 depends_on 의존 관계입니다. 예산에 기대는 항목만 다시 하고, KTX는 그대로 둡니다.",
    { x: 6.7, y: 3.85, w: 2.65, h: 1.1, fontSize: 10, color: SUB });
  s.addNotes("새 메시지가 오면 판단 모델이 진행 중인 요청과의 관계를 보완, 정정, 독립, 취소, 진행 중 하나로 고릅니다. 보완이나 정정이면 바뀐 항목에 의존하는 항목만 다시 합니다. 예산이 추가되면 호텔 후보, 호텔 선택, 총 일정만 다시 하고 KTX는 유지합니다.");
}

function slide3(s) {
  s.background = { color: BG };
  head(s, "끝낼 때", "타입 규칙으로 병합하고, 상태에 맞게 답합니다");
  card(s, 0.5, 1.5, 4.35, 3.6, CARD, "merge-rules");
  T(s, "충돌은 항목 타입별 규칙이 대부분 정리합니다", { x: 0.7, y: 1.65, w: 4, h: 0.3, fontSize: 11, bold: true, color: SUB });
  const rules = [
    ["제약", "모두 쌓아서 함께 지킵니다"],
    ["전제", "정정한 값이 이깁니다"],
    ["결정", "의존 항목이 바뀌면 다시 계산합니다"],
    ["확인 사실", "나중에 확인한 값이 이깁니다"],
    ["진행 상태", "브랜치 안에만 두고 병합하지 않습니다"],
    ["요청", "모두 보관합니다"],
  ];
  rules.forEach(([k, v], i) => {
    const y = 2.05 + i * 0.5;
    T(s, k, { x: 0.7, y, w: 1.1, h: 0.36, fontSize: 12, bold: true, color: VIOLET, valign: "middle" });
    T(s, v, { x: 1.85, y, w: 2.9, h: 0.36, fontSize: 11, valign: "middle" });
    if (i < rules.length - 1) s.addShape(pres.shapes.LINE, { x: 0.7, y: y + 0.43, w: 3.95, h: 0, line: { color: "E5E5EA", width: 0.75 } });
  });

  const ans = [
    ["중간 답변", "진행 중인 브랜치의 최신 커밋", "확정된 것은 단정하고, 진행 중인 것은 진행 중이라고 말합니다.", "\"KTX는 화요일 아침으로 정했고, 호텔은 다시 고르는 중입니다.\""],
    ["최종 답변", "모든 요청이 병합된 커밋", "각 부분의 출처 요청을 밝히고, 남은 충돌은 묻습니다.", "\"총 46만 원으로 예산 안입니다. 예산은 두 번째 요청에서 왔습니다.\""],
  ];
  ans.forEach(([k, src, how, ex], i) => {
    const y = 1.5 + i * 1.85;
    card(s, 5.1, y, 4.4, 1.75, CARD, "answer-" + i);
    T(s, k, { x: 5.3, y: y + 0.15, w: 1.6, h: 0.34, fontSize: 15, bold: true });
    pill(s, 6.75, y + 0.18, 2.55, src, i ? VIOLET_BG : GREY_BG, i ? VIOLET : SUB);
    T(s, how, { x: 5.3, y: y + 0.58, w: 4.0, h: 0.5, fontSize: 11 });
    T(s, ex, { x: 5.3, y: y + 1.1, w: 4.0, h: 0.5, fontSize: 10, color: SUB });
  });
  s.addNotes("병합할 때 Git은 줄이 겹친다는 것만 알지만, 온톨로지는 항목 타입마다 합치는 규칙을 압니다. 중간 답변은 진행 중인 브랜치에서, 최종 답변은 병합 커밋에서 씁니다. 답변도 커밋으로 남아서 그때 왜 그렇게 답했는지 다시 볼 수 있습니다.");
}

const builders = [slide1, slide2, slide3];
const order = [FIRST - 1, ...[0, 1, 2].filter(i => i !== FIRST - 1)];
order.forEach(i => builders[i](pres.addSlide()));
pres.writeFile({ fileName: OUT }).then(f => console.log("wrote", f));

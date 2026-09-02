const DEFAULT_NOTION_URL = "https://app.notion.com/p/sungbaeson/3b90d6651bb680a59147ec5ecd56cb98";
const ENTRY_KEY = "ojt-assistant-entries-v1";
const MIGRATION_KEY = "ojt-assistant-entries-migrated-v1";
const SETTINGS_KEY = "ojt-assistant-settings-v1";
const isCompactMode = new URLSearchParams(window.location.search).get("compact") === "1";

if (isCompactMode) document.title = "OJT 미니 도우미";

const state = {
  aiConfigured: false,
  provider: "local-rules",
  model: "",
  currentCompetencies: [],
  currentSystems: [],
  activityTopics: [],
  entries: [],
  monitorEnabled: true,
  editingId: null,
  settings: loadSettings(),
};

const el = (id) => document.getElementById(id);
const entryDate = el("entryDate");
const workMemo = el("workMemo");
const performedTasks = el("performedTasks");
const issues = el("issues");
const reflection = el("reflection");
const generateButton = el("generateButton");
const notionUrl = el("notionUrl");
const ojtStartDate = el("ojtStartDate");

function localDateISO(offsetDays = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

function loadSettings() {
  try {
    return {
      startDate: "2026-08-10",
      notionUrl: DEFAULT_NOTION_URL,
      ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"),
    };
  } catch {
    return { startDate: "2026-08-10", notionUrl: DEFAULT_NOTION_URL };
  }
}

function loadEntries() {
  return state.entries;
}

function readLegacyEntries() {
  try {
    const entries = JSON.parse(localStorage.getItem(ENTRY_KEY) || "[]");
    return Array.isArray(entries) ? entries : [];
  } catch {
    return [];
  }
}

async function refreshEntries() {
  try {
    const response = await fetch("/api/entries", { cache: "no-store" });
    const result = await response.json();
    state.entries = Array.isArray(result.entries) ? result.entries : [];
  } catch {
    state.entries = [];
  }
  renderHistory();
}

// 예전 버전이 브라우저에만 저장해 둔 기록을 로컬 DB로 한 번 옮긴다.
async function migrateLegacyEntries() {
  if (localStorage.getItem(MIGRATION_KEY) === "done") return;
  const legacy = readLegacyEntries();
  if (!legacy.length) {
    localStorage.setItem(MIGRATION_KEY, "done");
    return;
  }
  let moved = 0;
  for (const entry of legacy) {
    try {
      const response = await fetch("/api/entries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(entry),
      });
      if (response.ok) moved += 1;
    } catch {
      return;
    }
  }
  localStorage.setItem(MIGRATION_KEY, "done");
  if (moved) showToast(`이전 기록 ${moved}건을 이 노트북 저장소로 옮겼습니다.`);
}

async function syncSettings() {
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const result = await response.json();
    if (result.settings && Object.keys(result.settings).length) {
      state.settings = { ...state.settings, ...result.settings };
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
      ojtStartDate.value = state.settings.startDate;
      notionUrl.value = state.settings.notionUrl;
      refreshStage();
    } else {
      await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: state.settings }),
      });
    }
  } catch {
    /* 서버가 아직 응답하지 않으면 브라우저에 저장된 설정을 그대로 쓴다. */
  }
}

function computeWeek(dateString) {
  const start = new Date(`${state.settings.startDate}T00:00:00`);
  const target = new Date(`${dateString}T00:00:00`);
  const diff = Math.floor((target - start) / 86400000);
  return Math.max(1, Math.min(12, Math.floor(diff / 7) + 1));
}

function stageForWeek(week) {
  if (week <= 3) return "회사·사업 구조 및 IT 기초 파악";
  if (week <= 6) return "시스템 구조 및 운영 프로세스 이해";
  if (week <= 9) return "실무 참여 및 장애·운영 대응";
  return "독립 업무 수행 및 개선 과제";
}

function refreshStage() {
  const week = computeWeek(entryDate.value || localDateISO(-1));
  el("stageLabel").textContent = stageForWeek(week).replace("회사·사업 구조 및 ", "");
  el("weekLabel").textContent = `OJT ${week}주차`;
}

function selectedActivityTopics() {
  return state.activityTopics.filter((topic) => topic.selected);
}

function buildSelectedWorkText() {
  const topicText = selectedActivityTopics()
    .map((topic, index) => `${index + 1}. ${topic.title}${topic.description ? ` - ${topic.description}` : ""}`)
    .join("\n");
  return [topicText, workMemo.value.trim()].filter(Boolean).join("\n");
}

function formatTrackedTime(seconds) {
  const minutes = Math.round(Number(seconds || 0) / 60);
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}시간 ${rest}분` : `${hours}시간`;
}

function refreshSelectedCount() {
  el("selectedCount").textContent = `${selectedActivityTopics().length}개 선택`;
}

function renderTopics() {
  const container = el("topicList");
  container.innerHTML = "";
  if (!state.activityTopics.length) {
    const empty = document.createElement("div");
    empty.className = "topic-empty";
    empty.textContent = "선택한 날짜에 정리할 모니터링 기록이 없습니다.";
    container.appendChild(empty);
    refreshSelectedCount();
    return;
  }
  state.activityTopics.forEach((topic) => {
    const item = document.createElement("div");
    item.className = "topic-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "topic-select";
    checkbox.setAttribute("aria-label", `${topic.title} 선택`);
    checkbox.checked = Boolean(topic.selected);
    checkbox.addEventListener("change", () => {
      topic.selected = checkbox.checked;
      refreshSelectedCount();
    });

    const body = document.createElement("div");
    body.className = "topic-body";
    const titleRow = document.createElement("div");
    titleRow.className = "topic-title-row";
    const title = document.createElement("input");
    title.className = "topic-title topic-edit-title";
    title.type = "text";
    title.maxLength = 80;
    title.value = topic.title;
    title.setAttribute("aria-label", "업무 후보 제목 수정");
    title.addEventListener("input", () => {
      topic.title = title.value;
      checkbox.setAttribute("aria-label", `${topic.title || "업무 후보"} 선택`);
    });
    const time = document.createElement("span");
    time.className = "topic-time";
    const detailParts = [`${topic.minutes || 1}분`];
    if (Number(topic.clickCount || 0) > 0) detailParts.push(`${topic.clickCount}개 조작`);
    time.textContent = detailParts.join(" · ");
    titleRow.append(title, time);
    body.appendChild(titleRow);

    const description = document.createElement("textarea");
    description.className = "topic-description topic-edit-description";
    description.rows = 2;
    description.maxLength = 220;
    description.value = topic.description || "";
    description.placeholder = "확인된 세부 조작을 수정할 수 있습니다.";
    description.setAttribute("aria-label", "업무 후보 설명 수정");
    description.addEventListener("input", () => {
      topic.description = description.value;
    });
    body.appendChild(description);
    if (Array.isArray(topic.apps) && topic.apps.length) {
      const apps = document.createElement("div");
      apps.className = "topic-apps";
      apps.textContent = topic.apps.join(" · ");
      body.appendChild(apps);
    }
    item.append(checkbox, body);
    container.appendChild(item);
  });
  refreshSelectedCount();
}

async function refreshActivityStatus() {
  try {
    const response = await fetch(`/api/activity/status?date=${encodeURIComponent(entryDate.value || localDateISO())}`, { cache: "no-store" });
    const status = await response.json();
    state.monitorEnabled = Boolean(status.enabled);
    document.querySelector(".monitor-card").classList.toggle("paused", !state.monitorEnabled);
    el("monitorLabel").textContent = state.monitorEnabled ? "업무 활동 모니터링 중" : "모니터링 일시정지됨";
    const clickDetail = status.interactionMonitoring ? ` · 버튼·메뉴 ${status.interactionCount || 0}회` : "";
    el("monitorDetail").textContent = `${formatTrackedTime(status.trackedSeconds)} 기록${clickDetail} · Windows 계정 암호화 저장`;
    el("toggleMonitorButton").textContent = state.monitorEnabled ? "일시정지" : "다시 시작";
  } catch {
    el("monitorLabel").textContent = "모니터링 상태를 확인할 수 없음";
    el("monitorDetail").textContent = "프로그램을 다시 실행해보세요.";
  }
}

async function toggleMonitoring() {
  try {
    const response = await fetch("/api/activity/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !state.monitorEnabled }),
    });
    if (!response.ok) throw new Error();
    await refreshActivityStatus();
    showToast(state.monitorEnabled ? "업무 활동 모니터링을 시작했습니다." : "업무 활동 모니터링을 일시정지했습니다.");
  } catch {
    showToast("모니터링 상태를 변경하지 못했습니다.");
  }
}

async function loadActivityTopics(force = false) {
  const button = el("loadTopicsButton");
  const refreshButton = el("refreshTopicsButton");
  button.disabled = true;
  refreshButton.disabled = true;
  button.firstElementChild.textContent = force ? "기록을 다시 정리하는 중..." : "활동 기록을 정리하는 중...";
  try {
    const response = await fetch("/api/activity/topics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: entryDate.value, force }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "정리에 실패했습니다.");
    state.activityTopics = (result.topics || []).map((topic) => ({ ...topic, selected: false }));
    renderTopics();
    if (!state.activityTopics.length) {
      showToast("선택한 날짜에 기록된 활동이 없습니다.");
    } else {
      if (result.cached) {
        const at = String(result.cachedAt || "").slice(11, 16);
        showToast(`저장된 정리 결과 ${state.activityTopics.length}개입니다${at ? ` (${at} 기준)` : ""}. 최근 기록까지 반영하려면 다시 정리를 누르세요.`);
      } else {
        showToast(`${state.activityTopics.length}개의 업무 후보를 만들었습니다.`);
      }
    }
  } catch (error) {
    showToast(error.message || "활동 기록을 정리하지 못했습니다.");
  } finally {
    button.disabled = false;
    refreshButton.disabled = false;
    button.firstElementChild.textContent = "모니터링 내역 정리";
  }
}

async function refineReflection() {
  const button = el("refineButton");
  if (!performedTasks.value.trim()) {
    showToast("먼저 OJT 초안을 생성해주세요.");
    return;
  }
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "AI가 다듬는 중...";
  try {
    const week = computeWeek(entryDate.value);
    const response = await fetch("/api/refine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stage: `${week}주차 · ${stageForWeek(week)}`,
        performedTasks: performedTasks.value.trim(),
        issues: issues.value.trim(),
        competencyCandidates: state.currentCompetencies,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error === "AI_NOT_CONFIGURED" ? "로컬 AI가 준비되지 않았습니다." : result.error);
    reflection.value = result.reflection;
    showToast("학습 내용을 다듬었습니다. 내용을 확인해주세요.");
  } catch (error) {
    showToast(error.message || "학습 내용을 다듬지 못했습니다.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function inferSystems(text) {
  const matches = [];
  const rules = [
    [/acorn|에이콘/i, "Acorn"],
    [/리드\s*플래닛|lead\s*planet/i, "리드플래닛"],
    [/다우\s*오피스|그룹웨어|전자결재|사내\s*메일/i, "다우오피스"],
    [/\bpos\b|포스|가맹점|매장/i, "POS·매장"],
    [/excel|엑셀|powerpoint|ppt|office/i, "Office"],
  ];
  rules.forEach(([pattern, label]) => pattern.test(text) && matches.push(label));
  return matches;
}

function normalizeLines(text) {
  return text
    .replace(/\r/g, "")
    .split(/\n+|(?<=[.!?다음함됨음])\s+(?=[가-힣A-Za-z0-9])/)
    .map((line) => line.replace(/^[-•*\d.)\s]+/, "").trim())
    .filter(Boolean);
}

function inferIssueLines(lines) {
  const issuePattern = /어려|오류|에러|문제|장애|실패|지연|부족|헷갈|숙지.*필요|추가.*확인.*필요|권한.*없|접속.*안|되지 않|불가/;
  return lines.filter((line) => issuePattern.test(line));
}

function classifyLine(line) {
  if (/계정|권한|로그인|접속|발급|세팅|설정/.test(line)) return "access";
  if (/매출|데이터|자료|대조|검증|조회|csv|excel|엑셀|리포트|보고서/i.test(line)) return "data";
  if (/장애|오류|에러|문의|지원|원격|조치|복구|설치/.test(line)) return "support";
  if (/개발|구현|수정|테스트|배포|코드/.test(line)) return "development";
  if (/교육|학습|파악|확인|살펴|메뉴|기능/.test(line)) return "learning";
  return "general";
}

const sectionTitles = {
  access: "시스템 계정 및 접근 환경 확인",
  data: "운영 데이터 조회 및 검증",
  support: "IT 운영 지원 및 이슈 대응",
  development: "시스템 개선 및 개발 업무",
  learning: "업무 시스템 및 프로세스 파악",
  general: "주요 업무 수행",
};

function ensureSentence(line) {
  const cleaned = line.replace(/[.。]+$/, "").trim();
  if (!cleaned) return "";
  if (/(함|됨|확인|파악|진행|수행|처리|완료|검토|테스트|교육|발급|부여)$/.test(cleaned)) return cleaned;
  return cleaned;
}

function inferCompetencies(text, week) {
  const found = [];
  const add = (condition, label) => condition && !found.includes(label) && found.push(label);
  add(/계정|권한|로그인|접속|발급/.test(text), "사내 시스템 계정 및 권한 세팅");
  add(/acorn|에이콘|pos|포스|매장/i.test(text), "POS·매장 운영 시스템 구조 이해");
  add(/가맹점|본사|리드\s*플래닛/i.test(text), "본사·가맹점 데이터 흐름 이해");
  add(/다우\s*오피스|그룹웨어|전자결재|메일/.test(text), "그룹웨어 및 기본 업무 방식 이해");
  add(/매출|데이터|대조|검증|조회|엑셀|csv/i.test(text), "운영 데이터 검증 및 분석");
  add(/장애|오류|문의|지원|원격|조치/.test(text), "장애·운영 대응 경험");
  add(/개발|구현|수정|테스트|배포/.test(text), "시스템 개선 과제 수행");
  if (!found.length) {
    found.push(week <= 3 ? "기본 커뮤니케이션 및 업무 방식 이해" : week <= 6 ? "시스템 운영 프로세스 이해" : week <= 9 ? "실무 업무 참여 확대" : "독립 업무 수행 역량 강화");
  }
  return found.slice(0, 4);
}

function buildLocalDraft() {
  const sourceText = buildSelectedWorkText();
  const lines = normalizeLines(sourceText);
  const groups = new Map();
  lines.forEach((line) => {
    const type = classifyLine(line);
    if (!groups.has(type)) groups.set(type, []);
    groups.get(type).push(ensureSentence(line));
  });
  const performed = [...groups.entries()]
    .map(([type, items], index) => `${index + 1}. ${sectionTitles[type]}\n${items.map((item) => `• ${item}`).join("\n")}`)
    .join("\n\n");

  const week = computeWeek(entryDate.value);
  const allText = sourceText;
  const competencies = inferCompetencies(allText, week);
  const systems = inferSystems(allText);
  const issueLines = inferIssueLines(lines);
  const issueText = issueLines.length
    ? issueLines.map((line) => `• ${ensureSentence(line)}`).join("\n")
    : "특이사항 없음";
  const reflectionText = `• 금일 수행한 업무를 통해 ${competencies[0]} 관련 기본 업무 흐름을 확인함\n• 관련 세부 기준은 실제 업무를 수행하며 추가로 숙지할 예정`;

  return { performedTasks: performed, issues: issueText, reflection: reflectionText, competencies, systems };
}

async function generateDraft() {
  const selectedWork = buildSelectedWorkText();
  if (!selectedWork) {
    showToast("업무 후보를 선택하거나 추가 메모를 입력해주세요.");
    return;
  }
  setGenerating(true);
  const inferred = inferSystems(selectedWork);
  state.currentSystems = inferred;
  const week = computeWeek(entryDate.value);
  const payload = {
    date: entryDate.value,
    stage: `${week}주차 · ${stageForWeek(week)}`,
    workMemo: selectedWork,
    issueMemo: "",
    reflectionMemo: "",
    selectedTopics: selectedActivityTopics().map(({ title, description }) => ({ title, description })),
    manualMemo: workMemo.value.trim(),
    systems: inferred,
    competencyCandidates: inferCompetencies(selectedWork, week),
  };

  let draft;
  let source = "로컬 정리";
  if (state.aiConfigured) {
    try {
      const response = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "AI 작성에 실패했습니다.");
      draft = result.entry;
      source = `${result.provider || "AI"} · ${result.model}`;
    } catch (error) {
      draft = buildLocalDraft();
      showToast(`AI 연결이 어려워 로컬 초안으로 작성했습니다. ${error.message}`);
    }
  } else {
    draft = buildLocalDraft();
  }

  applyDraft(draft, source);
  setGenerating(false);
}

function applyDraft(draft, source = "작성 완료") {
  performedTasks.value = draft.performedTasks || "";
  issues.value = draft.issues || "특이사항 없음";
  reflection.value = draft.reflection || "";
  state.currentCompetencies = Array.isArray(draft.competencies) ? draft.competencies : [];
  state.currentSystems = Array.isArray(draft.systems) ? draft.systems : inferSystems(buildSelectedWorkText());
  renderCompetencies();
  el("emptyState").classList.add("hidden");
  el("draftEditor").classList.remove("hidden");
  el("draftStatus").textContent = source;
  el("draftStatus").classList.add("ready");
  if (isCompactMode) {
    document.documentElement.classList.add("compact-result-ready");
    window.setTimeout(() => document.querySelector(".output-panel").scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }
}

function setGenerating(active) {
  generateButton.disabled = active;
  el("generateButtonText").textContent = active ? "초안을 정리하는 중..." : "OJT 초안 생성";
}

function renderCompetencies() {
  const container = el("competencyTags");
  container.innerHTML = "";
  state.currentCompetencies.forEach((item) => {
    const tag = document.createElement("span");
    tag.textContent = item;
    container.appendChild(tag);
  });
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    showToast(successMessage);
  }
}

function compactCell(text) {
  return text.replace(/\n+/g, " / ").replace(/\t/g, " ").trim();
}

async function saveCurrentEntry() {
  if (!performedTasks.value.trim()) {
    showToast("먼저 OJT 초안을 생성해주세요.");
    return;
  }
  const entry = {
    id: state.editingId || undefined,
    date: entryDate.value,
    workMemo: buildSelectedWorkText(),
    manualMemo: workMemo.value.trim(),
    selectedTopics: selectedActivityTopics().map(({ selected, ...topic }) => topic),
    performedTasks: performedTasks.value.trim(),
    issues: issues.value.trim(),
    reflection: reflection.value.trim(),
    competencies: state.currentCompetencies,
    systems: state.currentSystems,
  };
  try {
    const response = await fetch("/api/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "저장하지 못했습니다.");
    state.editingId = result.entry.id;
    await refreshEntries();
    showToast("OJT 기록을 이 노트북에 저장했습니다.");
  } catch (error) {
    showToast(error.message || "기록을 저장하지 못했습니다.");
  }
}

function loadEntry(id) {
  const entry = loadEntries().find((item) => item.id === id);
  if (!entry) return;
  state.editingId = entry.id;
  entryDate.value = entry.date;
  workMemo.value = entry.manualMemo || (entry.selectedTopics?.length ? "" : entry.workMemo || "");
  state.activityTopics = (entry.selectedTopics || []).map((topic) => ({ ...topic, selected: true }));
  renderTopics();
  state.currentSystems = entry.systems || inferSystems(entry.workMemo || "");
  applyDraft(entry, "저장된 기록");
  refreshStage();
  refreshWorkCount();
  window.scrollTo({ top: document.querySelector(".workspace-grid").offsetTop - 20, behavior: "smooth" });
}

async function deleteEntry(id) {
  try {
    const response = await fetch("/api/entries/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    if (!response.ok) throw new Error();
    if (state.editingId === id) state.editingId = null;
    await refreshEntries();
    showToast("저장된 기록을 삭제했습니다.");
  } catch {
    showToast("기록을 삭제하지 못했습니다.");
  }
}

function formatKoreanDate(dateString) {
  return new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).format(new Date(`${dateString}T00:00:00`));
}

function renderHistory() {
  const entries = loadEntries().sort((a, b) => b.date.localeCompare(a.date) || b.updatedAt.localeCompare(a.updatedAt));
  const container = el("historyList");
  container.innerHTML = "";
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "아직 저장된 OJT 기록이 없습니다.";
    container.appendChild(empty);
  } else {
    entries.slice(0, 12).forEach((entry) => {
      const item = document.createElement("article");
      item.className = "history-item";
      item.tabIndex = 0;
      item.innerHTML = `
        <div>
          <div class="history-item-header">
            <span class="history-date">${escapeHtml(formatKoreanDate(entry.date))}</span>
            <span class="history-systems">${escapeHtml((entry.systems || []).join(" · ") || "일반 업무")}</span>
          </div>
          <p class="history-title">${escapeHtml(entry.workMemo || entry.performedTasks)}</p>
        </div>
        <div class="history-item-footer">
          <span>OJT ${computeWeek(entry.date)}주차</span>
          <button class="delete-history" type="button">삭제</button>
        </div>`;
      item.addEventListener("click", () => loadEntry(entry.id));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") loadEntry(entry.id);
      });
      item.querySelector(".delete-history").addEventListener("click", (event) => {
        event.stopPropagation();
        deleteEntry(entry.id);
      });
      container.appendChild(item);
    });
  }
  const current = new Date(`${entryDate.value || localDateISO()}T00:00:00`);
  const monday = new Date(current);
  const day = (monday.getDay() + 6) % 7;
  monday.setDate(monday.getDate() - day);
  const sunday = new Date(monday);
  sunday.setDate(sunday.getDate() + 6);
  const count = entries.filter((entry) => {
    const date = new Date(`${entry.date}T00:00:00`);
    return date >= monday && date <= sunday;
  }).length;
  el("weeklyCount").textContent = `${count}일`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function clearForm() {
  state.editingId = null;
  workMemo.value = "";
  performedTasks.value = "";
  issues.value = "";
  reflection.value = "";
  state.currentCompetencies = [];
  state.currentSystems = [];
  state.activityTopics.forEach((topic) => { topic.selected = false; });
  renderTopics();
  el("emptyState").classList.remove("hidden");
  el("draftEditor").classList.add("hidden");
  el("draftStatus").textContent = "작성 전";
  el("draftStatus").classList.remove("ready");
  document.documentElement.classList.remove("compact-result-ready");
  refreshWorkCount();
  workMemo.focus();
}

function refreshWorkCount() {
  el("workCount").textContent = `${workMemo.value.length.toLocaleString("ko-KR")}자`;
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3000);
}

function exportBackup() {
  const payload = { exportedAt: new Date().toISOString(), settings: state.settings, entries: loadEntries() };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `OJT_백업_${localDateISO()}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast("OJT 기록 백업 파일을 내려받았습니다.");
}

async function checkStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const status = await response.json();
    state.aiConfigured = Boolean(status.aiConfigured);
    state.provider = status.provider || "local-rules";
    state.model = status.model || "";
  } catch {
    state.aiConfigured = false;
    state.provider = "local-rules";
  }
  const badge = el("modeBadge");
  badge.classList.remove("ai", "local");
  badge.classList.add(state.aiConfigured ? "ai" : "local");
  const isLocalAi = state.provider === "local-ai";
  badge.innerHTML = `<span class="status-dot"></span>${isLocalAi ? "로컬 AI · 비용 0원" : state.provider === "openai" ? "OpenAI 정리 모드" : "로컬 규칙 모드"}`;
  el("aiSettingsMessage").textContent = isLocalAi
    ? `Ollama 로컬 AI 연결됨 · ${state.model} · 토큰 비용 없음`
    : state.provider === "openai"
      ? `OpenAI 연결됨 · ${state.model} · API 사용량에 따라 비용 발생`
      : "AI 모델 없음 · 비용 없는 규칙 기반 정리 사용";
}

async function saveSettings() {
  state.settings = {
    startDate: ojtStartDate.value || "2026-08-10",
    notionUrl: notionUrl.value.trim() || DEFAULT_NOTION_URL,
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
  refreshStage();
  renderHistory();
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: state.settings }),
    });
    showToast("기본 설정을 저장했습니다.");
  } catch {
    showToast("설정을 이 브라우저에만 저장했습니다.");
  }
}

document.querySelectorAll(".date-shortcut").forEach((button) => {
  button.addEventListener("click", () => {
    entryDate.value = localDateISO(button.dataset.date === "today" ? 0 : -1);
    state.activityTopics = [];
    renderTopics();
    refreshStage();
    renderHistory();
    refreshActivityStatus();
  });
});
entryDate.addEventListener("change", () => {
  state.activityTopics = [];
  renderTopics();
  refreshStage();
  renderHistory();
  refreshActivityStatus();
});
workMemo.addEventListener("input", refreshWorkCount);
generateButton.addEventListener("click", generateDraft);
el("clearButton").addEventListener("click", clearForm);
el("loadTopicsButton").addEventListener("click", () => loadActivityTopics(false));
el("refreshTopicsButton").addEventListener("click", () => loadActivityTopics(true));
el("refineButton").addEventListener("click", refineReflection);
el("toggleMonitorButton").addEventListener("click", toggleMonitoring);
el("saveButton").addEventListener("click", saveCurrentEntry);
el("openNotionButton").addEventListener("click", () => window.open(state.settings.notionUrl, "_blank", "noopener,noreferrer"));
el("copyRowButton").addEventListener("click", () => {
  const row = [entryDate.value.replaceAll("-", "/"), performedTasks.value, issues.value, reflection.value].map(compactCell).join("\t");
  copyText(row, "노션용 한 행을 복사했습니다. 첫 번째 셀에 붙여넣어 보세요.");
});
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => {
    const map = { performed: performedTasks, issues, reflection };
    copyText(map[button.dataset.copy].value, `${button.textContent.trim()} 완료`);
  });
});
el("settingsButton").addEventListener("click", () => {
  ojtStartDate.value = state.settings.startDate;
  notionUrl.value = state.settings.notionUrl;
  el("settingsDialog").showModal();
});
el("compactModeButton").addEventListener("click", async () => {
  if (isCompactMode) {
    window.open("/", "_blank", "noopener,noreferrer");
  } else {
    try {
      const response = await fetch("/api/mini/launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw new Error();
      showToast("OJT 미니 프로그램을 열었습니다.");
    } catch {
      showToast("미니 프로그램을 열지 못했습니다. 바탕화면 바로가기를 사용해주세요.");
    }
  }
});
el("saveSettingsButton").addEventListener("click", saveSettings);
el("exportButton").addEventListener("click", exportBackup);

entryDate.value = localDateISO();
el("compactModeButton").textContent = isCompactMode ? "전체 화면 ↗" : "미니 프로그램";
ojtStartDate.value = state.settings.startDate;
notionUrl.value = state.settings.notionUrl;
refreshStage();
refreshWorkCount();
renderHistory();
checkStatus();
refreshActivityStatus();
setInterval(refreshActivityStatus, 30000);

(async () => {
  await syncSettings();
  await migrateLegacyEntries();
  await refreshEntries();
})();

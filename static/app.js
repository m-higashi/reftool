"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

// フォルダのパス比較: 親を子より先に、サブツリーを連続させる(セグメント毎に比較)
function folderCmp(a, b) {
  const pa = a.split("/"), pb = b.split("/");
  const n = Math.min(pa.length, pb.length);
  for (let i = 0; i < n; i++) { if (pa[i] !== pb[i]) return pa[i].localeCompare(pb[i], "ja"); }
  return pa.length - pb.length;
}

// ---- 表示テーマ -------------------------------------------------------
// 既定は auto(端末の設定に追従)。light/dark を選んだときだけ data-theme を立てる。
// 実際の配色は style.css の3層で決まる(ここでは属性を出し入れするだけ)。
const THEMES = ["auto", "light", "dark"];
const THEME_ICON = { auto: "🌗", light: "☀️", dark: "🌙" };
const THEME_NAME = { auto: "端末の設定に合わせる", light: "ライト", dark: "ダーク" };

function currentTheme() {
  const t = localStorage.getItem("reftool-theme");
  return THEMES.includes(t) ? t : "auto";
}
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  const btn = $("#btn-theme");
  if (btn) { btn.textContent = THEME_ICON[t]; btn.title = `表示テーマ: ${THEME_NAME[t]}(クリックで切り替え)`; }
  const sel = $("#set-theme");
  if (sel) sel.value = t;
}
function setTheme(t) {
  if (t === "auto") localStorage.removeItem("reftool-theme");
  else localStorage.setItem("reftool-theme", t);
  applyTheme(t);
}
function cycleTheme() {
  const nv = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  setTheme(nv);
  toast("表示テーマ: " + THEME_NAME[nv]);
}

let CFG = null;
let PAGE = 1;
let PER = 50;
let SELECTED = null;
let SEARCH_TIMER = null;
let CURSOR = -1;      // キーボード操作で選んでいる行(一覧内のindex)
let DIRTY = null;     // 詳細パネルの未保存状態 {id, editors, isDirty(), save()}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), 2200);
}

// ---- 確認のしかた -----------------------------------------------------
// confirm() は使わない。「何が起きるか」をページ内に出し、実行ボタンを押させる。
function showPlan({ title, lines, actions }) {
  const box = $("#plan");
  box.innerHTML = "";
  box.classList.remove("hidden");
  box.appendChild(el("div", "plan-head", title));
  if (lines && lines.length) {
    const ul = el("ul");
    lines.forEach((t) => ul.appendChild(el("li", null, t)));
    box.appendChild(ul);
  }
  const btns = el("div", "row-btns");
  actions.forEach((a) => {
    const b = el("button", a.cls || null, a.label);
    b.onclick = async () => {
      btns.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { if (a.run) await a.run(); } catch (e) { toast("失敗: " + e.message); }
      hidePlan();
    };
    btns.appendChild(b);
  });
  // 実行するものが無い(お知らせだけ)のときは「閉じる」にする
  const cancel = el("button", null, actions.length ? "やめる" : "閉じる");
  cancel.onclick = hidePlan;
  btns.appendChild(cancel);
  box.appendChild(btns);
  box.scrollIntoView({ block: "nearest" });
}
function hidePlan() { const b = $("#plan"); b.classList.add("hidden"); b.innerHTML = ""; }

// ---- 初期化 -----------------------------------------------------------
async function init() {
  applyTheme(currentTheme());
  await loadConfig();
  bindEvents();
  await loadList();
  pollScanIfRunning();
  pollSyncIfRunning();
  if (location.hash === "#sync") openSyncModal();  // 同期画面への直リンク
}

async function loadConfig() {
  CFG = await api("/api/config");
  const cat = $("#f-category");
  cat.length = 1;
  CFG.categories.forEach((c) => cat.add(new Option(c, c)));
  const fol = $("#f-folder");
  fol.length = 1;
  // 親フォルダの直下にサブフォルダが来るよう、パスを階層順にソートして並べる
  const allFolders = Array.from(new Set([...(CFG.top_folders || []), ...(CFG.folders || [])]));
  allFolders.sort(folderCmp);
  allFolders.forEach((f) => {
    const depth = f.split("/").length - 1;
    const label = "　".repeat(depth) + f;   // 全角スペースで階層をインデント(ラベルはフルパス)
    fol.add(new Option(label, f));
  });
  renderStats();
}

function renderStats() {
  const c = CFG.counts || {};
  const last = CFG.last_scan_at ? `最終スキャン ${CFG.last_scan_at}` : "未スキャン";
  const st = $("#stats");
  st.textContent = `全${c.total || 0}件 / NEW ${c.new || 0} / 要対応 ${c.attention || 0} ・ ${last}`;
  st.title = "NEW=前回のスキャンで新しく見つかったもの / "
    + "要対応=ファイルが見つからない(欠落)か、開けなかった(読込不可)もの。"
    + "状態の絞り込みで一覧できます。";
}

function bindEvents() {
  $("#search").addEventListener("input", () => { clearTimeout(SEARCH_TIMER); SEARCH_TIMER = setTimeout(() => { PAGE = 1; loadList(); }, 300); });
  ["#f-category", "#f-status", "#f-folder", "#f-read", "#sort"].forEach((s) =>
    $(s).addEventListener("change", () => { PAGE = 1; loadList(); }));
  $("#f-fav").addEventListener("change", () => { PAGE = 1; loadList(); });
  $("#btn-scan").addEventListener("click", startScan);
  $("#btn-extract").addEventListener("click", startExtract);
  $("#btn-clearnew").addEventListener("click", () => { closeMaint(); clearFilteredNew(); });
  $("#btn-delmissing").addEventListener("click", () => { closeMaint(); deleteFilteredMissing(); });
  $("#btn-backup").addEventListener("click", () => { closeMaint(); doBackup(); });
  $("#btn-sync").addEventListener("click", () => { closeMaint(); openSyncModal(); });
  $("#btn-settings").addEventListener("click", () => { closeMaint(); openSettings(); });
  $("#btn-maint").addEventListener("click", toggleMaint);
  $("#sync-close").addEventListener("click", () => $("#sync-modal").classList.add("hidden"));
  $("#sync-preview").addEventListener("click", () => startSync(false));
  $("#sync-apply").addEventListener("click", syncApply);
  $("#btn-theme").addEventListener("click", cycleTheme);
  $("#set-close").addEventListener("click", closeSettings);
  $("#set-cancel").addEventListener("click", closeSettings);
  $("#set-save").addEventListener("click", saveSettings);
  // メニューの外側をクリックしたら閉じる
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".menu")) closeMaint();
  });
  document.addEventListener("keydown", onKeyDown);
}

// ---- メンテナンスメニュー ---------------------------------------------
function toggleMaint(ev) {
  ev.stopPropagation();
  const panel = $("#maint-panel");
  const open = panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !open);
  $("#btn-maint").setAttribute("aria-expanded", open ? "true" : "false");
}
function closeMaint() {
  $("#maint-panel").classList.add("hidden");
  $("#btn-maint").setAttribute("aria-expanded", "false");
}

// ---- キーボード操作 ---------------------------------------------------
function isTyping(t) {
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT");
}
function onKeyDown(ev) {
  if (ev.key === "Escape") {
    if (!$("#set-modal").classList.contains("hidden")) { closeSettings(); return; }
    if (!$("#sync-modal").classList.contains("hidden")) { $("#sync-modal").classList.add("hidden"); return; }
    if (!$("#plan").classList.contains("hidden")) { hidePlan(); return; }
    // 詳細パネル。未保存があるときは捨てずに選ばせる(閉じるボタンと同じ扱い)
    if (!$("#detail").classList.contains("hidden") && DIRTY && DIRTY.close) { DIRTY.close(); return; }
    closeMaint();
    if (isTyping(ev.target)) ev.target.blur();
    return;
  }
  if (isTyping(ev.target) || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  const rows = Array.from(document.querySelectorAll("#rows tr"));
  if (ev.key === "/") { ev.preventDefault(); $("#search").focus(); $("#search").select(); return; }
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    if (!rows.length) return;
    ev.preventDefault();
    CURSOR = Math.min(rows.length - 1, Math.max(0, CURSOR + (ev.key === "ArrowDown" ? 1 : -1)));
    markCursor(rows);
    return;
  }
  if (ev.key === "Enter" && CURSOR >= 0 && rows[CURSOR]) {
    // ボタンやリンクにフォーカスがあるときは、そちらの決定を優先する
    if (ev.target.tagName === "BUTTON" || ev.target.tagName === "A") return;
    ev.preventDefault();
    openDetail(+rows[CURSOR].dataset.id);
  }
}
function markCursor(rows) {
  rows.forEach((tr, i) => tr.classList.toggle("cursor", i === CURSOR));
  if (rows[CURSOR]) rows[CURSOR].scrollIntoView({ block: "nearest" });
}

// ---- 一覧 -------------------------------------------------------------
function currentParams() {
  const p = new URLSearchParams();
  const q = $("#search").value.trim();
  if (q) p.set("q", q);
  const map = { category: "#f-category", status: "#f-status", folder: "#f-folder", read_status: "#f-read", sort: "#sort" };
  for (const [k, sel] of Object.entries(map)) { const v = $(sel).value; if (v) p.set(k, v); }
  if ($("#f-fav").checked) p.set("favorite", "1");
  p.set("page", PAGE); p.set("per_page", PER);
  return p;
}

function hasFilter() {
  const p = currentParams();
  return ["q", "category", "status", "folder", "read_status", "favorite"].some((k) => p.get(k));
}

async function loadList() {
  let data;
  try { data = await api("/api/files?" + currentParams().toString()); }
  catch (e) { toast("読み込み失敗: " + e.message); return; }
  const tb = $("#rows"); tb.innerHTML = "";
  data.items.forEach((it) => tb.appendChild(renderRow(it)));
  CURSOR = -1;
  renderEmpty(data.total);
  renderPager(data.total, data.page, data.per_page, data.dup_groups);
}

// 0件のとき: 初回起動(DBが空)なら次にすることを案内し、絞り込みの結果なら普通の空表示にする
function renderEmpty(total) {
  const box = $("#empty");
  box.innerHTML = "";
  if (total > 0) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const libraryEmpty = !(CFG.counts && CFG.counts.total) && !hasFilter();
  if (!libraryEmpty) { box.textContent = "該当する文献がありません"; return; }
  const g = el("div", "empty-guide");
  g.appendChild(el("h3", null, "ようこそ。まずはファイルを読み込みましょう"));
  const ol = el("ol");
  ol.appendChild(el("li", null, `このツールのフォルダ(_reftool)を置いた場所の親フォルダ「${CFG.root_name || ""}」の中身を読み取ります`));
  ol.appendChild(el("li", null, "PDF・ppt・pptx をその中に置いてください(フォルダ分けは自由です。ファイルは移動しません)"));
  ol.appendChild(el("li", null, "下の「再スキャン」を押すと一覧に並びます"));
  g.appendChild(ol);
  g.appendChild(el("div", "muted", "読み込んだあと「メタ抽出」を押すと、PDFからタイトルやDOIを自動で拾います。"));
  const btns = el("div", "row-btns");
  const b = el("button", "primary", "再スキャンする");
  b.onclick = startScan;
  btns.appendChild(b);
  g.appendChild(btns);
  box.appendChild(g);
}

function renderRow(it) {
  const tr = el("tr");
  tr.dataset.id = it.id;
  if (SELECTED === it.id) tr.classList.add("selected");

  const mark = el("td", "c-mark");
  if (it.is_new) mark.appendChild(el("span", "badge new", "NEW"));
  if (it.status === "missing") mark.appendChild(el("span", "badge warn", "欠落"));
  if (it.status === "unreadable") mark.appendChild(el("span", "badge warn", "不可"));
  tr.appendChild(mark);

  const fav = el("td", "c-fav");
  const star = el("span", "star" + (it.favorite ? " on" : ""), it.favorite ? "★" : "☆");
  star.addEventListener("click", async (ev) => { ev.stopPropagation(); await toggleFav(it, star); });
  fav.appendChild(star); tr.appendChild(fav);

  const title = el("td", "c-title");
  if (it.title_missing) {
    title.appendChild(el("div", "title-main untaken", "(タイトル未取得)"));
  } else {
    title.appendChild(el("div", "title-main", it.title));
  }
  const fileline = el("div", "title-file", it.filename);
  // 同じ中身が他の場所にもある印。狭い列に置くと崩れるので、幅のあるタイトル列に出す
  if (it.dup_count > 1) {
    const dup = el("span", "badge dup", `同一${it.dup_count}か所`);
    dup.title = "まったく同じ内容のファイルが、この件数の場所に置かれています";
    fileline.appendChild(document.createTextNode(" "));
    fileline.appendChild(dup);
  }
  title.appendChild(fileline);
  tr.appendChild(title);

  const cat = el("td", "c-cat");
  if (it.category) cat.appendChild(el("span", "chip", it.category));
  tr.appendChild(cat);

  tr.appendChild(el("td", "c-journal muted", it.journal || ""));
  tr.appendChild(el("td", "c-folder muted", it.folder || ""));
  tr.appendChild(el("td", "c-read muted", it.read_status || ""));

  tr.addEventListener("click", () => openDetail(it.id));
  return tr;
}

function renderPager(total, page, per, dupGroups) {
  const pages = Math.max(1, Math.ceil(total / per));
  const p = $("#pager"); p.innerHTML = "";
  const groups = dupGroups ? `  (${dupGroups}種類の中身)` : "";
  const info = el("span", "muted", `${total}件  ${page}/${pages}ページ${groups}`);
  const prev = el("button", null, "‹ 前へ"); prev.disabled = page <= 1;
  const next = el("button", null, "次へ ›"); next.disabled = page >= pages;
  prev.onclick = () => { PAGE = page - 1; loadList(); };
  next.onclick = () => { PAGE = page + 1; loadList(); };
  p.append(prev, next, info);
}

async function toggleFav(it, star) {
  const nv = it.favorite ? 0 : 1;
  try { await api(`/api/files/${it.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ favorite: nv }) }); }
  catch (e) { toast("失敗: " + e.message); return; }
  it.favorite = nv; star.textContent = nv ? "★" : "☆"; star.classList.toggle("on", !!nv);
}

// ---- 詳細パネル -------------------------------------------------------
// 書きかけを黙って捨てないよう、開き直す前に未保存の変更を確認する
async function openDetail(id) {
  if (DIRTY && DIRTY.id !== id && DIRTY.isDirty()) {
    const pending = DIRTY;
    showPlan({
      title: "未保存の変更があります",
      lines: [`「${pending.filename}」の編集内容がまだ保存されていません。`],
      actions: [
        { label: "保存してから移動", cls: "primary", run: async () => { await pending.save(); DIRTY = null; await openDetail(id); } },
        { label: "破棄して移動", run: async () => { DIRTY = null; await openDetail(id); } },
      ],
    });
    return;
  }
  SELECTED = id;
  document.querySelectorAll("#rows tr").forEach((tr) => tr.classList.toggle("selected", +tr.dataset.id === id));
  let d;
  try { d = await api(`/api/files/${id}`); } catch (e) { toast("失敗: " + e.message); return; }
  const p = $("#detail"); p.classList.remove("hidden"); p.innerHTML = "";
  const editors = [];  // 明示保存する編集フィールド群

  const closeNoSave = () => {
    DIRTY = null;
    p.classList.add("hidden"); SELECTED = null;
    document.querySelectorAll("#rows tr.selected").forEach((t) => t.classList.remove("selected"));
  };
  // 未保存があるときは、閉じる前に選ばせる
  const closeAsk = () => {
    if (!isDirty()) { closeNoSave(); return; }
    showPlan({
      title: "未保存の変更があります",
      lines: [`「${d.filename}」の編集内容がまだ保存されていません。`],
      actions: [
        { label: "保存して閉じる", cls: "primary", run: async () => { await saveAndClose(id, editors, closeNoSave); } },
        { label: "破棄して閉じる", run: () => closeNoSave() },
      ],
    });
  };
  const isDirty = () => editors.some((e) => e.input.value !== e.orig);

  // 並び順: ファイル名 → パス → NEW解除 → 正式名称 → カテゴリ
  // ファイル名(見出し)＋右上に「閉じる」を同じ行で
  const header = el("div", "detail-header");
  header.appendChild(el("h2", null, d.filename));
  const closeTop = el("button", "close", "✕ 閉じる");
  closeTop.onclick = closeAsk;
  header.appendChild(closeTop);
  p.appendChild(header);

  // パス
  p.appendChild(el("div", "path path-above-title", d.rel_path));

  // NEW解除 / 状態
  const badges = el("div", "field");
  if (d.is_new) { const b = el("button", null, "NEWを解除"); b.onclick = async () => { await api(`/api/files/${id}/clear-new`, { method: "POST" }); toast("NEW解除"); refreshAfter(); }; badges.appendChild(b); }
  if (d.status === "missing") {
    badges.appendChild(el("span", "badge warn", "要対応: ファイルが見つかりません"));
    const del = el("button", "danger", "この欠落を削除…");
    del.onclick = () => {
      showPlan({
        title: "欠落レコードを1件削除します",
        lines: [
          `対象: ${d.filename}`,
          "この記録に付けた正式名称・メモ・カテゴリも一緒に消えます(元に戻せません)。",
          "ファイルを移動しただけの場合は、削除ではなく「移動された可能性(再紐付け)」を使ってください。",
        ],
        actions: [{
          label: "削除する", cls: "danger",
          run: async () => {
            await api(`/api/files/${id}/delete-missing`, { method: "POST" });
            toast("欠落レコードを削除しました"); closeNoSave(); await refreshAfter();
          },
        }],
      });
    };
    badges.appendChild(del);
  }
  if (d.status === "unreadable") badges.appendChild(el("span", "badge warn", "要対応: 読込不可"));
  if (badges.children.length) p.appendChild(badges);

  // 正式名称
  p.appendChild(editField("正式文献名(タイトル)", "title_user", d.title_user, d.title_auto, "input", editors));
  // カテゴリ
  p.appendChild(categoryField(d, editors));
  // 読書ステータス
  p.appendChild(readField(d, editors));

  // 開く / パスコピー
  const openBtns = el("div", "row-btns");
  if (d.ext === "pdf") {
    const a = el("a"); a.href = `/api/files/${id}/open`; a.target = "_blank";
    const ob = el("button", "primary", "PDFを開く"); a.appendChild(ob); openBtns.appendChild(a);
  } else {
    const a = el("a"); a.href = `/api/files/${id}/open?download=1`;
    const ob = el("button", "primary", "ダウンロード"); a.appendChild(ob); openBtns.appendChild(a);
  }
  const copyPath = el("button", null, "パスをコピー");
  copyPath.onclick = () => { navigator.clipboard.writeText(d.rel_path); toast("パスをコピーしました"); };
  openBtns.appendChild(copyPath);
  p.appendChild(openBtns);

  // メモ → 雑誌名・DOI → 目次 の順
  p.appendChild(editField(CFG.memo1_label, "memo1", d.memo1, null, "textarea", editors));
  p.appendChild(editField("雑誌名", "journal_user", d.journal_user, d.journal_auto, "input", editors));
  p.appendChild(editField("DOI", "doi_user", d.doi_user, d.doi_auto, "input", editors));
  p.appendChild(editField("URL", "url_user", d.url_user, null, "input", editors));
  p.appendChild(editField(CFG.memo2_label, "memo2", d.memo2, null, "textarea", editors));

  // メモ欄の下に保存/閉じるボタン(＋未保存の表示)
  const botBtns = el("div", "detail-actions");
  const saveClose = el("button", "primary", "保存して閉じる");
  saveClose.onclick = () => { saveAndClose(id, editors, closeNoSave).catch(() => {}); };  // 失敗時は閉じない
  const closeNoSaveBtn = el("button", null, "保存せずに閉じる");
  closeNoSaveBtn.onclick = closeNoSave;
  const dirtyMark = el("span", "dirty-mark hidden", "未保存の変更があります");
  botBtns.append(saveClose, closeNoSaveBtn, dirtyMark);
  p.appendChild(botBtns);

  const refreshDirty = () => dirtyMark.classList.toggle("hidden", !isDirty());
  editors.forEach((e) => { e.input.addEventListener("input", refreshDirty); e.input.addEventListener("change", refreshDirty); });
  DIRTY = { id, filename: d.filename, isDirty, close: closeAsk,
            save: () => saveAndClose(id, editors, () => {}) };

  // Crossref / 引用
  const tools = el("div", "row-btns");
  const cr = el("button", null, "Crossref補完");
  cr.onclick = async () => { try { const r = await api(`/api/files/${id}/crossref`, { method: "POST" }); toast(r.ok ? "Crossref補完しました" : r.reason); if (r.ok) openDetail(id); } catch (e) { toast(e.message); } };
  tools.appendChild(cr);
  const bib = el("button", null, "BibTeXコピー");
  bib.onclick = () => copyCite(id, "bibtex");
  const plain = el("button", null, "引用(プレーン)コピー");
  plain.onclick = () => copyCite(id, "plain");
  tools.append(bib, plain);
  p.appendChild(el("div", "section-t", "引用・補完"));
  p.appendChild(tools);

  // 移動候補
  if (d.move_candidates && d.move_candidates.length) {
    p.appendChild(el("div", "section-t", "移動された可能性(再紐付け)"));
    d.move_candidates.forEach((c) => {
      const row = el("div", "cand"); row.appendChild(el("span", null, c.rel_path));
      const b = el("button", "primary", "ここに紐付け");
      b.onclick = async () => { await api(`/api/files/${id}/relink`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_id: c.id }) }); toast("再紐付けしました"); DIRTY = null; openDetail(c.id); refreshAfter(); };
      row.appendChild(b); p.appendChild(row);
    });
  }

  // 重複
  if (d.duplicates && d.duplicates.length) {
    p.appendChild(el("div", "section-t", `重複(同一内容 ${d.duplicates.length}件)`));
    d.duplicates.forEach((dp) => {
      const row = el("div", "dup"); row.appendChild(el("span", null, dp.rel_path));
      const b = el("button", null, "開く"); b.onclick = () => openDetail(dp.id); row.appendChild(b);
      p.appendChild(row);
    });
  }
}

function editField(label, key, userVal, autoVal, kind, editors) {
  const f = el("div", "field");
  f.appendChild(el("label", null, label));
  const input = kind === "textarea" ? el("textarea") : el("input");
  input.value = userVal || "";
  if (autoVal && !userVal) input.placeholder = "自動抽出値: " + autoVal;
  f.appendChild(input);
  if (autoVal) {
    const hint = el("div", "auto-hint", (userVal ? "自動抽出値: " : "自動抽出値を採用中: ") + autoVal + (userVal ? "(手動値で上書き中)" : ""));
    f.appendChild(hint);
  } else if (kind === "input" && !userVal) {
    f.appendChild(el("div", "auto-hint untaken", "未取得"));
  }
  editors.push({ key, input, orig: userVal || "" });
  return f;
}

function categoryField(d, editors) {
  const f = el("div", "field");
  f.appendChild(el("label", null, "カテゴリ"));
  const sel = el("select");
  sel.add(new Option("(自動: " + (d.category_auto || "―") + ")", ""));
  CFG.categories.forEach((c) => sel.add(new Option(c, c)));
  // 設定から外されたカテゴリが手動指定されている場合も、選択肢として残す(勝手に変えない)
  if (d.category_user && !CFG.categories.includes(d.category_user)) {
    sel.add(new Option(d.category_user + "(設定の一覧にありません)", d.category_user));
  }
  sel.value = d.category_user || "";
  f.appendChild(sel);
  f.appendChild(el("div", "auto-hint", "空=フォルダ名から自動判定。選ぶと手動固定(再スキャンで上書きされません)。"));
  editors.push({ key: "category_user", input: sel, orig: d.category_user || "" });
  return f;
}

function readField(d, editors) {
  const f = el("div", "field");
  f.appendChild(el("label", null, "読書ステータス"));
  const sel = el("select");
  CFG.read_statuses.forEach((s) => sel.add(new Option(s, s)));
  sel.value = d.read_status || "未読";
  f.appendChild(sel);
  editors.push({ key: "read_status", input: sel, orig: d.read_status || "未読" });
  return f;
}

// 編集フィールドを一括保存してから閉じる。変更が無ければ保存せず閉じる。保存失敗時は閉じない。
async function saveAndClose(id, editors, closeFn) {
  const body = {};
  let changed = false;
  for (const e of editors) { const v = e.input.value; if (v !== e.orig) { body[e.key] = v; changed = true; } }
  if (!changed) { toast("変更はありません"); DIRTY = null; closeFn(); return; }
  try { await api(`/api/files/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { toast("保存失敗: " + e.message); throw e; }
  editors.forEach((e) => { e.orig = e.input.value; });   // 保存済みを基準に更新
  toast("保存しました (" + Object.keys(body).length + "項目)");
  DIRTY = null;
  closeFn();
  await refreshAfter();
}

async function copyCite(id, fmt) {
  try { const r = await api(`/api/files/${id}/cite?fmt=${fmt}`); await navigator.clipboard.writeText(r.text); toast((fmt === "bibtex" ? "BibTeX" : "引用") + "をコピーしました"); }
  catch (e) { toast("失敗: " + e.message); }
}

async function refreshAfter() { CFG = await api("/api/config"); renderStats(); await loadList(); }

// ---- スキャン / 抽出 --------------------------------------------------
async function startScan() {
  try { const r = await api("/api/scan", { method: "POST" }); if (!r.ok) { toast(r.reason); return; } }
  catch (e) { toast(e.message); return; }
  toast("スキャン開始"); pollScan();
}

function pollScanIfRunning() { fetch("/api/scan/status").then(r => r.json()).then(s => { if (s.running) pollScan(); }); }

async function pollScan() {
  const prog = $("#progress"); prog.classList.remove("hidden");
  const tick = async () => {
    let s; try { s = await api("/api/scan/status"); } catch { return; }
    if (s.running) {
      prog.textContent = `スキャン中… ${s.phase} ${s.processed}/${s.total}`;
      setTimeout(tick, 700);
    } else {
      if (s.phase === "error") prog.textContent = "スキャンエラー: " + s.error;
      else prog.textContent = `スキャン完了: 追加${s.added} / 欠落${s.missing} / 移動候補${s.moved_candidates}`;
      setTimeout(() => prog.classList.add("hidden"), 6000);
      await refreshAfter();
      pollExtractIfRunning();
    }
  };
  tick();
}

async function startExtract() {
  try { const r = await api("/api/extract", { method: "POST" }); if (!r.ok) { toast(r.reason); return; } }
  catch (e) { toast(e.message); return; }
  toast("メタ抽出開始"); pollExtract();
}
function pollExtractIfRunning() { fetch("/api/extract/status").then(r => r.json()).then(s => { if (s.running) pollExtract(); }); }
async function pollExtract() {
  const prog = $("#progress"); prog.classList.remove("hidden");
  const tick = async () => {
    let s; try { s = await api("/api/extract/status"); } catch { return; }
    if (s.running) { prog.textContent = `メタ抽出中… ${s.processed}/${s.total}`; setTimeout(tick, 800); }
    else { prog.textContent = `メタ抽出完了`; setTimeout(() => prog.classList.add("hidden"), 4000); await refreshAfter(); }
  };
  tick();
}

function filterParams() {
  const p = currentParams();
  p.delete("page"); p.delete("per_page"); p.delete("sort");
  return p;
}

async function clearFilteredNew() {
  const r = await api("/api/clear-new?" + filterParams().toString(), { method: "POST" });
  toast(r.count > 0 ? `NEWを${r.count}件解除しました` : "対象のNEWはありませんでした");
  refreshAfter();
}

// 何件消えるかを先に数えてから実行させる(取り消せない操作)
async function deleteFilteredMissing() {
  const count = await countMissingInFilter();
  if (count === null) return;
  if (count === 0) { toast("対象の欠落レコードはありません"); return; }
  showPlan({
    title: `欠落レコードを ${count}件 削除します`,
    lines: [
      hasFilter() ? "対象は、いま絞り込んでいる条件に一致する『欠落』だけです。" : "対象は『欠落』の全件です(絞り込みなし)。",
      "付けた正式名称・メモ・カテゴリも一緒に消えます(元に戻せません)。ファイル自体は既に存在しません。",
      "移動しただけのファイルは、削除せず詳細画面の「再紐付け」で引き継げます。",
    ],
    actions: [{
      label: `${count}件を削除する`, cls: "danger",
      run: async () => {
        const r = await api("/api/delete-missing?" + filterParams().toString(), { method: "POST" });
        toast(`欠落レコードを${r.count}件削除しました`);
        await refreshAfter();
      },
    }],
  });
}

// 削除APIと同じ条件(絞り込み ＋ status=missing)で件数だけ取得する
async function countMissingInFilter() {
  const p = filterParams();
  p.set("status", "missing");
  p.set("page", "1"); p.set("per_page", "1");
  try { return (await api("/api/files?" + p.toString())).total; }
  catch (e) { toast("件数を取得できませんでした: " + e.message); return null; }
}

async function doBackup() {
  try { const r = await api("/api/backup", { method: "POST" }); toast("バックアップ作成: " + r.file); }
  catch (e) { toast("失敗: " + e.message); }
}

// ---- 設定 -------------------------------------------------------------
let SET = null;

async function openSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { toast("失敗: " + e.message); return; }
  SET = s;
  const body = $("#set-body");
  body.innerHTML = "";

  const group = (title) => { const g = el("div", "set-group"); g.appendChild(el("h3", null, title)); body.appendChild(g); return g; };
  const row = (g, label, node, hint) => {
    const r = el("div", "set-row");
    r.appendChild(el("label", null, label));
    const v = el("div", "set-val");
    v.appendChild(node);
    if (hint) v.appendChild(el("div", "auto-hint", hint));
    r.appendChild(v); g.appendChild(r); return node;
  };

  // 表示
  const g0 = group("表示");
  const theme = el("select"); theme.id = "set-theme";
  THEMES.forEach((t) => theme.add(new Option(THEME_NAME[t], t)));
  theme.value = currentTheme();
  theme.onchange = () => setTheme(theme.value);
  row(g0, "テーマ", theme, "この端末のブラウザに保存されます。");

  // カテゴリ
  const g1 = group("カテゴリ");
  const cats = el("textarea"); cats.id = "set-cats";
  cats.value = s.editable.categories.join("\n");
  cats.style.minHeight = "150px";
  const usage = Object.entries(s.category_usage || {});
  row(g1, "一覧(1行に1つ)", cats,
    "上から順にプルダウンへ出ます。" +
    (usage.length ? " 手動で指定されている数: " + usage.map(([k, v]) => `${k} ${v}件`).join(" / ") : ""));
  const def = el("select"); def.id = "set-default";
  row(g1, "自動判定で決まらないとき", def, "フォルダ名から判定できなかったファイルに付くカテゴリです。");
  const syncDefault = () => {
    const list = cats.value.split("\n").map((x) => x.trim()).filter(Boolean);
    const keep = def.value;
    def.length = 0;
    list.forEach((c) => def.add(new Option(c, c)));
    def.value = list.includes(keep) ? keep : (list.includes(s.editable.default_category) ? s.editable.default_category : (list[0] || ""));
  };
  cats.addEventListener("input", syncDefault);
  syncDefault();
  def.value = s.editable.default_category;

  // メモ欄
  const g2 = group("メモ欄の名前");
  const m1 = el("input"); m1.id = "set-memo1"; m1.value = s.editable.memo1_label;
  const m2 = el("input"); m2.id = "set-memo2"; m2.value = s.editable.memo2_label;
  row(g2, "1つ目", m1, "詳細画面に出る見出しです(既定「メモ」)。");
  row(g2, "2つ目", m2, "既定「目次」。長い文章を貼る欄として使えます。");

  // バックアップ
  const g3 = group("バックアップ");
  const bEn = el("input"); bEn.type = "checkbox"; bEn.id = "set-bkenabled"; bEn.checked = s.editable.backup_enabled;
  const bl = el("label", "chk"); bl.appendChild(bEn); bl.appendChild(document.createTextNode(" 起動時に1日1回、自動で控えを取る"));
  row(g3, "自動バックアップ", bl, "手動の作成はいつでも「メンテナンス」からできます。");
  const keep = el("input"); keep.type = "number"; keep.id = "set-keep";
  keep.min = s.limits.keep_range[0]; keep.max = s.limits.keep_range[1]; keep.value = s.editable.backup_keep;
  row(g3, "残す世代数", keep, `古いものから消えます(${s.limits.keep_range[0]}〜${s.limits.keep_range[1]})。`);

  // Crossref
  const g4 = group("DOIからの情報取得(Crossref)");
  const cEn = el("input"); cEn.type = "checkbox"; cEn.id = "set-crenabled"; cEn.checked = s.editable.crossref_enabled;
  const cl = el("label", "chk"); cl.appendChild(cEn); cl.appendChild(document.createTextNode(" DOIが取れたら正式な書名・雑誌名を問い合わせる"));
  row(g4, "利用する", cl, "インターネットに接続できないときは自動で見送られます。");
  const mail = el("input"); mail.id = "set-mailto"; mail.value = s.editable.crossref_mailto; mail.placeholder = "(空欄でも使えます)";
  row(g4, "連絡先メール", mail, "Crossrefに任意で伝えるものです。書くと優先的に応答してもらえます。");

  // 変更できない設定
  const g5 = group("この画面からは変えられない設定");
  const ro = s.readonly;
  const info = el("div");
  info.appendChild(el("div", "set-readonly", `待ち受け ${ro.host}:${ro.port}`));
  info.appendChild(el("div", "auto-hint", `読み取る拡張子: ${ro.extensions.join(", ")} ／ タイトル抽出で読む先頭ページ数: ${ro.metadata_pages} ／ 読み取り対象フォルダ: ${ro.root_name}`));
  info.appendChild(el("div", "auto-hint", "これらは設定ファイル(config.toml)を直接書き換えてから、アプリを起動し直してください。間違えると起動できなくなるため、この画面には出していません。"));
  const r5 = el("div", "set-row"); r5.appendChild(el("label", null, "接続とスキャン")); const v5 = el("div", "set-val"); v5.appendChild(info); r5.appendChild(v5); g5.appendChild(r5);

  $("#set-modal").classList.remove("hidden");
}

function closeSettings() { $("#set-modal").classList.add("hidden"); }

async function saveSettings() {
  const body = {
    categories: $("#set-cats").value.split("\n").map((x) => x.trim()).filter(Boolean),
    default_category: $("#set-default").value,
    memo1_label: $("#set-memo1").value,
    memo2_label: $("#set-memo2").value,
    backup_enabled: $("#set-bkenabled").checked,
    backup_keep: $("#set-keep").value,
    crossref_enabled: $("#set-crenabled").checked,
    crossref_mailto: $("#set-mailto").value,
  };
  let r;
  try { r = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { toast("保存できません: " + e.message); return; }
  closeSettings();
  toast("設定を保存しました(控え: " + r.backup + ")");
  if (r.orphans && r.orphans.length) {
    showPlan({
      title: "一覧から外したカテゴリが、まだ使われています",
      lines: [
        "次のカテゴリは手動指定として残っています: " + r.orphans.join(" / "),
        "ファイルの分類は変更していません。付け替えるには、そのファイルの詳細画面でカテゴリを選び直してください。",
      ],
      actions: [],
    });
  }
  await refreshAfter();
}

// ---- 他端末同期 -------------------------------------------------------
async function openSyncModal() {
  const modal = $("#sync-modal"); modal.classList.remove("hidden");
  $("#sync-plan").innerHTML = "";
  $("#sync-apply").disabled = true;
  const box = $("#sync-backups"); box.textContent = "読み込み中…";
  let r;
  try { r = await api("/api/sync/backups"); } catch (e) { box.textContent = "失敗: " + e.message; return; }
  box.innerHTML = "";
  if (!r.backups.length) {
    box.appendChild(el("div", "muted", "backups フォルダにバックアップDB(.db)がありません。メイン端末の _reftool/backups/ から最新のファイルをこの端末の同じ場所へコピーしてください。"));
    return;
  }
  r.backups.forEach((b, i) => {
    const lab = el("label", "sync-item");
    const rad = document.createElement("input");
    rad.type = "radio"; rad.name = "sync-backup"; rad.value = b.name; if (i === 0) rad.checked = true;
    rad.addEventListener("change", () => { $("#sync-apply").disabled = true; $("#sync-plan").innerHTML = ""; });
    lab.appendChild(rad);
    lab.appendChild(el("span", null, b.name));
    lab.appendChild(el("span", "muted", `${(b.size / 1024 / 1024).toFixed(1)}MB / ${b.mtime.replace("T", " ")}`));
    box.appendChild(lab);
  });
}

function selectedBackup() {
  const r = document.querySelector('input[name="sync-backup"]:checked');
  return r ? r.value : null;
}

async function syncApply() {
  const name = selectedBackup(); if (!name) { toast("バックアップを選んでください"); return; }
  showPlan({
    title: "この端末の配置を、記録に合わせて並べ替えます",
    lines: [
      `使う記録: ${name}`,
      "いまのデータベースは、置き換える前に控えを取ります。",
      "ファイルは記録どおりの場所へ移動します(中身は変わりません)。",
      "上のプレビューに出ている「移動予定」がそのまま実行されます。",
    ],
    actions: [{ label: "同期を実行する", cls: "primary", run: async () => { await startSync(true); } }],
  });
}

async function startSync(apply) {
  const name = selectedBackup(); if (!name) { toast("バックアップを選んでください"); return; }
  try {
    const r = await api(`/api/sync/${apply ? "apply" : "preview"}?backup=${encodeURIComponent(name)}`, { method: "POST" });
    if (!r.ok) { toast(r.reason); return; }
  } catch (e) { toast("失敗: " + e.message); return; }
  pollSync();
}

function pollSyncIfRunning() {
  fetch("/api/sync/status").then(r => r.json())
    .then(s => { if (s.running) { $("#sync-modal").classList.remove("hidden"); pollSync(); } })
    .catch(() => {});
}

const SYNC_PHASES = {
  reading: "バックアップDB読込中", inventory: "ローカルファイル照合中", planning: "配置計画作成中",
  backup: "現在のDBを退避中", "db-import": "バックアップDBを取り込み中", moving: "ファイル移動中", scanning: "再スキャン中",
};

async function pollSync() {
  const pane = $("#sync-plan"); pane.textContent = "処理中…";
  const tick = async () => {
    let s; try { s = await api("/api/sync/status"); } catch { return; }
    if (s.running) {
      const p = SYNC_PHASES[s.phase] || s.phase;
      pane.textContent = `${p}…` + (s.total ? ` ${s.processed}/${s.total}` : "");
      setTimeout(tick, 700); return;
    }
    if (s.phase === "error") { pane.textContent = "エラー: " + s.error; return; }
    renderSyncResult(s);
    if (s.mode === "apply") { toast("同期が完了しました"); await refreshAfter(); }
  };
  tick();
}

function renderSyncResult(s) {
  const pane = $("#sync-plan"); pane.innerHTML = "";
  const plan = s.plan;
  if (!plan) { pane.textContent = ""; return; }
  const list = (title, arr, total, fmt) => {
    if (!arr || !arr.length) return;
    pane.appendChild(el("div", "section-t", `${title}(${total != null ? total : arr.length}件)`));
    const box = el("div", "sync-list");
    arr.slice(0, 200).forEach((x) => box.appendChild(el("div", null, fmt(x))));
    const shown = Math.min(arr.length, 200);
    if ((total != null ? total : arr.length) > shown) box.appendChild(el("div", "muted", `…ほか${(total != null ? total : arr.length) - shown}件`));
    pane.appendChild(box);
  };
  if (s.mode === "apply" && s.result) {
    const r = s.result;
    pane.appendChild(el("div", "sync-summary",
      `✅ 同期完了: メタ情報${r.imported}件を取り込み / ファイル移動${r.moved}件 / 空フォルダ削除${r.pruned_dirs}件`));
    if (plan.missing_total) pane.appendChild(el("div", "muted", `不足${plan.missing_total}件は「欠落」として登録されています。メイン端末からファイルをコピーして再スキャンしてください。`));
    list("移動エラー", r.move_errors, null, (x) => `${x.path}: ${x.error}`);
  } else {
    pane.appendChild(el("div", "sync-summary",
      `記録${plan.backup_total}件: 配置一致 ${plan.ok} / 移動予定 ${plan.moves_total} / 不足 ${plan.missing_total} / この端末のみ ${plan.extra_total}` +
      (plan.conflicts_total ? ` / 競合 ${plan.conflicts_total}` : "")));
    $("#sync-apply").disabled = false;
  }
  list("移動予定", plan.moves, plan.moves_total, (m) => `${m.src} → ${m.dst}`);
  list("不足(メイン端末からコピーが必要)", plan.missing, plan.missing_total, (x) => x);
  list("この端末にのみあるファイル(そのまま残ります)", plan.extra, plan.extra_total, (x) => x);
  list("競合(移動先に記録外のファイルがあるためスキップ)", plan.conflicts, plan.conflicts_total, (m) => `${m.src} → ${m.dst}`);
  if (plan.bad_paths && plan.bad_paths.length) list("不正なパス(無視)", plan.bad_paths, null, (x) => x);
}

init();

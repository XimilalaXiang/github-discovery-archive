/* =========================================================================
   GitHub Discovery Archive — vanilla JS app (no framework).
   ========================================================================= */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const DATA_URL = '/data/projects.json';

// Full text of truncated day summaries, keyed by element id.
let summaryStore = {};

/* ------------------------------------------------------------------ state */

const state = {
  data: null,
  query: '',
  tags: new Set(),
  languages: new Set(),
  flags: new Set(), // 'multi' | 'archived'
  view: 'timeline',
  sort: 'date-desc',
  filtersOpen: false,
};

/* ------------------------------------------------------------------ helpers */

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return '—';
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}

function fmtFullNum(n) {
  if (n == null || Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US');
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = iso.slice(0, 10);
  return d;
}

function langColor(p) {
  const c = p.github && p.github.language_color;
  return c || '#7d838d';
}

/* ------------------------------------------------------------------ data */

async function loadData() {
  setStatus('loading');
  const res = await fetch(DATA_URL, { cache: 'no-cache' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const json = await res.json();
  if (!json || !Array.isArray(json.projects)) throw new Error('数据集格式无效');
  return json;
}

/* ------------------------------------------------------------------ filtering */

function matchesQuery(p, q) {
  if (!q) return true;
  const hay = [
    p.full_name,
    p.name,
    p.owner,
    p.github && p.github.description,
    (p.github && p.github.topics || []).join(' '),
    (p.reason_tags || []).join(' '),
    (p.github && p.github.language) || '',
  ]
    .join(' ')
    .toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => hay.includes(term));
}

function getFiltered() {
  const q = state.query.trim();
  return state.data.projects.filter((p) => {
    if (!matchesQuery(p, q)) return false;
    if (state.tags.size && !p.reason_tags.some((t) => state.tags.has(t))) return false;
    if (state.languages.size) {
      const lang = (p.github && p.github.language) || '';
      if (!state.languages.has(lang)) return false;
    }
    if (state.flags.has('multi') && p.recommendation_count < 2) return false;
    if (state.flags.has('archived') && !(p.github && p.github.archived)) return false;
    return true;
  });
}

/* ------------------------------------------------------------------ sorting */

function sortProjects(list, mode) {
  const arr = list.slice();
  const stars = (p) => (p.github && typeof p.github.stars === 'number' ? p.github.stars : -1);
  switch (mode) {
    case 'stars-desc':
      arr.sort((a, b) => stars(b) - stars(a) || a.full_name.localeCompare(b.full_name));
      break;
    case 'name-asc':
      arr.sort((a, b) => a.full_name.localeCompare(b.full_name));
      break;
    case 'recent-desc':
      arr.sort((a, b) => {
        const av = (a.github && a.github.pushed_at) || '';
        const bv = (b.github && b.github.pushed_at) || '';
        return bv.localeCompare(av);
      });
      break;
    case 'recs-desc':
      arr.sort(
        (a, b) => b.recommendation_count - a.recommendation_count || stars(b) - stars(a)
      );
      break;
    case 'date-desc':
    default:
      arr.sort((a, b) => {
        const av = a.last_recommended || '';
        const bv = b.last_recommended || '';
        if (av !== bv) return bv.localeCompare(av);
        return stars(b) - stars(a);
      });
  }
  return arr;
}

/* ------------------------------------------------------------------ cards */

function cardHtml(p) {
  const gh = p.github || {};
  const stars = typeof gh.stars === 'number' ? `<span class="card-stars">★ ${fmtNum(gh.stars)}</span>` : '';
  const desc = gh.description
    ? `<p class="card-desc">${escapeHtml(gh.description)}</p>`
    : `<p class="card-desc is-empty">暂无描述</p>`;
  const lang = gh.language
    ? `<span class="lang-dot"><i style="background:${escapeHtml(langColor(p))}"></i>${escapeHtml(gh.language)}</span>`
    : '';
  const tags = (p.reason_tags || [])
    .slice(0, 3)
    .map((t) => `<span class="tag-mini">${escapeHtml(t)}</span>`)
    .join('');
  const flags = [];
  if (p.recommendation_count > 1) flags.push(`<span class="pill-flag mult">推荐 ${p.recommendation_count} 次</span>`);
  if (gh.archived) flags.push('<span class="pill-flag archived">已归档</span>');
  if (gh.status === 'not_found') flags.push('<span class="pill-flag notfound">已失效</span>');
  const owner = p.full_name.split('/')[0];
  const shortName = p.name;

  return `<button class="card" data-full-name="${escapeHtml(p.full_name)}" type="button"
      aria-label="查看 ${escapeHtml(p.full_name)} 详情">
    <div class="card-top">
      <span class="card-name"><span class="owner">${escapeHtml(owner)}/</span>${escapeHtml(shortName)}</span>
      ${stars}
    </div>
    ${desc}
    <div class="card-meta">
      ${lang}
      ${tags}
      ${flags.join('')}
    </div>
  </button>`;
}

/* ------------------------------------------------------------------ views */

function render() {
  if (!state.data) return;
  const filtered = getFiltered();
  updateActiveFilters(filtered.length);
  updateHeaderStats();

  if (state.view === 'timeline') renderTimeline(filtered);
  else if (state.view === 'gallery') renderGallery(filtered);
  else renderStats(state.data);
}

function renderTimeline(filtered) {
  const root = $('#timeline-root');
  const byDate = new Map();
  for (const p of filtered) {
    for (const d of p.recommendation_dates) {
      if (!byDate.has(d)) byDate.set(d, []);
      byDate.get(d).push(p);
    }
  }
  const dates = Array.from(byDate.keys()).sort().reverse();

  if (!dates.length) {
    root.innerHTML = emptyState();
    return;
  }

  const latestDate = dates[0];
  const latestProjects = sortProjects(dedupeList(byDate.get(latestDate)), 'stars-desc');

  let html = `<div class="latest-band">
    <div class="latest-head">
      <h2>最新一期 · 每日精选</h2>
      <span class="date">${escapeHtml(latestDate)}</span>
    </div>
    <div class="latest-rail" role="list">${latestProjects.map(cardHtml).join('')}</div>
  </div>`;

  html += `<div class="section-title"><span>完整时间线</span><span class="count">共 ${dates.length} 天</span></div>`;

  summaryStore = {};
  const LIMIT = 150;
  const dayHtml = dates
    .map((date, dayIdx) => {
      const items = sortProjects(dedupeList(byDate.get(date)), state.sort);
      const summary = daySummary(date);
      let summaryHtml = '';
      if (summary) {
        const id = `summary-${dayIdx}`;
        const long = summary.length > LIMIT;
        if (long) summaryStore[id] = summary;
        const shown = long ? summary.slice(0, LIMIT).trimEnd() + '…' : summary;
        summaryHtml =
          `<p class="day-summary" id="${id}">${escapeHtml(shown)}</p>` +
          (long
            ? `<button class="day-summary-toggle" type="button" aria-expanded="false" aria-controls="${id}" data-toggle-summary="${id}">展开全文</button>`
            : '');
      }
      return `<div class="timeline-day">
        <div class="day-head">
          <span class="day-date">${escapeHtml(date)}</span>
          <span class="day-count">${items.length} 个项目</span>
        </div>
        ${summaryHtml}
        <div class="day-grid">${items.map(cardHtml).join('')}</div>
      </div>`;
    })
    .join('');

  root.innerHTML = html + dayHtml;
}

function dedupeList(list) {
  const seen = new Set();
  const out = [];
  for (const p of list) {
    if (seen.has(p.full_name)) continue;
    seen.add(p.full_name);
    out.push(p);
  }
  return out;
}

function daySummary(date) {
  const day = (state.data.days || []).find((d) => d.date === date);
  return day && day.preference_summary ? day.preference_summary : '';
}

function renderGallery(filtered) {
  const root = $('#gallery-root');
  if (!filtered.length) {
    root.innerHTML = emptyState();
    return;
  }
  const sorted = sortProjects(filtered, state.sort);
  root.innerHTML =
    `<div class="section-title" style="grid-column:1/-1"><span>全部项目</span><span class="count">${sorted.length} 个</span></div>` +
    sorted.map(cardHtml).join('');
}

function renderStats(data) {
  const root = $('#stats-root');
  const s = data.stats || {};
  const topStars = sortProjects(data.projects, 'stars-desc').filter((p) => p.github && typeof p.github.stars === 'number').slice(0, 10);
  const topRecs = sortProjects(data.projects, 'recs-desc').filter((p) => p.recommendation_count > 1).slice(0, 8);
  const langs = collapseTail((s.languages || []).slice(0, 14), 3);
  const tags = (s.tags || []).slice(0, 14);

  root.innerHTML = `
    <div class="stat-cards">
      <div class="stat-card"><div class="v">${fmtFullNum(s.unique_projects)}</div><div class="k">去重项目 · 来自 ${fmtFullNum(s.total_records)} 条推荐</div></div>
      <div class="stat-card"><div class="v">${fmtFullNum(s.total_days)}</div><div class="k">推荐天数 · 平均 ${s.total_days ? (s.total_records / s.total_days).toFixed(1) : '—'}/天</div></div>
      <div class="stat-card"><div class="v">${fmtNum(s.stars_total)}</div><div class="k">星标合计 · ${fmtFullNum(s.stars_known)} 个已知</div></div>
      <div class="stat-card"><div class="v">${s.enriched_total ? Math.round((s.enriched_ok / s.enriched_total) * 100) : 0}%</div><div class="k">元数据覆盖 · ${fmtFullNum(s.enriched_ok)}/${fmtFullNum(s.enriched_total)}</div></div>
    </div>
    <div class="panel-grid">
      <div class="panel">
        <h3>语言分布</h3>
        ${barList(langs)}
      </div>
      <div class="panel">
        <h3>热门推荐标签</h3>
        ${barList(tags)}
      </div>
      <div class="panel panel-wide">
        <h3>推荐活跃度（按天）</h3>
        ${heatmap(data.days || [])}
      </div>
      <div class="panel">
        <h3>星标 Top 10</h3>
        ${barList(
          topStars.map((p) => ({ name: p.full_name, count: p.github.stars })),
          { valueFormat: fmtNum, labelWidth: 150 }
        )}
      </div>
      <div class="panel">
        <h3>被多次推荐的项目</h3>
        ${
          topRecs.length
            ? barList(
                topRecs.map((p) => ({ name: p.full_name, count: p.recommendation_count })),
                { labelWidth: 150 }
              )
            : '<p class="muted">暂无重复推荐</p>'
        }
      </div>
      <div class="panel">
        <h3>数据概览</h3>
        ${overviewRows(s)}
      </div>
    </div>`;
}

function collapseTail(items, minCount) {
  // Keep items above minCount; fold the rest into an "其他" bucket.
  const kept = items.filter((i) => i.count > minCount);
  const rest = items.filter((i) => i.count <= minCount);
  if (!rest.length) return items;
  const restSum = rest.reduce((acc, i) => acc + i.count, 0);
  return kept.concat([{ name: `其他 (${rest.length})`, count: restSum }]);
}

function overviewRows(s) {
  const rows = [
    ['时间范围', `${s.date_min || '—'} → ${s.date_max || '—'}`],
    ['唯一维护者', `${fmtFullNum((s.top_owners || []).length)}+`],
    ['仓库修复', `${fmtFullNum((s.topics || []).length)} 个 topic`],
  ];
  return rows
    .map(
      ([k, v]) =>
        `<div class="bar-row" style="grid-template-columns:96px 1fr"><span class="label">${escapeHtml(k)}</span><span class="val" style="text-align:left">${escapeHtml(v)}</span></div>`
    )
    .join('');
}

function barList(items, opts = {}) {
  const valueFormat = opts.valueFormat || ((n) => fmtFullNum(n));
  if (!items || !items.length) return '<p class="muted">暂无数据</p>';
  const max = Math.max(...items.map((i) => i.count || 0), 1);
  const width = opts.labelWidth || 120;
  return items
    .map((i) => {
      const pct = Math.round(((i.count || 0) / max) * 100);
      return `<div class="bar-row" style="grid-template-columns:${width}px 1fr 48px">
        <span class="label" title="${escapeHtml(i.name)}">${escapeHtml(i.name)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
        <span class="val">${escapeHtml(valueFormat(i.count))}</span>
      </div>`;
    })
    .join('');
}

function heatmap(days) {
  if (!days.length) return '<p class="muted">暂无数据</p>';
  const counts = days.map((d) => d.count || 0);
  const max = Math.max(...counts, 1);
  const min = Math.min(...counts);
  const ordered = days.slice().sort((a, b) => a.date.localeCompare(b.date));
  const cells = ordered
    .map((d) => {
      const intensity = max === min ? 0.5 : (d.count - min) / (max - min);
      const alpha = 0.2 + intensity * 0.75;
      const bg = `rgba(124,138,255,${alpha.toFixed(2)})`;
      return `<span class="heat-cell" style="background:${bg}" title="${escapeHtml(d.date)}: ${d.count} 个项目"></span>`;
    })
    .join('');
  const legendSteps = [0.2, 0.45, 0.7, 0.95]
    .map((a) => `<span class="sw" style="background:rgba(124,138,255,${a})"></span>`)
    .join('');
  return `<div class="heat" role="img" aria-label="每日推荐数量热力图，共 ${days.length} 天，单日 ${min}–${max} 个项目">${cells}</div>
    <div class="heat-legend">每格为一天 · 少 ${legendSteps} 多 · 单日 ${min}–${max} 个项目，共 ${days.length} 天</div>`;
}

/* ------------------------------------------------------------------ filters UI */

function buildFilters() {
  const s = state.data.stats || {};
  const tags = (s.tags || []).slice(0, 40);
  const langs = s.languages || [];

  $('#tag-filters').innerHTML = tags
    .map(
      (t) =>
        `<button class="chip" data-filter="tag" data-value="${escapeHtml(t.name)}" type="button">${escapeHtml(t.name)} <span class="n">${t.count}</span></button>`
    )
    .join('');
  $('#lang-filters').innerHTML = langs
    .map(
      (l) =>
        `<button class="chip" data-filter="lang" data-value="${escapeHtml(l.name)}" type="button">${escapeHtml(l.name)} <span class="n">${l.count}</span></button>`
    )
    .join('');

  const tc = $('[data-count-for="tags"]');
  const lc = $('[data-count-for="languages"]');
  if (tc) tc.textContent = `(${tags.length})`;
  if (lc) lc.textContent = `(${langs.length})`;
}

function updateActiveFilters(resultCount) {
  const wrap = $('#active-filters');
  const chips = [];
  for (const t of state.tags) chips.push({ kind: 'tag', value: t, label: `标签: ${t}` });
  for (const l of state.languages) chips.push({ kind: 'lang', value: l, label: `语言: ${l}` });
  for (const f of state.flags)
    chips.push({ kind: 'flag', value: f, label: f === 'multi' ? '多次推荐' : '已归档' });

  if (!chips.length) {
    wrap.hidden = true;
    wrap.innerHTML = '';
  } else {
    wrap.hidden = false;
    wrap.innerHTML =
      chips
        .map(
          (c) =>
            `<button class="a-chip" data-remove-kind="${c.kind}" data-remove-value="${escapeHtml(c.value)}" type="button">${escapeHtml(c.label)} <span aria-hidden="true">×</span></button>`
        )
        .join('') +
      `<span class="muted" style="align-self:center;font-size:12px;margin-left:4px">${resultCount} 个结果</span>`;
  }

  const badge = $('#filter-count');
  const n = state.tags.size + state.languages.size + state.flags.size;
  if (n) {
    badge.hidden = false;
    badge.textContent = String(n);
  } else {
    badge.hidden = true;
  }

  // Mark active chips.
  $$('.chip').forEach((chip) => {
    const kind = chip.dataset.filter || 'flag';
    const value = chip.dataset.value || chip.dataset.status;
    let on = false;
    if (kind === 'tag') on = state.tags.has(value);
    else if (kind === 'lang') on = state.languages.has(value);
    else if (kind === 'flag' || chip.dataset.status) on = state.flags.has(value);
    chip.classList.toggle('is-on', on);
    chip.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function updateHeaderStats() {
  const s = state.data.stats || {};
  setStat('range', `${s.date_min || '—'} → ${s.date_max || '—'}`);
  setStat('generated', (state.data.generated_at || '').replace('T', ' ').replace('Z', ' UTC'));
  setStat('unique', fmtFullNum(s.unique_projects));
  setStat('records', fmtFullNum(s.total_records));
}

function setStat(name, value) {
  $$(`[data-stat="${name}"]`).forEach((el) => (el.textContent = value));
}

/* ------------------------------------------------------------------ states */

function setStatus(kind, message) {
  const region = $('#status-region');
  if (kind === 'loading') {
    region.innerHTML = `<div class="state"><div class="spinner"></div><h3>正在加载数据集…</h3><p>首次加载会读取完整推荐档案。</p></div>`;
    region.hidden = false;
    skeleton();
    return;
  }
  if (kind === 'error') {
    region.innerHTML = `<div class="state error"><h3>加载失败</h3><p>${escapeHtml(message || '无法读取数据集')}</p>
      <p style="margin-top:12px"><button class="btn-secondary" id="retry" type="button">重试</button></p></div>`;
    region.hidden = false;
    const r = $('#retry');
    if (r) r.addEventListener('click', boot);
    return;
  }
  if (kind === 'ready') {
    region.hidden = true;
    region.innerHTML = '';
    document.body.classList.remove('is-loading');
  }
}

function skeleton() {
  const root = $('#timeline-root');
  root.innerHTML = `<div class="skeleton-grid">${Array.from({ length: 6 })
    .map(() => '<div class="skeleton-card"></div>')
    .join('')}</div>`;
}

function emptyState() {
  return `<div class="state"><h3>没有匹配的项目</h3>
    <p>试试调整搜索关键词或清除筛选条件。</p>
    <p style="margin-top:12px"><button class="btn-secondary" id="empty-reset" type="button">清除全部筛选</button></p></div>`;
}

/* ------------------------------------------------------------------ dialog */

function openDetail(fullName) {
  const p = state.data.projects.find((x) => x.full_name === fullName);
  if (!p) return;
  const gh = p.github || {};
  const dialog = $('#detail');
  const tags = (p.reason_tags || []).map((t) => `<span class="tag-mini">${escapeHtml(t)}</span>`).join('');
  const topics = (gh.topics || []).map((t) => `<span class="tag-mini">${escapeHtml(t)}</span>`).join('');
  const dates = (p.recommendation_dates || [])
    .map((d) => `<span>${escapeHtml(d)}</span>`)
    .join('');
  const summary = daySummary(p.last_recommended);

  $('#detail-body').innerHTML = `
    <h2 id="detail-title">${escapeHtml(p.full_name)}</h2>
    <p class="detail-desc">${gh.description ? escapeHtml(gh.description) : '暂无描述'}</p>
    <div class="detail-actions">
      <a class="btn-primary" href="${escapeHtml(p.url)}" target="_blank" rel="noopener noreferrer">在 GitHub 打开 ↗</a>
      ${gh.homepage ? `<a class="btn-secondary" href="${escapeHtml(gh.homepage)}" target="_blank" rel="noopener noreferrer">项目主页 ↗</a>` : ''}
    </div>
    <div class="detail-stats">
      <div class="ds"><div class="v">${fmtNum(gh.stars)}</div><div class="k">星标</div></div>
      <div class="ds"><div class="v">${fmtNum(gh.forks)}</div><div class="k">Fork</div></div>
      <div class="ds"><div class="v">${gh.language ? escapeHtml(gh.language) : '—'}</div><div class="k">语言</div></div>
      <div class="ds"><div class="v">${gh.license ? escapeHtml(gh.license) : '—'}</div><div class="k">许可证</div></div>
      <div class="ds"><div class="v">${fmtNum(gh.open_issues)}</div><div class="k">开放 Issue</div></div>
      <div class="ds"><div class="v">${p.recommendation_count}</div><div class="k">推荐次数</div></div>
    </div>
    ${tags ? `<div class="detail-section"><h4>推荐标签</h4><div class="card-meta">${tags}</div></div>` : ''}
    ${topics ? `<div class="detail-section"><h4>仓库 Topics</h4><div class="card-meta">${topics}</div></div>` : ''}
    <div class="detail-section"><h4>推荐日期</h4><div class="detail-dates">${dates}</div></div>
    ${summary ? `<div class="detail-section"><h4>当日推荐说明</h4><p class="day-summary expanded" style="margin:0;-webkit-line-clamp:unset">${escapeHtml(summary)}</p></div>` : ''}
    <div class="detail-section"><h4>仓库动态</h4>
      <div class="detail-dates">
        <span>创建 ${escapeHtml(fmtDate(gh.created_at))}</span>
        <span>最近推送 ${escapeHtml(fmtDate(gh.pushed_at))}</span>
        <span>元数据 ${escapeHtml(fmtDate(gh.fetched_at))}</span>
      </div>
    </div>`;

  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  const inner = dialog.querySelector('.detail-inner');
  if (inner) inner.scrollTop = 0;
}

function syncHash() {
  const parts = [];
  if (state.query) parts.push('q=' + encodeURIComponent(state.query));
  if (state.tags.size) parts.push('t=' + Array.from(state.tags).map(encodeURIComponent).join(','));
  if (state.languages.size) parts.push('l=' + Array.from(state.languages).map(encodeURIComponent).join(','));
  if (state.flags.size) parts.push('f=' + Array.from(state.flags).join(','));
  if (state.view !== 'timeline') parts.push('v=' + state.view);
  if (state.sort !== 'date-desc') parts.push('s=' + state.sort);
  const hash = parts.length ? '#' + parts.join('&') : '';
  history.replaceState(null, '', location.pathname + location.search + hash);
}

function applyHash() {
  const hash = location.hash.replace(/^#/, '');
  if (!hash) return;
  const params = new URLSearchParams(hash);
  if (params.get('q')) {
    state.query = params.get('q');
    $('#search').value = state.query;
  }
  if (params.get('t')) params.get('t').split(',').filter(Boolean).forEach((t) => state.tags.add(decodeURIComponent(t)));
  if (params.get('l')) params.get('l').split(',').filter(Boolean).forEach((l) => state.languages.add(decodeURIComponent(l)));
  if (params.get('f')) params.get('f').split(',').filter(Boolean).forEach((f) => state.flags.add(f));
  if (params.get('v')) switchView(params.get('v'), false);
  if (params.get('s')) {
    state.sort = params.get('s');
    $('#sort').value = state.sort;
  }
}

/* ------------------------------------------------------------------ wiring */

function switchView(view, update = true) {
  if (!['timeline', 'gallery', 'stats'].includes(view)) view = 'timeline';
  state.view = view;
  $$('.view-btn').forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('#view-timeline').hidden = view !== 'timeline';
  $('#view-gallery').hidden = view !== 'gallery';
  $('#view-stats').hidden = view !== 'stats';
  if (update) {
    syncHash();
    render();
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function wire() {
  $('#search').addEventListener(
    'input',
    debounce((e) => {
      state.query = e.target.value;
      $('#search-clear').hidden = !state.query;
      syncHash();
      render();
    }, 120)
  );
  $('#search-clear').addEventListener('click', () => {
    state.query = '';
    $('#search').value = '';
    $('#search-clear').hidden = true;
    syncHash();
    render();
    $('#search').focus();
  });

  $('#sort').addEventListener('change', (e) => {
    state.sort = e.target.value;
    syncHash();
    render();
  });

  $$('.view-btn').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.view)));

  $('#filters-toggle').addEventListener('click', () => {
    state.filtersOpen = !state.filtersOpen;
    const panel = $('#filters');
    panel.hidden = !state.filtersOpen;
    $('.layout').classList.toggle('has-filters', state.filtersOpen);
    $('#filters-toggle').setAttribute('aria-expanded', state.filtersOpen ? 'true' : 'false');
  });

  // Delegate chip + card clicks.
  document.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip) {
      const kind = chip.dataset.filter || (chip.dataset.status ? 'flag' : null);
      const value = chip.dataset.value || chip.dataset.status;
      if (kind === 'tag') toggle(state.tags, value);
      else if (kind === 'lang') toggle(state.languages, value);
      else toggle(state.flags, value);
      syncHash();
      render();
      return;
    }
    const aChip = e.target.closest('.a-chip');
    if (aChip) {
      const kind = aChip.dataset.removeKind;
      const value = aChip.dataset.removeValue;
      if (kind === 'tag') state.tags.delete(value);
      else if (kind === 'lang') state.languages.delete(value);
      else state.flags.delete(value);
      syncHash();
      render();
      return;
    }
    const card = e.target.closest('.card');
    if (card) {
      openDetail(card.dataset.fullName);
      return;
    }
    const summaryToggle = e.target.closest('[data-toggle-summary]');
    if (summaryToggle) {
      const id = summaryToggle.dataset.toggleSummary;
      const target = document.getElementById(id);
      if (target) {
        const expanded = summaryToggle.getAttribute('aria-expanded') === 'true';
        target.textContent = expanded
          ? (summaryStore[id] || target.textContent).slice(0, 150).trimEnd() + '…'
          : summaryStore[id] || target.textContent;
        summaryToggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        summaryToggle.textContent = expanded ? '展开全文' : '收起';
      }
      return;
    }
    if (e.target.id === 'empty-reset') {
      resetFilters();
    }
  });

  $('#filters-reset').addEventListener('click', resetFilters);
  $('#detail-close').addEventListener('click', () => $('#detail').close());
  $('#detail').addEventListener('click', (e) => {
    if (e.target === $('#detail')) $('#detail').close();
  });

  // Keyboard shortcuts.
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (e.key === '/' && !typing) {
      e.preventDefault();
      $('#search').focus();
    } else if (e.key === 'Escape' && typing && e.target.id === 'search') {
      e.target.blur();
    }
  });

  window.addEventListener('hashchange', () => {
    // Support back/forward navigation.
    resetStateSilently();
    applyHash();
    render();
  });
}

function toggle(set, value) {
  if (set.has(value)) set.delete(value);
  else set.add(value);
}

function resetFilters() {
  state.tags.clear();
  state.languages.clear();
  state.flags.clear();
  state.query = '';
  $('#search').value = '';
  $('#search-clear').hidden = true;
  syncHash();
  render();
}

function resetStateSilently() {
  state.query = '';
  state.tags.clear();
  state.languages.clear();
  state.flags.clear();
  state.view = 'timeline';
  state.sort = 'date-desc';
  $('#search').value = '';
  $('#sort').value = 'date-desc';
  $$('.view-btn').forEach((b) => {
    const on = b.dataset.view === 'timeline';
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('#view-timeline').hidden = false;
  $('#view-gallery').hidden = true;
  $('#view-stats').hidden = true;
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  try {
    const data = await loadData();
    state.data = data;
    buildFilters();
    applyHash();
    setStatus('ready');
    render();
  } catch (err) {
    setStatus('error', err && err.message ? err.message : String(err));
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    wire();
    boot();
  });
} else {
  wire();
  boot();
}

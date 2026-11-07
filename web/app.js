/* Bubble Board front-end: a zoomable pack layout over /api/bubbles. */
(() => {
  'use strict';

  const API = {
    config: '/api/config',
    universes: '/api/universes',
    bubbles: '/api/bubbles',
    history: '/api/history',
  };

  const TIMEFRAMES = [
    { value: '1d', label: '1日' },
    { value: '5d', label: '5日' },
    { value: '1mo', label: '1月' },
    { value: '3mo', label: '3月' },
    { value: '1y', label: '1年' },
  ];

  // 涨跌只有两种色相：红=跌、绿=涨；幅度只影响亮度，气泡内部留空（空心环）
  const UP_COLOR = '#33e884';
  const DOWN_COLOR = '#ff4a55';
  const UP_DIM = '#2a7d54';
  const DOWN_DIM = '#8c2b35';
  const FLAT_RING = 'rgba(120, 133, 152, 0.34)';
  const FLAT_CORE = 'rgba(120, 133, 152, 0.08)';

  const ringOf = (value, clamp) => {
    if (value === null || value === undefined) {
      return { ring: FLAT_RING, core: FLAT_CORE, glow: 0, flat: true };
    }
    const ratio = Math.min(1, Math.abs(value) / Math.max(clamp, 1e-6));
    const eased = Math.pow(ratio, 0.62); // 小幅波动也看得出颜色，不必等到 ±10%
    const base = value >= 0 ? UP_COLOR : DOWN_COLOR;
    const dim = value >= 0 ? UP_DIM : DOWN_DIM;
    // 环几乎保持满饱和：幅度只调控“亮度”，不把颜色调成脏褐色
    const ring = d3.interpolateRgb(dim, base)(0.68 + 0.32 * eased);
    const core = value >= 0 ? 'url(#core-up)' : 'url(#core-down)';
    return { ring, core, glow: eased, flat: false };
  };


  const $ = (id) => document.getElementById(id);
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // 半径 ∝ size^(scale/2)。三种口径：均衡 0.26（最大/最小约 2.5–3 倍）、√压缩 0.5（约 9 倍）、
  // 面积∝数值 1（约 100 倍）。默认取「均衡」，气泡之间差得不多、读数更整齐。
  const SCALE_OPTIONS = [0.26, 0.5, 1];
  const DEFAULT_SCALE = 0.26;
  // 顶栏「数量」弹窗里的常用档位；0 表示全部（取 config.max_nodes）
  const LIMIT_PRESETS = [20, 50, 100, 200, 0];
  const DEFAULT_LIMIT = 100;
  // 气泡口径默认「24h 成交额」：成交额比市值更快反映当下资金在关注谁
  const DEFAULT_SIZE = 'volume';
  const HISTORY_TTL_MS = 60_000;
  const HISTORY_STALE_MS = 15 * 60_000;
  const HISTORY_CACHE_LIMIT = 64;
  let snapshotRequest = null;
  let historyRequest = null;
  let historyRequestKey = null;
  let historyToken = 0;

  const state = {
    config: null,
    universes: [],
    universe: null,
    window: '24h',
    size: DEFAULT_SIZE,
    scale: DEFAULT_SCALE,
    group: 'none',
    limit: DEFAULT_LIMIT,
    query: '',
    snapshot: null,
    selected: null,
    timeframe: '1mo',
    remaining: 0,
    loading: false,
    requestToken: 0,
    historyCache: new Map(),
    historyCooldowns: new Map(),
  };

  const el = {
    universes: $('universes'),
    windows: $('windows'),
    bases: $('bases'),
    scales: $('scales'),
    groups: $('groups'),
    limitTrigger: $('limit-trigger'),
    limitPop: $('limit-pop'),
    limitValue: $('limit-value'),
    limitPresets: $('limit-presets'),
    limitForm: $('limit-form'),
    limitInput: $('limit-input'),
    search: $('search'),
    refresh: $('refresh'),
    relayout: $('relayout'),
    countdown: $('countdown'),
    legendMin: $('legend-min'),
    legendMax: $('legend-max'),
    stage: document.querySelector('.stage'),
    svg: d3.select('#bubbles'),
    loading: $('loading'),
    loadingText: $('loading-text'),
    empty: $('empty'),
    tooltip: $('tooltip'),
    statusMeta: $('status-meta'),
    statusWarnings: $('status-warnings'),
    statusUpdated: $('status-updated'),
    detail: $('detail'),
    detailClose: $('detail-close'),
    detailSymbol: $('detail-symbol'),
    detailName: $('detail-name'),
    detailSector: $('detail-sector'),
    detailPrice: $('detail-price'),
    detailCurrency: $('detail-currency'),
    detailChange: $('detail-change'),
    detailFoot: $('detail-foot'),
    detailWindows: $('detail-windows'),
    detailFacts: $('detail-facts'),
    timeframes: $('timeframes'),
    sparkline: d3.select('#sparkline'),
    sparkMeta: $('spark-meta'),
    toast: $('toast'),
  };

  /* ------------------------------------------------------------ formatting */

  const fmtPct = (value) => {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const sign = value > 0 ? '+' : '';
    return `${sign}${value.toFixed(2)}%`;
  };

  const fmtCompact = (value) => {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const units = [
      [1e12, 'T'],
      [1e9, 'B'],
      [1e6, 'M'],
      [1e3, 'K'],
    ];
    for (const [factor, suffix] of units) {
      if (Math.abs(value) >= factor) {
        const scaled = value / factor;
        const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
        return `${scaled.toFixed(digits)}${suffix}`;
      }
    }
    return value.toFixed(2);
  };

  const fmtPrice = (value) => {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    if (Math.abs(value) >= 1000) {
      return value.toLocaleString('en-US', { maximumFractionDigits: 0 });
    }
    if (Math.abs(value) >= 1) return value.toFixed(2);
    if (Math.abs(value) >= 0.01) return value.toFixed(4);
    return value.toFixed(6);
  };

  /* 告警文案：状态栏只放一句人话，英文原文留在 title 里 */
  const WARNING_TEXT = {
    stale_cache: '行情为缓存副本',
    no_data: '暂无可用标的',
    missing_symbols: '部分标的无行情',
    chunk_failed: '数据源返回异常',
    quotes_fell_back: '已切换到备用行情源',
    openbb_skipped: 'OpenBB 冷却中',
    partial_coverage: '上游覆盖不全',
  };

  const warningText = (warning) => WARNING_TEXT[warning.code] || warning.message || warning.code;

  const fmtClock = (iso) => {
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return '—';
    return parsed.toLocaleTimeString('zh-CN', { hour12: false });
  };

  const toneOf = (value) => {
    if (value === null || value === undefined) return 'flat';
    if (value > 0.005) return 'up';
    if (value < -0.005) return 'down';
    return 'flat';
  };

  const basisLabel = () => (state.size === 'volume' ? '成交额' : '市值');

  /** Largest font size that keeps ``text`` inside a circle of ``radius``. */
  const fitFont = (text, radius, fill = 0.86) => {
    const usable = radius * 2 * fill;
    return usable / Math.max(String(text).length * 0.62, 1);
  };

  /* ---------------------------------------------------------------- helpers */

  const toast = (() => {
    let timer = 0;
    return (message) => {
      el.toast.textContent = message;
      el.toast.hidden = false;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        el.toast.hidden = true;
      }, 4200);
    };
  })();

  async function fetchJSON(url, { signal, timeout = 20_000 } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    try {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      const text = await response.text();
      let payload = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }
      if (!response.ok) {
        const error = payload && payload.error ? payload.error : {};
        const detail = error.message || `${response.status} ${response.statusText}`;
        const failure = new Error(error.hint ? `${detail}（${error.hint}）` : detail);
        failure.code = error.code;
        failure.retryAfter = Math.min(3600, Math.max(1, Number(response.headers.get('Retry-After')) || 60));
        throw failure;
      }
      if (payload === null) throw new Error('响应不是合法的 JSON');
      return payload;
    } catch (error) {
      if (timedOut) throw new Error('请求超时，请稍后重试');
      throw error;
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  /* -------------------------------------------------------------- controls */

  function segmented(container, options, current, onPick, describe) {
    container.replaceChildren();
    for (const option of options) {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.dataset.value = option.value;
      button.textContent = option.label;
      button.setAttribute('aria-checked', String(option.value === current));
      button.addEventListener('click', () => onPick(option.value));
      if (describe) button.title = describe(option);
      container.append(button);
    }
  }

  function syncSegmented(container, value) {
    for (const button of container.querySelectorAll('button')) {
      button.setAttribute('aria-checked', String(button.dataset.value === String(value)));
    }
  }

  // 静态 HTML 里的分段按钮用的是 data-scale / data-group / data-limit，
  // 统一补一份 data-value，syncSegmented 才能正确点亮当前项。
  function adoptSegmentValues(container, key) {
    for (const button of container.querySelectorAll('button')) {
      const raw = button.dataset[key];
      if (raw !== undefined) button.dataset.value = raw;
    }
  }

  function renderUniverseTabs() {
    el.universes.replaceChildren();
    for (const universe of state.universes) {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.id = universe.id;
      button.textContent = universe.name;
      button.setAttribute('aria-selected', String(universe.id === state.universe));
      button.title = `${universe.name} · ${universe.instrument_count} 个标的 · ${universe.currency}`;
      button.addEventListener('click', () => {
        if (state.universe === universe.id) return;
        state.universe = universe.id;
        const meta = currentUniverse();
        if (meta && !meta.windows.includes(state.window)) state.window = meta.default_window;
        renderUniverseTabs();
        renderWindowButtons();
        closeDetail();
        void loadSnapshot();
      });
      el.universes.append(button);
    }
  }

  function currentUniverse() {
    return state.universes.find((item) => item.id === state.universe) || null;
  }

  function renderWindowButtons() {
    const meta = currentUniverse();
    const allowed = meta ? meta.windows : state.config.windows.map((item) => item.value);
    const options = state.config.windows.filter((item) => allowed.includes(item.value));
    segmented(el.windows, options, state.window, (value) => {
      state.window = value;
      syncSegmented(el.windows, value);
      renderLegend();
      void loadSnapshot();
    });
  }

  function renderBaseButtons() {
    segmented(el.bases, state.config.size_bases, state.size, (value) => {
      state.size = value;
      syncSegmented(el.bases, value);
      void loadSnapshot();
    });
  }

  function renderLegend() {
    const clamp = Number(state.config.color_clamp_pct) || 10;
    el.legendMin.textContent = `-${clamp}%`;
    el.legendMax.textContent = `+${clamp}%`;
  }

  /* ------------------------------------------------------------- data load */

  function bubbleURL() {
    const limit = Number(state.limit) > 0 ? Number(state.limit) : Number(state.config.max_nodes || 400);
    const params = new URLSearchParams({
      universe: state.universe,
      window: state.window,
      size: state.size,
      limit: String(limit),
    });
    return `${API.bubbles}?${params.toString()}`;
  }

  async function loadSnapshot() {
    if (!state.universe) return;
    const token = ++state.requestToken;
    snapshotRequest?.abort();
    snapshotRequest = new AbortController();
    state.loading = true;
    el.refresh.classList.add('spinning');
    if (!state.snapshot) {
      el.loading.hidden = false;
      el.loadingText.textContent = '正在加载行情…';
    }
    try {
      const snapshot = await fetchJSON(bubbleURL(), { signal: snapshotRequest.signal });
      if (token !== state.requestToken) return;
      state.snapshot = snapshot;
      el.empty.hidden = true;
      updateGroupHint();
      render();
      updateStatus();
      state.remaining = Number(state.config.auto_refresh_seconds) || 60;
      if (state.selected) selectSymbol(state.selected, { keepDetail: true });
    } catch (error) {
      if (token !== state.requestToken || error.name === 'AbortError') return;
      el.loading.hidden = true;
      toast(`加载失败：${error.message}`);
      if (!state.snapshot) {
        el.empty.hidden = false;
        el.empty.textContent = `暂时无法获取行情：${error.message}`;
      }
    } finally {
      if (token === state.requestToken) {
        state.loading = false;
        el.refresh.classList.remove('spinning');
        el.loading.hidden = true;
        syncURL();
      }
    }
  }

  /* ---------------------------------------------------------------- layout */

  const root = el.svg.append('g').attr('class', 'viewport');

  // 两组复用渐变：气泡内部“上亮下暗”的一点体积感，按涨/跌各一个
  const defs = el.svg.append('defs');
  const coreGradient = (id, color) =>
    defs
      .append('radialGradient')
      .attr('id', id)
      .attr('cx', '50%')
      .attr('cy', '50%')
      .attr('r', '50%')
      .call((gradient) => {
        gradient
          .append('stop')
          .attr('offset', '0%')
          .attr('stop-color', color)
          .attr('stop-opacity', 0.02);
        gradient
          .append('stop')
          .attr('offset', '62%')
          .attr('stop-color', color)
          .attr('stop-opacity', 0.05);
        gradient
          .append('stop')
          .attr('offset', '88%')
          .attr('stop-color', color)
          .attr('stop-opacity', 0.14);
        gradient
          .append('stop')
          .attr('offset', '100%')
          .attr('stop-color', color)
          .attr('stop-opacity', 0.2);
      });
  coreGradient('core-up', UP_COLOR);
  coreGradient('core-down', DOWN_COLOR);

  const fitLayer = root.append('g').attr('class', 'fit');
  const sectorLayer = fitLayer.append('g').attr('class', 'sectors');
  const bubbleLayer = fitLayer.append('g').attr('class', 'bubbles');
  const sectorLabelLayer = fitLayer.append('g').attr('class', 'sector-labels');

  let zoomBehaviour = null;
  let lastZoom = d3.zoomIdentity;

  function stageSize() {
    const rect = el.stage.getBoundingClientRect();
    // 抽屉从两条工具栏下面开始，这里把工具栏总高度写成 CSS 变量，抽屉和它对齐。
    document.documentElement.style.setProperty('--bar-h', `${Math.max(0, Math.round(rect.top))}px`);
    // 详情抽屉打开时 `.stage.has-detail` 会加右侧内边距，画布可用宽度要跟着缩，
    // 否则右侧那几颗球会被抽屉盖住（用户看到的现象：设了 20 个、只看到 14 个）。
    const style = window.getComputedStyle(el.stage);
    const inset = Number.parseFloat(style.paddingRight) || 0;
    const width = Math.max(1, rect.width - inset);
    return { width, height: Math.max(1, rect.height - 2) };
  }

  /** Scale/centre the packed layout so the cluster fills the stage evenly. */
  function fitLayout(layout, width, height) {
    const nodes = layout.descendants().filter((node) => node.depth > 0);
    if (!nodes.length) {
      fitLayer.attr('transform', null);
      return;
    }
    const margin = 26;
    const minX = d3.min(nodes, (node) => node.x - node.r);
    const maxX = d3.max(nodes, (node) => node.x + node.r);
    const minY = d3.min(nodes, (node) => node.y - node.r);
    const maxY = d3.max(nodes, (node) => node.y + node.r);
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const k = Math.min(
      (width - margin * 2) / spanX,
      (height - margin * 2) / spanY,
      1.9,
    );
    const tx = width / 2 - (k * (minX + maxX)) / 2;
    const ty = height / 2 - (k * (minY + maxY)) / 2;
    fitLayer.attr('transform', `translate(${tx.toFixed(2)},${ty.toFixed(2)}) scale(${k.toFixed(4)})`);
  }

  // 半径 ∝ size^p，p 由「目标半径比」反推，所以换资产类别时视觉差异一致：
  //   均衡   → 最大/最小 ≈ 2.2 倍
  //   适中   → ≈ 4.2 倍
  //   面积∝数值 → p = 0.5（严格面积比例，BTC 会吃掉半屏）
  function sizeExponent(nodes, mode) {
    const target = mode === 0.26 || mode === '0.26' ? 2.2 : mode === 0.5 || mode === '0.5' ? 4.2 : 0;
    if (!target) return 0.5;
    let lo = Infinity;
    let hi = 0;
    for (const node of nodes) {
      const size = Math.max(node.size_value, 1);
      if (size < lo) lo = size;
      if (size > hi) hi = size;
    }
    if (!(hi > lo)) return 0.5;
    const p = Math.log(target) / Math.log(hi / lo);
    return Math.min(0.5, Math.max(0.02, p));
  }

  function buildHierarchy(nodes, scale) {
    const p = sizeExponent(nodes, scale);
    const weight = (item) => Math.pow(Math.max(item.size_value, 1), p * 2);
    if (state.group !== 'sector') {
      return d3
        .hierarchy({
          children: nodes
            .map((item) => ({ ...item, weight: weight(item) }))
            .sort((a, b) => b.weight - a.weight),
        })
        .sum((node) => node.weight || 0)
        .sort((a, b) => b.value - a.value);
    }

    const groups = d3.group(nodes, (node) => node.sector || '未分类');
    const children = [...groups.entries()]
      .map(([sector, items]) => ({
        sector,
        children: items
          .slice()
          .sort((a, b) => b.size_value - a.size_value)
          .map((item) => ({ ...item, weight: weight(item) })),
      }))
      .sort(
        (a, b) =>
          d3.sum(b.children, (item) => item.weight) - d3.sum(a.children, (item) => item.weight),
      );

    return d3
      .hierarchy({ children })
      .sum((node) => (node.weight === undefined ? 0 : node.weight))
      .sort((a, b) => b.value - a.value);
  }

  /* --------------------------------------------------------------- physics */
  //
  // 引擎：planck.js 1.4（Box2D 的 JS 移植，MIT，本地打包在 /vendor/planck.min.js）。
  // Box2D 是游戏行业里用了十几年的刚体引擎，顺序冲量求解器 —— 该有的接触稳定性、
  // 堆叠不抖、动量传递都在引擎里，我们只负责「每颗球一根回家的弹簧」和拖动。
  //
  //  · 刚体：圆形 fixture，`density 0.001`（质量 ∝ r²）、`friction 0`、`restitution 0`。
  //  · 回位：每步给「目标 − 当前位置」算一个弹簧力 `F = m·(k·Δx − c·v)`，
  //          目标 = 布局位置 + 自己的游动偏移；阻尼来自力本身，不用 linearDamping。
  //  · 拖动：把刚体切成 `kinematic` 并每帧 setTransform（无限质量、只挤别人）；
  //          松手切回 `dynamic`，把跟手速度写进 setLinearVelocity。
  //  · 墙：四个静态 box，`restitution 0`。
  //  · 固定步长 1/60s，单帧最多补 3 步（`world.step(1/60, 5, 3)`）。

  const planck = window.planck;

  const sim = {
    nodes: [],
    bySymbol: new Map(),
    raf: 0,
    last: 0,
    acc: 0,
    dragging: null,
    dragFrom: { x: 0, y: 0 },
    dragMoved: 0,
    mouseJoint: null,
    layoutKey: '',
    width: 0,
    height: 0,
    float: !prefersReducedMotion && !window.matchMedia('(pointer: coarse), (max-width: 860px)').matches,
    idleMs: 0,
    activeMs: 0,
    world: null,
    ready: false,
    lastX: 0,
    lastY: 0,
    lastT: 0,
    vx: 0,
    vy: 0,
  };

  const SPRING_K = 11; // 弹簧刚度（1/s²）：越大回位越快
  const SPRING_C = 6.4; // 阻尼（1/s）：≈ 临界阻尼，回位不过冲
  const WANDER_RADIUS = 6; // 游动半径（px）
  const WANDER_SPEED = 0.2; // 游动角速度（rad/s）
  const FIXED_STEP_MS = 1000 / 60;
  const MAX_STEPS = 3;
  const THROW_SCALE = 0.08; // 松手时的速度保留比例（跟手速度 → 甩出速度）
  const THROW_MAX = 140; // 甩出速度上限（px/s）
  const WALL_PADDING = 16; // 墙离画布边缘多远（px）：0 会让球心正好卡在边上，2 留一点余量
  const DRAG_FORCE = 20000; // 鼠标关节的最大拉力（× 质量）：越大越"跟手"，越小越黏
  const DRAG_FREQUENCY = 10; // 鼠标关节频率（Hz）：跟随的软硬
  const DRAG_DAMPING = 0.72; // 鼠标关节阻尼比
  const DRAG_MAX_SPEED = 2200; // 被拖球的速度上限（px/s），只是个兜底
  const OVERLAP_PASSES = 10; // 每步补几轮位置投影（把残余穿透抹平）
  const DRAG_INV_MASS = 0.5; // 拖动中被抓的那颗也让一点路（0 = 完全不动，1 = 和邻居一样让）
  const OVERLAP_TOLERANCE = 0.2; // 允许的亚像素重叠（px）：看不见，但能避免每帧抖
  const REST_GAP = 9; // 静止时相邻球之间保留的空隙（px）：谁也别贴着谁
  const REST_PUSH = 0.22; // 空隙不足时每帧补回的比例（软推，不抢弹簧的活）
  const BOUNCE_RESTITUTION = 0.68; // 接触时的弹性：撞击后按这个比例还回去
  const BOUNCE_MIN_SPEED = 55; // 低于这个接近速度（px/s）轻轻碰上就不弹，避免抖动
  const BOUNCE_DAMPING = 0.55; // 撞击冲量在两侧之间的分配基准
  const BOUNCE_COOLDOWN = 180; // 同一对球两次弹开的冷却（ms）
  const HIT_PULSE_MS = 220; // 撞击高亮持续时间（ms）

  /** 每颗球以不同相位绕锚点慢慢画圈：整张图像在缓慢呼吸。 */
  function wanderOffset(entry) {
    if (!sim.float) return { x: 0, y: 0 };
    const t = performance.now() / 1000;
    return {
      x: Math.cos(t * WANDER_SPEED + entry.phase) * WANDER_RADIUS,
      y: Math.sin(t * WANDER_SPEED * 0.78 + entry.phase * 1.7) * WANDER_RADIUS,
    };
  }

  function buildWorld() {
    // Box2D 默认每步最多移动 2 个"单位";我们把 1 单位当 1px,
    // 于是 2 单位/步 = 120px/s —— 拖起来会像拖着铅球。放宽到 40px/步。
    planck.Settings.maxTranslation = 10;
    const world = new planck.World({ gravity: planck.Vec2(0, 0) });
    world.setAutoClearForces(true);
    sim.world = world;
    const w = Math.max(sim.width, 1);
    const h = Math.max(sim.height, 1);
    const pad = WALL_PADDING;
    const thick = 60;
    const walls = [
      { x: w / 2, y: -pad - thick / 2, hw: w / 2 + pad + thick, hh: thick / 2 },
      { x: w / 2, y: h + pad + thick / 2, hw: w / 2 + pad + thick, hh: thick / 2 },
      { x: -pad - thick / 2, y: h / 2, hw: thick / 2, hh: h / 2 + pad + thick },
      { x: w + pad + thick / 2, y: h / 2, hw: thick / 2, hh: h / 2 + pad + thick },
    ];
    for (const wall of walls) {
      const body = world.createBody({ type: 'static', position: planck.Vec2(wall.x, wall.y) });
      body.createFixture(planck.Box(wall.hw, wall.hh), {
        friction: 0,
        restitution: 0,
        density: 0,
      });
    }
    sim.ground = world.createBody({ type: 'static', position: planck.Vec2(0, 0) });
  }

  function syncSim(leaves, relayout) {
    const seen = new Set();
    const nodes = [];
    if (!sim.ready || relayout || !sim.world) {
      buildWorld();
      sim.bySymbol.clear();
      sim.ready = true;
    }
    for (const leaf of leaves) {
      const symbol = leaf.data.symbol;
      seen.add(symbol);
      let entry = sim.bySymbol.get(symbol);
      if (!entry) {
        const body = sim.world.createBody({
          type: 'dynamic',
          position: planck.Vec2(leaf.x, leaf.y),
          linearDamping: 0,
          angularDamping: 0.6,
          fixedRotation: true,
        });
        body.createFixture(planck.Circle(leaf.r), {
          density: 0.001,
          friction: 0,
          restitution: 0,
        });
        body.setUserData({ symbol });
        entry = {
          symbol,
          phase: Math.random() * Math.PI * 2,
          body,
          homeX: leaf.x,
          homeY: leaf.y,
          x: leaf.x,
          y: leaf.y,
          r: leaf.r,
          el: null,
        };
        sim.bySymbol.set(symbol, entry);
      } else {
        entry.homeX = leaf.x;
        entry.homeY = leaf.y;
        entry.r = leaf.r;
        if (relayout) {
          entry.body.setTransform(planck.Vec2(leaf.x, leaf.y), 0);
          entry.body.setLinearVelocity(planck.Vec2(0, 0));
          entry.x = leaf.x;
          entry.y = leaf.y;
        }
      }
      nodes.push(entry);
    }
    for (const key of [...sim.bySymbol.keys()]) {
      if (seen.has(key)) continue;
      sim.world.destroyBody(sim.bySymbol.get(key).body);
      sim.bySymbol.delete(key);
    }
    sim.nodes = nodes;
  }

  function paintSim() {
    for (const entry of sim.nodes) {
      const p = entry.body.getPosition();
      entry.x = p.x;
      entry.y = p.y;
      if (!entry.el) continue;
      const next = `translate3d(${p.x.toFixed(2)}px, ${p.y.toFixed(2)}px, 0)`;
      if (entry.paint === next) continue;
      entry.paint = next;
      // 用 CSS transform 而不是 transform 属性：走合成器，不触发整棵 SVG 重绘
      entry.el.style.transform = next;
    }
  }

  function stepSim() {
    const dt = FIXED_STEP_MS / 1000;
    for (const entry of sim.nodes) {
      const body = entry.body;
      // 被拖的那颗由鼠标关节驱动，跳过回位弹簧（否则会和手抢方向）
      if (sim.dragging === entry) {
        // 速度上限：快甩时宁可让它落后一点，也不要一步跨太远把邻居弹飞
        const v = body.getLinearVelocity();
        const speed = Math.hypot(v.x, v.y);
        if (speed > DRAG_MAX_SPEED) {
          const ratio = DRAG_MAX_SPEED / speed;
          body.setLinearVelocity(planck.Vec2(v.x * ratio, v.y * ratio));
        }
        continue;
      }
      if (body.getType() === 'kinematic') continue;
      const wander = wanderOffset(entry);
      const p = body.getPosition();
      const v = body.getLinearVelocity();
      const ax = (entry.homeX + wander.x - p.x) * SPRING_K - v.x * SPRING_C;
      const ay = (entry.homeY + wander.y - p.y) * SPRING_K - v.y * SPRING_C;
      const mass = body.getMass();
      body.applyForceToCenter(planck.Vec2(ax * mass, ay * mass), true);
    }
    sim.world.step(dt, 5, 3);
    // 冲量求解器是"软"的：弹簧一直往里压时，位置迭代会留下一两像素的残余穿透。
    // 拖动过程中允许重叠，但停下来之后必须是分开的 —— 这里再补几轮纯位置投影。
    separateOverlaps();
    // 少量标的时球很大，四面墙之间塞不下，求解器可能把它们从角落挤出去；
    // 兜底按半径把球心钳回画布内，保证"显示 20 个"就是真的 20 个都看得见。
    clampToStage();
    paintSim();
  }

  /**
   * 硬钳位：球心永远落在 [r + padding, 尺寸 − r − padding] 内。
   * 球比画布还大时退化为居中，至少不会整颗消失。
   */
  function clampToStage() {
    const width = sim.width;
    const height = sim.height;
    if (!(width > 0) || !(height > 0)) return;
    for (const entry of sim.nodes) {
      if (sim.dragging === entry) continue;
      const body = entry.body;
      const position = body.getPosition();
      const minX = entry.r + WALL_PADDING;
      const maxX = width - entry.r - WALL_PADDING;
      const minY = entry.r + WALL_PADDING;
      const maxY = height - entry.r - WALL_PADDING;
      const x = minX > maxX ? width / 2 : Math.min(Math.max(position.x, minX), maxX);
      const y = minY > maxY ? height / 2 : Math.min(Math.max(position.y, minY), maxY);
      if (Math.abs(x - position.x) > 0.01 || Math.abs(y - position.y) > 0.01) {
        body.setPosition(planck.Vec2(x, y));
        const velocity = body.getLinearVelocity();
        body.setLinearVelocity(planck.Vec2(velocity.x * 0.5, velocity.y * 0.5));
      }
    }
  }

  /**
   * 纯位置投影：不改速度、不注入能量，只把仍然叠在一起的球沿圆心连线推开。
   * 拖动中的那颗不参与（用户允许拖动时压过去），松手后自然会被推正。
   */
  function separateOverlaps() {
    const nodes = sim.nodes;
    if (nodes.length < 2) return;
    let maxR = 0;
    for (const node of nodes) if (node.r > maxR) maxR = node.r;
    const cell = Math.max(maxR * 2, 24);
    for (let pass = 0; pass < OVERLAP_PASSES; pass += 1) {
      const cells = new Map();
      const key = (ix, iy) => ix * 100000 + iy;
      for (const node of nodes) {
        const p = node.body.getPosition();
        node.x = p.x;
        node.y = p.y;
        const ix = Math.floor(p.x / cell);
        const iy = Math.floor(p.y / cell);
        const bucketKey = key(ix, iy);
        let bucket = cells.get(bucketKey);
        if (!bucket) {
          bucket = [];
          cells.set(bucketKey, bucket);
        }
        bucket.push(node);
      }
      let worst = 0;
      for (const node of nodes) {
        // 被拖的那颗也参与：它自己不动（逆质量 0），把邻居整个推开 ——
        // 这样拖到哪儿都是"实心"的，不会压着别人叠在一起。
        const ix = Math.floor(node.x / cell);
        const iy = Math.floor(node.y / cell);
        for (let dx = -1; dx <= 1; dx += 1) {
          for (let dy = -1; dy <= 1; dy += 1) {
            const bucket = cells.get(key(ix + dx, iy + dy));
            if (!bucket) continue;
            for (const other of bucket) {
              if (other === node) continue;
              let ox = other.x - node.x;
              let oy = other.y - node.y;
              const contactDist = node.r + other.r;
              const minDist = contactDist + REST_GAP;
              let dist = Math.hypot(ox, oy);
              if (dist >= minDist - OVERLAP_TOLERANCE) continue;
              if (dist < 0.001) {
                ox = (node.symbol < other.symbol ? 1 : -1) * 0.5;
                oy = 0.5;
                dist = Math.hypot(ox, oy);
              }
              const nx = ox / dist;
              const ny = oy / dist;
              // 真接触（穿透）要全量推开；只是"靠太近"（空隙不足）就软推，
              // 免得和回位弹簧互相抢，抖成一团。
              const hard = Math.max(0, contactDist - dist);
              const air = Math.max(0, minDist - dist - hard);
              const push = hard + air * REST_PUSH;
              worst = Math.max(worst, hard + air * REST_PUSH);
              // 按 1/r² 分配（大球更重，让小球让路）
              const wa = (node === sim.dragging ? DRAG_INV_MASS : 1) / (node.r * node.r);
              const wb = other === sim.dragging ? 0 : 1 / (other.r * other.r);
              const total = wa + wb || 1;
              const moveA = push * (wa / total);
              const moveB = push * (wb / total);
              node.body.setPosition(planck.Vec2(node.x - nx * moveA, node.y - ny * moveA));
              node.x -= nx * moveA;
              node.y -= ny * moveA;
              if (moveB > 0) {
                other.body.setPosition(planck.Vec2(other.x + nx * moveB, other.y + ny * moveB));
                other.x += nx * moveB;
                other.y += ny * moveB;
              }
              // 只有第一轮处理撞击（多轮会重复加冲量）：把接近速度按弹性还回去
              if (pass === 0 && hard > 0.08) {
                bouncePair(node, other, nx, ny, wa, wb, total);
              }
            }
          }
        }
      }
      if (worst <= OVERLAP_TOLERANCE) break;
    }
  }

  /** 一对球撞上了：按接近速度把冲量还给两侧（大球少动、小球弹开）。 */
  function bouncePair(a, b, nx, ny, wa, wb, total) {
    const va = a.body.getLinearVelocity();
    const vb = b.body.getLinearVelocity();
    const closing = (vb.x - va.x) * nx + (vb.y - va.y) * ny; // > 0 表示正在分开
    if (closing > -BOUNCE_MIN_SPEED) return; // 只是被慢慢挤开，没有"撞"的感觉
    const now = performance.now();
    if (!canBounce(a, b, now)) return;
    markBounce(a, b, now);
    const impulse = -closing * (1 + BOUNCE_RESTITUTION) * BOUNCE_DAMPING;
    const kickA = impulse * (wa / total);
    const kickB = impulse * (wb / total);
    // 被拖的那颗也吃一点反冲：手感上像"顶到了实心东西"，但只给 40%，免得和鼠标关节打架
    const dampA = a === sim.dragging ? 0.4 : 1;
    const dampB = b === sim.dragging ? 0.4 : 1;
    a.body.setLinearVelocity(planck.Vec2(va.x - nx * kickA * dampA, va.y - ny * kickA * dampA));
    b.body.setLinearVelocity(planck.Vec2(vb.x + nx * kickB * dampB, vb.y + ny * kickB * dampB));
    // 拖动中的那颗由指针控制，只让被撞的一侧闪
    if (a === sim.dragging) pulseHit(b);
    else if (b === sim.dragging) pulseHit(a);
    else {
      pulseHit(a);
      pulseHit(b);
    }
  }

  function canBounce(a, b, now) {
    if (!a.bumped) return true;
    const last = a.bumped.get(b.symbol) ?? 0;
    return now - last > BOUNCE_COOLDOWN;
  }

  function markBounce(a, b, now) {
    if (!a.bumped) a.bumped = new Map();
    if (!b.bumped) b.bumped = new Map();
    a.bumped.set(b.symbol, now);
    b.bumped.set(a.symbol, now);
  }

  /** 撞一下：环亮一下（220ms），让"碰撞"有可见的反馈。 */
  function pulseHit(entry) {
    if (!entry.el) return;
    entry.el.classList.add('is-hit');
    if (entry.hitTimer) window.clearTimeout(entry.hitTimer);
    entry.hitTimer = window.setTimeout(() => {
      entry.el?.classList.remove('is-hit');
      entry.hitTimer = 0;
    }, HIT_PULSE_MS);
  }

  function runSim(timestamp) {
    sim.raf = window.requestAnimationFrame(runSim);
    if (!sim.last) sim.last = timestamp;
    const elapsed = Math.min(timestamp - sim.last, 120);
    sim.last = timestamp;
    sim.acc += elapsed;
    let steps = 0;
    while (sim.acc >= FIXED_STEP_MS && steps < MAX_STEPS) {
      stepSim();
      sim.acc -= FIXED_STEP_MS;
      steps += 1;
    }
    if (steps >= MAX_STEPS) sim.acc = 0;
    if (!sim.float && !sim.dragging && steps > 0) {
      sim.activeMs += elapsed;
      let moving = false;
      for (const entry of sim.nodes) {
        const p = entry.body.getPosition();
        if (entry.restX === undefined || Math.hypot(p.x - entry.restX, p.y - entry.restY) > 0.08) {
          moving = true;
        }
        entry.restX = p.x;
        entry.restY = p.y;
      }
      sim.idleMs = moving ? 0 : sim.idleMs + elapsed;
      // 密集布局中弹簧和碰撞可能持续微调，限制无人交互时的计算时长。
      if (sim.idleMs >= 600 || sim.activeMs >= 3000) stopSim();
    }
  }

  function startSim() {
    sim.idleMs = 0;
    sim.activeMs = 0;
    if (sim.raf || !sim.world || document.hidden) return;
    sim.last = 0;
    sim.acc = 0;
    sim.raf = window.requestAnimationFrame(runSim);
  }

  function stopSim() {
    if (!sim.raf) return;
    window.cancelAnimationFrame(sim.raf);
    sim.raf = 0;
  }

  function bindBubbleDrag(selection) {
    selection.call(
      d3
        .drag()
        .clickDistance(4)
        .on('start', function onDragStart(event, node) {
          const entry = sim.bySymbol.get(node.data.symbol);
          if (!entry) return;
          sim.dragging = entry;
          sim.dragFrom = { x: entry.x, y: entry.y };
          // 抓住的是球的"某个点"，不是球心：记下指针到球心的偏移，
          // 否则鼠标一按下球心就会瞬移到指针下面（看起来像闪一下）。
          sim.grab = { x: entry.x - event.x, y: entry.y - event.y };
          sim.dragMoved = 0;
          sim.lastX = event.x;
          sim.lastY = event.y;
          sim.lastT = performance.now();
          sim.vx = 0;
          sim.vy = 0;
          // 用 Box2D 的鼠标关节抓球：关节有最大拉力和频率，球是被"拉"过去的，
          // 不会瞬移 —— 也就不会把邻居一帧弹开（那正是之前"闪现"的根因）。
          entry.body.setAwake(true);
          sim.mouseJoint = sim.world.createJoint(
            new planck.MouseJoint(
              {
                maxForce: entry.body.getMass() * DRAG_FORCE,
                frequencyHz: DRAG_FREQUENCY,
                dampingRatio: DRAG_DAMPING,
              },
              sim.ground,
              entry.body,
              planck.Vec2(event.x + sim.grab.x, event.y + sim.grab.y),
            ),
          );
          hideTooltip();
          d3.select(this).classed('is-dragging', true).raise();
          startSim();
        })
        .on('drag', function onDrag(event, node) {
          const entry = sim.bySymbol.get(node.data.symbol);
          if (!entry || sim.dragging !== entry || !sim.mouseJoint) return;
          const now = performance.now();
          const dt = Math.max(8, now - (sim.lastT || now - 16)) / 1000;
          sim.lastT = now;
          sim.vx = (event.x - (sim.lastX ?? event.x)) / dt;
          sim.vy = (event.y - (sim.lastY ?? event.y)) / dt;
          sim.lastX = event.x;
          sim.lastY = event.y;
          sim.mouseJoint.setTarget(planck.Vec2(event.x + sim.grab.x, event.y + sim.grab.y));
          sim.dragMoved = Math.max(
            sim.dragMoved,
            Math.hypot(entry.x - sim.dragFrom.x, entry.y - sim.dragFrom.y),
          );
          startSim();
        })
        .on('end', function onDragEnd(event, node) {
          const entry = sim.bySymbol.get(node.data.symbol);
          d3.select(this).classed('is-dragging', false);
          if (sim.mouseJoint) {
            sim.world.destroyJoint(sim.mouseJoint);
            sim.mouseJoint = null;
          }
          if (!entry) return;
          const clicked = sim.dragMoved < 5;
          sim.dragging = null;
          // 松手：锚点落在球自己停下的地方，所以它不会弹回布局位
          entry.homeX = entry.body.getPosition().x;
          entry.homeY = entry.body.getPosition().y;
          if (clicked) {
            entry.body.setLinearVelocity(planck.Vec2(0, 0));
            selectSymbol(node.data.symbol);
            return;
          }
          // 鼠标关节已经把球拉出了速度，这里只留一部分当作"甩出去"的惯性
          const v = entry.body.getLinearVelocity();
          const vx = v.x * THROW_SCALE;
          const vy = v.y * THROW_SCALE;
          const speed = Math.hypot(vx, vy);
          const scale = speed > THROW_MAX ? THROW_MAX / speed : 1;
          entry.body.setLinearVelocity(planck.Vec2(vx * scale, vy * scale));
          startSim();
        }),
    );
  }

  /** 稳定的伪随机：同样的标的集合 → 同样的铺法，切换窗口时不会整屏重排。 */
  function seededRandom(seedText) {
    let seed = 2166136261;
    for (let i = 0; i < seedText.length; i += 1) {
      seed ^= seedText.charCodeAt(i);
      seed = Math.imul(seed, 16777619);
    }
    return () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed / 4294967296;
    };
  }

  /**
   * 铺满整屏：半径按权重定好，先用“最远点插入”把大泡泡散开、小泡泡补缝，
   * 再跑松弛分离掉重叠。返回与 pack 叶子同形状的数组（data / x / y / r）。
   */
  function rectLeaves(nodes, width, height, scale) {
    const p = sizeExponent(nodes, scale);
    const items = nodes
      .map((node) => ({ data: node, weight: Math.pow(Math.max(node.size_value, 1), p * 2) }))
      .sort((a, b) => b.weight - a.weight);
    const rand = seededRandom(
      `${state.universe}|${state.size}|${scale}|${items.length}|${items.map((item) => item.data.symbol).join(',')}`,
    );

    const wsum = d3.sum(items, (item) => item.weight) || 1;
    const fill = 0.52; // 气泡总面积占屏幕的比例，剩下的是走道（越小越透气）
    const k = Math.sqrt((width * height * fill) / (Math.PI * wsum));
    const minR = 10;
    const maxR = Math.min(width, height) * 0.26;
    for (const item of items) {
      item.r = Math.max(minR, Math.min(maxR, k * Math.sqrt(item.weight)));
    }

    // 候选点：抖动的网格，保证“均匀铺开”而不是一坨
    const cols = Math.max(6, Math.round(Math.sqrt((items.length * width) / height)));
    const rows = Math.max(6, Math.ceil(items.length / cols));
    const cellW = width / cols;
    const cellH = height / rows;
    const candidates = [];
    for (let row = 0; row < rows; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        candidates.push({
          x: cellW * (col + 0.5) + (rand() - 0.5) * cellW * 0.72,
          y: cellH * (row + 0.5) + (rand() - 0.5) * cellH * 0.72,
          used: false,
        });
      }
    }

    const placed = [];
    for (const item of items) {
      let best = null;
      let bestScore = -Infinity;
      for (const candidate of candidates) {
        if (candidate.used) continue;
        const edge = Math.min(candidate.x, width - candidate.x, candidate.y, height - candidate.y);
        let score;
        if (!placed.length) {
          // 第一颗放在正中，避免最大的泡泡跑到角落
          score = -Math.hypot(candidate.x - width / 2, candidate.y - height / 2);
        } else {
          let distance = Infinity;
          for (const other of placed) {
            const d = Math.hypot(candidate.x - other.x, candidate.y - other.y) - other.r;
            if (d < distance) distance = d;
          }
          score = distance;
        }
        score -= Math.max(0, item.r - edge) * 1.6; // 大泡泡离边缘太近要扣分
        if (score > bestScore) {
          bestScore = score;
          best = candidate;
        }
      }
      if (!best) break;
      best.used = true;
      item.x = best.x;
      item.y = best.y;
      placed.push(item);
    }

    for (const item of items) {
      if (item.x === undefined) {
        item.x = width / 2 + (rand() - 0.5) * width * 0.8;
        item.y = height / 2 + (rand() - 0.5) * height * 0.8;
      }
    }

    relaxToRect(items, width, height);
    return items;
  }

  function relaxToRect(items, width, height) {
    const gap = 14; // 布好局时留的余量：静止时球之间就有呼吸空间，不是贴着排
    const pad = 18;
    for (let pass = 0; pass < 90; pass += 1) {
      let worst = 0;
      for (let i = 0; i < items.length; i += 1) {
        const a = items[i];
        for (let j = i + 1; j < items.length; j += 1) {
          const b = items[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let dist = Math.hypot(dx, dy);
          const limit = a.r + b.r + gap;
          if (dist >= limit) continue;
          if (dist < 0.001) {
            dx = Math.random() - 0.5;
            dy = Math.random() - 0.5;
            dist = Math.hypot(dx, dy) || 0.5;
          }
          const push = (limit - dist) * 0.5;
          const nx = dx / Math.max(dist, 0.001);
          const ny = dy / Math.max(dist, 0.001);
          worst = Math.max(worst, limit - dist);
          a.x -= nx * push;
          a.y -= ny * push;
          b.x += nx * push;
          b.y += ny * push;
        }
      }
      for (const item of items) {
        item.x = Math.max(item.r + pad, Math.min(width - item.r - pad, item.x));
        item.y = Math.max(item.r + pad, Math.min(height - item.r - pad, item.y));
      }
      if (worst < 0.6) break;
    }
  }

  function render() {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const { width, height } = stageSize();
    const nodes = snapshot.nodes || [];
    if (!nodes.length) {
      stopSim();
      sim.bySymbol.clear();
      sim.nodes = [];
      sectorLayer.selectAll('*').remove();
      sectorLabelLayer.selectAll('*').remove();
      bubbleLayer.selectAll('*').remove();
      el.empty.hidden = false;
      el.empty.textContent = '当前口径没有可用数据，换一个窗口或资产类别试试。';
      return;
    }

    const clamp = Number(state.config.color_clamp_pct) || 10;
    const grouped = state.group === 'sector';
    let sectorNodes = [];
    let leaves;
    if (grouped) {
      // 分组视图仍用 pack：每个行业一袋，方便看结构
      const layout = d3
        .pack()
        .size([width, height])
        .padding((node) => (node.depth === 1 ? 46 : 8))(buildHierarchy(nodes, state.scale));
      sectorNodes = layout.children || [];
      leaves = layout.leaves();
      fitLayout(layout, width, height);
    } else {
      // 不分组：铺满整屏，不再套成一个圆
      fitLayer.attr('transform', null);
      leaves = rectLeaves(nodes, width, height, state.scale);
    }
    const duration = prefersReducedMotion ? 0 : 520;

    // Sector envelopes ------------------------------------------------------
    const sectorSel = sectorLayer
      .selectAll('g.sector')
      .data(sectorNodes, (node) => node.data.sector)
      .join(
        (enter) => {
          const group = enter.append('g').attr('class', 'sector');
          group.append('circle').attr('class', 'sector-ring').attr('r', 0);
          return group;
        },
        (update) => update,
        (exit) => exit.remove(),
      );

    sectorSel
      .select('circle')
      .transition()
      .duration(duration)
      .ease(d3.easeCubicOut)
      .attr('cx', (node) => node.x)
      .attr('cy', (node) => node.y)
      .attr('r', (node) => node.r);

    const sectorText = (node) => `${node.data.sector} · ${node.data.children.length}`;
    const sectorVisible = (node) => (node.r > 62 ? 1 : 0);

    const sectorLabels = sectorLabelLayer
      .selectAll('g.sector-tag')
      .data(sectorNodes, (node) => node.data.sector)
      .join(
        (enter) => {
          const group = enter.append('g').attr('class', 'sector-tag');
          group.append('rect').attr('class', 'sector-pill');
          group.append('text').attr('class', 'sector-label');
          return group;
        },
        (update) => update,
        (exit) => exit.remove(),
      );

    sectorLabels
      .select('rect')
      .attr('x', (node) => node.x - (sectorText(node).length * 3.9 + 9))
      .attr('y', (node) => node.y - node.r + 7)
      .attr('width', (node) => sectorText(node).length * 7.8 + 18)
      .attr('height', 19)
      .attr('rx', 9.5)
      .style('opacity', sectorVisible);

    sectorLabels
      .select('text')
      .attr('x', (node) => node.x)
      .attr('y', (node) => node.y - node.r + 20.5)
      .style('opacity', (node) => sectorVisible(node) * 0.92)
      .text(sectorText);

    // Bubbles ---------------------------------------------------------------
    const bubbleSel = bubbleLayer
      .selectAll('g.bubble')
      .data(leaves, (node) => node.data.symbol)
      .join(
        (enter) => {
          const group = enter.append('g').attr('class', 'bubble');
          group
            .append('circle')
            .attr('class', 'halo')
            .attr('r', 0)
            .attr('fill', 'none')
            .style('pointer-events', 'none');
          group.append('circle').attr('class', 'ring').attr('r', 0).attr('fill', 'none');
          group
            .append('image')
            .attr('class', 'logo')
            .attr('opacity', 0)
            .on('error', function onLogoError() {
              const image = d3.select(this);
              const node = image.datum();
              const failed = image.attr('data-tried') || '';
              const fallback = node && node.data && node.data.logo_fallback;
              if (fallback && failed !== 'fallback') {
                image.attr('data-tried', 'fallback').attr('href', fallback);
                return;
              }
              image.attr('opacity', 0);
            });
          group.append('text').attr('class', 'symbol');
          group.append('text').attr('class', 'change');
          return group;
        },
        (update) => update,
        (exit) => exit.transition().duration(duration).style('opacity', 0).remove(),
      )
      // 小的先画、大的后画：避免邻居的光晕渗进大泡泡内部
      .sort((a, b) => a.r - b.r);

    const styleOf = (node) => ringOf(node.data.change_pct, clamp);
    // 涨跌幅文字用“环色提亮”的版本：比纯白更像数据，又比环色本身清楚
    const changeToneOf = (node) => {
      const value = node.data.change_pct;
      if (value === null || value === undefined) return 'rgba(176, 188, 205, 0.8)';
      return 'rgba(228, 235, 246, 0.76)';
    };

    // 外层微光：宽度很窄，只给边缘一点“发光”，不再是一圈糊掉的雾
    bubbleSel
      .select('circle.halo')
      .transition()
      .duration(duration)
      .ease(d3.easeCubicOut)
      .attr('r', (node) => Math.max(node.r, 1.4))
      .attr('stroke', (node) => styleOf(node).ring)
      .attr('stroke-width', (node) => Math.max(1.6, Math.min(5, node.r * 0.07)))
      .attr('stroke-opacity', (node) => 0.08 + 0.12 * styleOf(node).glow);

    // 圆环本体：玻璃质感 —— 极淡的填充 + 一条细的实色边
    bubbleSel
      .select('circle.ring')
      .transition()
      .duration(duration)
      .ease(d3.easeCubicOut)
      .attr('r', (node) => Math.max(node.r, 1.4))
      .attr('fill', (node) => styleOf(node).core)
      .attr('fill-opacity', (node) => (styleOf(node).flat ? 1 : 0.6 + 0.4 * styleOf(node).glow))
      .attr('stroke', (node) => styleOf(node).ring)
      .attr('stroke-width', (node) => Math.max(1.8, Math.min(4, node.r * 0.045)));

    const labels = new Map();
    for (const node of leaves) {
      const r = Math.max(node.r, 1.4);
      const changeText = fmtPct(node.data.change_pct);
      const hasLogo = Boolean(node.data.logo) && r >= 18;
      const showPct = r >= (hasLogo ? 26 : 22) && changeText !== '—';
      const logoSize = hasLogo ? Math.min(56, r * 0.58) : 0;
      const symbolFont = Math.max(
        8,
        Math.min(34, r * 0.36, fitFont(node.data.symbol, r, hasLogo ? 0.94 : 0.96)),
      );
      const changeFont = Math.max(8, Math.min(15, r * 0.22, fitFont(changeText, r, 0.92)));
      const showSymbol = r >= 10;
      let symbolY = 0;
      let changeY = 0;
      if (hasLogo && showPct) {
        symbolY = r * 0.1;
        changeY = r * 0.42;
      } else if (hasLogo) {
        symbolY = r * 0.34;
      } else if (showPct) {
        symbolY = -r * 0.2;
        changeY = r * 0.26;
      }
      labels.set(node.data.symbol, {
        hasLogo,
        logoSize,
        logoY: hasLogo ? (showPct ? -r * 0.28 : -r * 0.16) : 0,
        showSymbol,
        showPct,
        symbolFont,
        changeFont,
        symbolY,
        changeY,
      });
    }

    bubbleSel
      .select('image.logo')
      .attr('x', (node) => -labels.get(node.data.symbol).logoSize / 2)
      .attr('y', (node) => {
        const label = labels.get(node.data.symbol);
        return label.logoY - label.logoSize / 2;
      })
      .attr('width', (node) => labels.get(node.data.symbol).logoSize)
      .attr('height', (node) => labels.get(node.data.symbol).logoSize)
      .attr('href', (node) => (labels.get(node.data.symbol).hasLogo ? node.data.logo : null))
      .attr('data-tried', null)
      .attr('opacity', (node) => (labels.get(node.data.symbol).hasLogo ? 1 : 0));


    bubbleSel
      .select('text.symbol')
      .attr('x', 0)
      .attr('y', (node) => labels.get(node.data.symbol).symbolY)
      .attr('font-size', (node) => labels.get(node.data.symbol).symbolFont)
      .style('display', (node) => (labels.get(node.data.symbol).showSymbol ? null : 'none'))
      .text((node) => node.data.symbol);

    bubbleSel
      .select('text.change')
      .attr('x', 0)
      .attr('y', (node) => labels.get(node.data.symbol).changeY)
      .attr('font-size', (node) => labels.get(node.data.symbol).changeFont)
      .attr('fill', (node) => changeToneOf(node))
      .style('display', (node) => (labels.get(node.data.symbol).showPct ? null : 'none'))
      .text((node) => fmtPct(node.data.change_pct));

    bubbleSel
      .on('mouseenter', (event, node) => showTooltip(event, node.data))
      .on('mousemove', (event) => moveTooltip(event))
      .on('mouseleave', hideTooltip);

    bindBubbleDrag(bubbleSel);

    // 位置交给物理引擎：布局只给初始位置，之后由碰撞 + 拖动决定，松手不回弹
    sim.width = width;
    sim.height = height;
    const layoutKey = `${state.universe}|${state.window}|${state.size}|${state.scale}|${state.group}|${width}x${height}`;
    const relayout = layoutKey !== sim.layoutKey;
    sim.layoutKey = layoutKey;
    syncSim(leaves, relayout);
    bubbleSel.each(function attachSimElement(node) {
      const entry = sim.bySymbol.get(node.data.symbol);
      if (entry) entry.el = this;
    });
    paintSim();
    startSim();

    applySearchHighlight();
    if (state.selected) paintSelection();
  }

  /* --------------------------------------------------------------- tooltip */

  function tooltipRow(label, value) {
    return `<div class="tip-row"><span>${label}</span><span>${value}</span></div>`;
  }

  function showTooltip(event, node) {
    // 抽屉打开时不再弹悬浮提示：它会被面板压住一半，也抢走注意力。
    if (state.selected) return;
    if (sim.dragging) return;
    const windowLabel = windowLabelOf(state.window);
    const sector = node.sector ? `${node.sector} · ` : '';
    const rows = [
      tooltipRow(windowLabel, fmtPct(node.change_pct)),
      tooltipRow('价格', fmtPrice(node.price)),
      tooltipRow(basisLabel(), fmtCompact(node.size_value)),
      tooltipRow('占比', `${(node.share * 100).toFixed(2)}%`),
    ];
    el.tooltip.innerHTML = `
      <div class="tip-name">${escapeHTML(node.name || node.symbol)}</div>
      <div class="tip-sub">${escapeHTML(sector)}${escapeHTML(node.symbol)} · 排名 ${node.rank}</div>
      ${rows.join('')}`;
    el.tooltip.hidden = false;
    moveTooltip(event);
  }

  function moveTooltip(event) {
    if (sim.dragging) {
      hideTooltip();
      return;
    }
    const pad = 14;
    const rect = el.tooltip.getBoundingClientRect();
    let left = event.clientX + pad;
    let top = event.clientY + pad;
    if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - pad;
    if (top + rect.height > window.innerHeight - 8) top = event.clientY - rect.height - pad;
    el.tooltip.style.left = `${Math.max(8, left)}px`;
    el.tooltip.style.top = `${Math.max(8, top)}px`;
  }

  function hideTooltip() {
    el.tooltip.hidden = true;
  }

  function escapeHTML(text) {
    return String(text).replace(/[&<>"']/g, (char) => {
      switch (char) {
        case '&':
          return '&amp;';
        case '<':
          return '&lt;';
        case '>':
          return '&gt;';
        case '"':
          return '&quot;';
        default:
          return '&#39;';
      }
    });
  }

  function windowLabelOf(value) {
    const option = state.config.windows.find((item) => item.value === value);
    return option ? option.label : value;
  }

  /* ---------------------------------------------------------------- search */

  function applySearchHighlight() {
    const query = state.query.trim().toLowerCase();
    bubbleLayer.selectAll('g.bubble').classed('is-match', (node) => {
      if (!query) return false;
      const haystack = `${node.data.symbol} ${node.data.name}`.toLowerCase();
      return haystack.includes(query);
    });
    bubbleLayer.selectAll('g.bubble').classed('is-dim', (node) => {
      if (!query) return false;
      const haystack = `${node.data.symbol} ${node.data.name}`.toLowerCase();
      return !haystack.includes(query);
    });
  }

  function firstMatch() {
    const query = state.query.trim().toLowerCase();
    if (!query || !state.snapshot) return null;
    return (
      state.snapshot.nodes.find((node) =>
        `${node.symbol} ${node.name}`.toLowerCase().includes(query),
      ) || null
    );
  }

  function paintSelection() {
    bubbleLayer
      .selectAll('g.bubble')
      .classed('is-selected', (node) => node.data.symbol === state.selected);
  }

  /* ---------------------------------------------------------------- detail */

  function nodeBySymbol(symbol) {
    if (!state.snapshot) return null;
    return state.snapshot.nodes.find((node) => node.symbol === symbol) || null;
  }

  async function selectSymbol(symbol, options = {}) {
    const node = nodeBySymbol(symbol);
    if (!node) return;
    state.selected = symbol;
    paintSelection();
    el.detail.hidden = false;
    el.stage.classList.add('has-detail');
    hideTooltip();
    el.detailSymbol.textContent = node.symbol;
    el.detailName.textContent = node.name || node.symbol;
    el.detailSector.textContent = `${node.sector || '未分类'} · 排名 ${node.rank}/${state.snapshot.stats.returned}`;
    el.detailPrice.textContent = fmtPrice(node.price);
    el.detailCurrency.textContent = node.currency;
    el.detailPrice.className = `detail-price ${toneOf(node.change_pct)}`;
    const windowChange = (node.changes || {})[state.window];
    el.detailChange.textContent = windowChange === null || windowChange === undefined
      ? '—'
      : `${windowChange > 0 ? '+' : ''}${windowChange.toFixed(2)}%`;
    el.detailChange.className = `detail-change ${toneOf(windowChange)}`;
    const basisText = state.size === 'volume' ? '24h 成交额' : '市值';
    el.detailFoot.textContent = `${windowLabelOf(state.window)} · ${basisText}口径 · 更新于 ${fmtClock(state.snapshot.generated_at)}`;

    const allowedWindows = (currentUniverse()?.windows || []).slice();
    const windowRows = Object.entries(node.changes || {})
      .map(([key, value]) => ({
        key,
        label: windowLabelOf(key),
        value,
      }))
      .filter(
        (row) =>
          state.config.windows.some((item) => item.value === row.key) &&
          (!allowedWindows.length || allowedWindows.includes(row.key)),
      );
    el.detailWindows.innerHTML = windowRows
      .map(
        (row) =>
          `<div><dt>${escapeHTML(row.label)}</dt><dd class="${toneOf(row.value)}">${fmtPct(row.value)}</dd></div>`,
      )
      .join('');

    el.detailFacts.innerHTML = [
      [basisLabel(), fmtCompact(node.size_value)],
      ['占比', `${(node.share * 100).toFixed(2)}%`],
      ['数据源', escapeHTML(node.source || '—')],
      ['质量', qualityLabel(node.quality)],
    ]
      .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`)
      .join('');

    syncSegmented(el.timeframes, state.timeframe);
    await loadHistory(node.symbol, options.keepDetail === true);
    syncURL();
  }

  function qualityLabel(quality) {
    switch (quality) {
      case 'ok':
        return '正常';
      case 'partial':
        return '部分数据';
      case 'stale':
        return '缓存数据';
      default:
        return escapeHTML(quality || '—');
    }
  }

  function closeDetail() {
    historyToken += 1;
    historyRequest?.abort();
    state.selected = null;
    el.detail.hidden = true;
    el.stage.classList.remove('has-detail');
    paintSelection();
    hideTooltip();
    syncURL();
  }

  async function loadHistory(symbol, quiet) {
    const universe = state.universe;
    const timeframe = state.timeframe;
    const key = `${universe}:${symbol}:${timeframe}`;
    if (historyRequestKey === key && !historyRequest?.signal.aborted) return;
    const token = ++historyToken;
    historyRequest?.abort();
    historyRequest = new AbortController();
    historyRequestKey = null;
    const current = () => token === historyToken && state.universe === universe
      && state.selected === symbol && state.timeframe === timeframe;
    const cached = state.historyCache.get(key);
    const age = cached ? Date.now() - cached.storedAt : Infinity;
    const showCached = cached && age < HISTORY_STALE_MS;
    if (showCached) {
      paintHistory(cached.series);
      if (age < HISTORY_TTL_MS) return;
      el.sparkMeta.append(' · 缓存走势，更新中…');
    } else if (!quiet) {
      el.sparkMeta.textContent = '加载走势…';
      drawSparkline([]);
    }
    const waitSeconds = Math.ceil(((state.historyCooldowns.get(universe) || 0) - Date.now()) / 1000);
    if (waitSeconds > 0) {
      const message = `行情源限流，请在 ${waitSeconds} 秒后重试`;
      if (showCached) {
        paintHistory(cached.series);
        el.sparkMeta.append(` · 缓存走势，${message}`);
      } else {
        el.sparkMeta.textContent = message;
        drawSparkline([]);
      }
      return;
    }
    historyRequestKey = key;
    try {
      const params = new URLSearchParams({ universe, symbol, timeframe });
      const series = await fetchJSON(`${API.history}?${params.toString()}`, {
        signal: historyRequest.signal,
      });
      if (!current()) return;
      state.historyCache.delete(key);
      state.historyCache.set(key, { series, storedAt: Date.now() });
      while (state.historyCache.size > HISTORY_CACHE_LIMIT) {
        state.historyCache.delete(state.historyCache.keys().next().value);
      }
      paintHistory(series);
    } catch (error) {
      if (!current() || error.name === 'AbortError') return;
      if (error.code === 'provider_rate_limited') {
        state.historyCooldowns.set(universe, Date.now() + error.retryAfter * 1000);
      }
      if (showCached) {
        paintHistory(cached.series);
        el.sparkMeta.append(' · 更新失败，显示缓存走势');
      } else {
        el.sparkMeta.textContent = `走势不可用：${error.message}`;
        drawSparkline([]);
      }
    } finally {
      if (token === historyToken) historyRequestKey = null;
    }
  }

  function paintHistory(series) {
    drawSparkline(series.points || []);
    const points = series.points || [];
    if (points.length >= 2) {
      const first = points[0].c;
      const last = points[points.length - 1].c;
      const change = first ? ((last - first) / first) * 100 : null;
      el.sparkMeta.innerHTML = `${escapeHTML(series.source || '')} · ${points.length} 点 · <span class="${toneOf(change)}">${fmtPct(change)}</span>`;
    } else {
      el.sparkMeta.textContent = '暂无该时间范围的走势';
    }
  }

  function drawSparkline(points) {
    const line = el.sparkline.select('.spark-line');
    const area = el.sparkline.select('.spark-area');
    if (!points || points.length < 2) {
      line.attr('d', null);
      area.attr('d', null);
      return;
    }
    const width = 320;
    const height = 96;
    const xs = d3
      .scaleLinear()
      .domain(d3.extent(points, (point) => point.t))
      .range([2, width - 2]);
    const extent = d3.extent(points, (point) => point.c);
    const pad = (extent[1] - extent[0]) * 0.12 || Math.abs(extent[1]) * 0.02 || 1;
    const ys = d3
      .scaleLinear()
      .domain([extent[0] - pad, extent[1] + pad])
      .range([height - 4, 4]);

    const path = d3
      .line()
      .x((point) => xs(point.t))
      .y((point) => ys(point.c))
      .curve(d3.curveMonotoneX);
    const last = points[points.length - 1];
    const first = points[0];
    const rising = last.c >= first.c;
    line.attr('d', path(points)).attr('stroke', rising ? '#3ddc97' : '#ff5f6d');
    area
      .attr('d', `${path(points)} L ${xs(last.t)} ${height} L ${xs(first.t)} ${height} Z`)
      .attr('fill', rising ? 'rgba(61,220,151,0.16)' : 'rgba(255,95,109,0.16)');
  }

  /* ---------------------------------------------------------------- status */

  function updateGroupHint() {
    const nodes = state.snapshot?.nodes || [];
    if (!nodes.length) return;
    const mapped = nodes.filter((node) => node.sector).length;
    const ratio = Math.round((mapped / nodes.length) * 100);
    const button = el.groups.querySelector('button[data-group="sector"]');
    if (!button) return;
    button.title = `按行业分组：${nodes.length} 个标的中 ${mapped} 个已归类（${ratio}%），其余归入「未分类」`;
  }

  function updateStatus() {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const stats = snapshot.stats;
    // 请求 20 但只有 14 颗时，把「为什么少」直接写在状态栏，别让人以为是自己设错了
    const requested = Number(state.limit) > 0 ? Number(state.limit) : 0;
    const shortfall =
      requested > 0 && stats.returned < requested
        ? `（请求 ${requested}，上游可用 ${stats.returned}）`
        : '';
    el.statusMeta.textContent = `${snapshot.universe.name} · ${windowLabelOf(snapshot.window)} · 气泡 ${stats.returned} 个 · 上游 ${stats.provider_quotes} 条 · 覆盖率 ${Math.round(stats.coverage * 100)}%${shortfall}`;
    const warnings = snapshot.warnings || [];
    el.statusWarnings.textContent = warnings.length
      ? warnings.map((warning) => warningText(warning)).join(' · ')
      : '';
    el.statusWarnings.title = warnings.map((warning) => warning.message).join('\n');
    el.statusUpdated.textContent = `更新于 ${fmtClock(snapshot.generated_at)}`;
    const stale = warnings.some((warning) => warning.code === 'stale_cache');
    el.statusUpdated.classList.toggle('down', stale);
  }

  function tick() {
    if (!state.config) return;
    if (document.hidden || state.loading) {
      el.countdown.textContent = '';
      return;
    }
    state.remaining -= 1;
    if (state.remaining <= 0) {
      state.remaining = Number(state.config.auto_refresh_seconds) || 60;
      void loadSnapshot();
      return;
    }
    el.countdown.textContent = `${state.remaining}s`;
  }

  /* ------------------------------------------------------------------- url */

  function syncURL() {
    const params = new URLSearchParams();
    if (state.universe) params.set('universe', state.universe);
    params.set('window', state.window);
    if (state.size !== DEFAULT_SIZE) params.set('size', state.size);
    if (state.scale !== DEFAULT_SCALE) params.set('scale', String(state.scale));
    if (state.group !== 'none') params.set('group', state.group);
    if (state.limit !== DEFAULT_LIMIT) params.set('limit', String(state.limit));
    if (state.query) params.set('q', state.query);
    if (state.selected) params.set('symbol', state.selected);
    const url = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState(null, '', url);
  }

  function restoreFromURL() {
    const params = new URLSearchParams(window.location.search);
    const universe = params.get('universe');
    if (universe && state.universes.some((item) => item.id === universe)) {
      state.universe = universe;
    }
    const universeMeta = currentUniverse();
    const windowParam = params.get('window');
    if (windowParam && state.config.windows.some((item) => item.value === windowParam)) {
      state.window = windowParam;
    } else if (universeMeta) {
      state.window = universeMeta.default_window;
    }
    const sizeParam = params.get('size');
    if (sizeParam && state.config.size_bases.some((item) => item.value === sizeParam)) {
      state.size = sizeParam;
    }
    const scaleParam = Number(params.get('scale'));
    if (SCALE_OPTIONS.includes(scaleParam)) state.scale = scaleParam;
    if (params.get('group') === 'sector') state.group = 'sector';
    const limitParam = params.has('limit') ? Number(params.get('limit')) : null;
    if (limitParam !== null && Number.isInteger(limitParam) && limitParam >= 0) {
      state.limit = limitParam;
    }
    const query = params.get('q');
    if (query) {
      state.query = query;
      el.search.value = query;
    }
    const symbol = params.get('symbol');
    if (symbol) state.selected = symbol;
  }

  /* ------------------------------------------------------------------ zoom */

  function setupZoom() {
    zoomBehaviour = d3
      .zoom()
      .scaleExtent([1, 12])
      // 画布只接受滚轮缩放；鼠标和触摸拖动仅由气泡自身处理。
      .filter((event) => event.type === 'wheel')
      .on('zoom', (event) => {
        lastZoom = event.transform;
        root.attr('transform', event.transform);
      });

    el.svg.call(zoomBehaviour).on('dblclick.zoom', null);
    el.svg.on('dblclick', () => {
      el.svg.transition().duration(320).call(zoomBehaviour.transform, d3.zoomIdentity);
    });
  }

  function syncScaleButtons() {
    syncSegmented(el.scales, state.scale);
    syncSegmented(el.groups, state.group);
    renderLimitPicker();
  }

  /* ------------------------------------------------------- 数量弹窗（picker） */

  function maxNodes() {
    return Number(state.config?.max_nodes) || 400;
  }

  function limitLabel(limit) {
    return Number(limit) > 0 ? String(limit) : '全部';
  }

  // 自定义过的数量存本地，下次打开弹窗就能一键选回来（属于"页面上的常用数字"）
  const LIMIT_STORE_KEY = 'bubbleboard.customLimits';
  const LIMIT_CUSTOM_MAX = 4;

  function loadCustomLimits() {
    try {
      const raw = window.localStorage.getItem(LIMIT_STORE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return [];
      return parsed
        .map((value) => Math.round(Number(value)))
        .filter((value) => Number.isFinite(value) && value > 0)
        .slice(0, LIMIT_CUSTOM_MAX);
    } catch {
      return [];
    }
  }

  function rememberLimit(value) {
    if (!(value > 0) || LIMIT_PRESETS.includes(value)) return;
    const custom = loadCustomLimits().filter((item) => item !== value);
    custom.unshift(value);
    try {
      window.localStorage.setItem(LIMIT_STORE_KEY, JSON.stringify(custom.slice(0, LIMIT_CUSTOM_MAX)));
    } catch {
      // 隐私模式下 localStorage 可能不可写，忽略即可
    }
  }

  function limitChoices() {
    const custom = loadCustomLimits().filter((value) => !LIMIT_PRESETS.includes(value));
    const all = [...LIMIT_PRESETS];
    // 「全部」永远排最后
    const tail = all.pop();
    return [...all, ...custom, tail];
  }

  function renderLimitPicker() {
    const limit = Number(state.limit);
    el.limitValue.textContent = limitLabel(limit);
    const cap = maxNodes();
    el.limitInput.max = String(cap);
    el.limitInput.placeholder = `自定义数量 · 最多 ${cap}`;
    el.limitPresets.replaceChildren(
      ...limitChoices().map((value) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.limit = String(value);
        button.textContent = limitLabel(value);
        button.setAttribute('aria-pressed', String(value === limit));
        return button;
      }),
      plusButton(),
    );
  }

  function plusButton() {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.limit = '+';
    button.textContent = '+';
    button.setAttribute('aria-label', '自定义数量');
    return button;
  }

  function openCustomLimit() {
    el.limitPresets.hidden = true;
    el.limitForm.hidden = false;
    el.limitInput.value = Number(state.limit) > 0 ? String(state.limit) : '';
    window.requestAnimationFrame(() => {
      el.limitInput.focus({ preventScroll: true });
    });
  }

  function closeCustomLimit() {
    el.limitForm.hidden = true;
    el.limitPresets.hidden = false;
    el.limitInput.value = '';
  }

  function openLimitPicker() {
    el.limitPop.hidden = false;
    el.limitTrigger.setAttribute('aria-expanded', 'true');
    renderLimitPicker();
    closeCustomLimit();
    window.requestAnimationFrame(() => {
      const active = el.limitPresets.querySelector('[aria-pressed="true"]');
      if (active) active.focus({ preventScroll: true });
    });
  }

  function closeLimitPicker() {
    el.limitPop.hidden = true;
    el.limitTrigger.setAttribute('aria-expanded', 'false');
    closeCustomLimit();
  }

  function isLimitPickerOpen() {
    return !el.limitPop.hidden;
  }

  function applyLimit(raw) {
    const cap = maxNodes();
    const value = Math.round(Number(raw));
    if (!Number.isFinite(value)) {
      toast('数量得是一个整数');
      return;
    }
    if (value < 0) {
      toast('数量不能是负数');
      return;
    }
    const next = value > cap ? cap : value;
    if (value > cap) toast(`最多 ${cap} 个，已按上限显示`);
    closeLimitPicker();
    if (value <= cap) rememberLimit(next);
    if (next === state.limit) {
      renderLimitPicker();
      return;
    }
    state.limit = next;
    renderLimitPicker();
    void loadSnapshot();
    syncURL();
  }

  /* ------------------------------------------------------------------ boot */

  function wireEvents() {
    const controlsToggle = $('controls-toggle');
    const controls = $('view-controls');
    let controlsAnimation = null;
    const collapsedControls = {
      height: '0px', minHeight: '0px', paddingTop: '0px', paddingBottom: '0px',
      borderBottomWidth: '0px', opacity: '0',
    };
    const controlsFrame = () => {
      const style = window.getComputedStyle(controls);
      return Object.fromEntries(Object.keys(collapsedControls).map((key) => [
        key, key === 'height' ? `${controls.getBoundingClientRect().height}px` : style[key],
      ]));
    };
    controlsToggle.addEventListener('click', () => {
      const expanded = controlsToggle.getAttribute('aria-expanded') !== 'true';
      const from = controls.classList.contains('is-expanded') ? controlsFrame() : collapsedControls;
      controlsAnimation?.cancel();
      controlsToggle.setAttribute('aria-expanded', String(expanded));
      controlsToggle.textContent = expanded ? '收起筛选' : '筛选设置';
      if (prefersReducedMotion) {
        controls.classList.toggle('is-expanded', expanded);
        controls.style.overflow = '';
        return;
      }
      // 先测量自然高度，再在当前高度与目标之间过渡；快速连点也从当前帧继续。
      controls.classList.add('is-expanded');
      const to = expanded ? controlsFrame() : collapsedControls;
      controls.style.overflow = 'hidden';
      controlsAnimation = controls.animate([from, to], {
        duration: 260,
        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
        fill: 'both',
      });
      controlsAnimation.onfinish = () => {
        controls.classList.toggle('is-expanded', expanded);
        controlsAnimation.cancel();
        controlsAnimation = null;
        // 展开后恢复溢出，避免数量选择弹窗被裁切。
        controls.style.overflow = '';
      };
    });
    el.refresh.addEventListener('click', () => {
      state.historyCache.clear();
      void loadSnapshot();
    });

    el.relayout.addEventListener('click', () => {
      // 强制重新排布：拖动过的气泡回到布局给的位置
      sim.layoutKey = '';
      render();
    });

    el.scales.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-scale]');
      if (!button) return;
      const next = Number(button.dataset.scale);
      if (state.scale === next) return;
      state.scale = next;
      syncScaleButtons();
      render();
      syncURL();
    });

    el.groups.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-group]');
      if (!button || state.group === button.dataset.group) return;
      state.group = button.dataset.group;
      syncScaleButtons();
      render();
      syncURL();
    });

    el.limitTrigger.addEventListener('click', () => {
      if (isLimitPickerOpen()) closeLimitPicker();
      else openLimitPicker();
    });

    el.limitPresets.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-limit]');
      if (!button) return;
      if (button.dataset.limit === '+') {
        openCustomLimit();
        return;
      }
      applyLimit(button.dataset.limit);
    });

    el.limitForm.addEventListener('submit', (event) => {
      event.preventDefault();
      applyLimit(el.limitInput.value);
    });

    el.limitInput.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      closeCustomLimit();
      window.requestAnimationFrame(() => el.limitTrigger.focus({ preventScroll: true }));
    });

    document.addEventListener('pointerdown', (event) => {
      if (!isLimitPickerOpen()) return;
      if (el.limitPop.contains(event.target) || el.limitTrigger.contains(event.target)) return;
      closeLimitPicker();
    });

    let searchTimer = 0;
    el.search.addEventListener('input', () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.query = el.search.value.trim();
        applySearchHighlight();
        syncURL();
      }, 120);
    });

    el.search.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      window.clearTimeout(searchTimer);
      state.query = el.search.value.trim();
      applySearchHighlight();
      const match = firstMatch();
      if (match) {
        selectSymbol(match.symbol);
        el.search.blur();
      }
    });

    el.detailClose.addEventListener('click', closeDetail);

    el.svg.on('click', (event) => {
      if (event.target.tagName === 'svg') closeDetail();
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (isLimitPickerOpen()) closeLimitPicker();
        closeDetail();
      }
      if (event.key === '/' && document.activeElement !== el.search) {
        event.preventDefault();
        el.search.focus();
      }
    });

    el.timeframes.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button || !state.selected) return;
      state.timeframe = button.dataset.value;
      syncSegmented(el.timeframes, state.timeframe);
      void loadHistory(state.selected, false);
    });

    let resizeTimer = 0;
    const resize = new ResizeObserver(() => {
      if (!state.snapshot) return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => render(), 120);
    });
    resize.observe(el.stage);

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopSim();
      else startSim();
    });

    window.setInterval(tick, 1000);
  }

  async function boot() {
    adoptSegmentValues(el.scales, 'scale');
    adoptSegmentValues(el.groups, 'group');
    segmented(el.timeframes, TIMEFRAMES, state.timeframe, () => {});
    setupZoom();
    try {
      const [config, universes] = await Promise.all([
        fetchJSON(API.config),
        fetchJSON(API.universes),
      ]);
      state.config = config;
      const versionLabel = $('status-version');
      versionLabel.textContent = config.version
        ? `v${config.version}${config.revision ? ` · ${config.revision.slice(0, 7)}` : ''}` : '';
      versionLabel.title = config.revision ? `部署提交：${config.revision}` : '未提供部署提交信息';
      state.universes = universes;
      document.title = `${config.title} · 资产气泡图`;
      if (!state.universe) state.universe = universes[0] ? universes[0].id : null;
      restoreFromURL();
      if (!state.universe) throw new Error('没有可用的资产类别');
      const meta = currentUniverse();
      if (meta && !meta.windows.includes(state.window)) state.window = meta.default_window;

      renderUniverseTabs();
      renderWindowButtons();
      renderBaseButtons();
      syncScaleButtons();
      renderLegend();
      wireEvents();
      state.remaining = Number(config.auto_refresh_seconds) || 60;
      await loadSnapshot();
      if (state.selected) await selectSymbol(state.selected, { keepDetail: true });
    } catch (error) {
      el.loading.hidden = true;
      el.empty.hidden = false;
      el.empty.textContent = `初始化失败：${error.message}`;
      toast(`初始化失败：${error.message}`);
    }
  }

  void boot();
})();

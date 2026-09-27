/* Houston 1947 zoning story — app.js
 * Loads the data contract (meta, palette, results, lookup, extent), builds
 * five MapLibre maps, a scrollytelling sequence, a swipe comparison, an
 * agreement map with click lookups, and four D3 charts.
 */
(() => {
  'use strict';

  const DATA = 'data/';
  // Tiles ship as one PMTiles archive per layer. Locally that's the docs/tiles
  // folder served over plain HTTP; in production it's a Cloudflare R2 bucket
  // (R2 must serve HTTP Range requests, which PMTiles relies on).
  const TILE_BASE = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'tiles/'
    : 'https://pub-f0e803f2490142f29d397852dea3dcfa.r2.dev/';
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Register the pmtiles:// protocol so MapLibre can read tiles directly out
  // of a single .pmtiles archive via HTTP range requests.
  if (typeof pmtiles !== 'undefined' && typeof maplibregl !== 'undefined') {
    const pmtilesProtocol = new pmtiles.Protocol();
    maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile);
  } else {
    console.warn('[houston1947] pmtiles library not loaded — raster overlays will not render');
  }

  const FALLBACK_PALETTE = {
    lu2026: {},
    zones1947: {},
    agree: { strict: '#2a78d6', cumulative: '#86b6ef', mismatch: '#e34948', neutral: '#c3c2b7' },
    groups: {
      Residential: '#2a78d6',
      Commercial: '#eb6834',
      Industrial: '#1baf7a',
      'Public/Park': '#eda100',
      Neutral: '#898781'
    }
  };

  const ZONE_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'];

  // ---------------------------------------------------------------- utils

  async function fetchJSON(path, fallback) {
    try {
      const res = await fetch(path);
      if (!res.ok) throw new Error(res.status + ' ' + path);
      return await res.json();
    } catch (err) {
      console.warn('[houston1947] could not load', path, err.message);
      return fallback;
    }
  }

  function b64ToUint8(b64) {
    try {
      const bin = atob(b64);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      return arr;
    } catch (e) {
      return new Uint8Array(0);
    }
  }

  function lngLatToMeters(lng, lat) {
    const R = 6378137;
    const x = (lng * Math.PI / 180) * R;
    const y = Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI / 180) / 2)) * R;
    return [x, y];
  }

  function fmtPct(v) {
    return (v === null || v === undefined || Number.isNaN(v)) ? '—' : Math.round(v) + '%';
  }

  function currentDark() {
    const forced = document.body.getAttribute('data-theme');
    if (forced === 'dark') return true;
    if (forced === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  // ---------------------------------------------------------------- theme

  function initTheme() {
    const stored = localStorage.getItem('h1947-theme');
    if (stored === 'dark' || stored === 'light') {
      document.body.setAttribute('data-theme', stored);
    }
    const btn = document.getElementById('theme-toggle');
    btn.addEventListener('click', () => {
      const dark = currentDark();
      const next = dark ? 'light' : 'dark';
      document.body.setAttribute('data-theme', next);
      try { localStorage.setItem('h1947-theme', next); } catch (e) {}
      onThemeChange();
    });
  }

  const themeListeners = [];
  function onThemeChange() { themeListeners.forEach((fn) => { try { fn(currentDark()); } catch (e) { console.warn(e); } }); }

  // ---------------------------------------------------------------- app state
  const App = {
    meta: null,
    palette: FALLBACK_PALETTE,
    results: null,
    lookup: null,
    extent: null
  };

  function groupColor(name) {
    return (App.palette.groups && App.palette.groups[name]) || FALLBACK_PALETTE.groups[name] || '#898781';
  }
  function zoneColor(code) {
    return (App.palette.zones1947 && App.palette.zones1947[code]) || '#898781';
  }
  function luColor(name) {
    return (App.palette.lu2026 && App.palette.lu2026[name]) || '#898781';
  }

  // ---------------------------------------------------------------- basemap + style building

  // Basemap: OpenFreeMap vector styles (free, no API key; OSM data). Both themes are
  // fetched once in main() so buildStyle() can stay synchronous.
  const BASEMAP_STYLE_URLS = {
    light: 'https://tiles.openfreemap.org/styles/positron',
    dark: 'https://tiles.openfreemap.org/styles/dark'
  };
  App.baseStyles = { light: null, dark: null };

  // Reads either the new meta.json shape —
  //   {"tiles": {"scan": {"file": "scan.pmtiles", "format": "webp", "minzoom": 10, "maxzoom": 16}, ...}}
  // — or the older per-layer zooms/tile_formats shape, and tolerates either.
  function layerMeta(kind) {
    const tilesMeta = App.meta && App.meta.tiles && App.meta.tiles[kind];
    if (tilesMeta) {
      return {
        file: tilesMeta.file || `${kind}.pmtiles`,
        minzoom: tilesMeta.minzoom != null ? tilesMeta.minzoom : 10,
        maxzoom: tilesMeta.maxzoom != null ? tilesMeta.maxzoom : (kind === 'scan' ? 16 : 15)
      };
    }
    // Old shape fallback (pre-PMTiles contract): zooms.<kind> + tile_formats.<kind>.
    const zooms = (App.meta && App.meta.zooms && App.meta.zooms[kind]) || {};
    return {
      file: `${kind}.pmtiles`,
      minzoom: zooms.min != null ? zooms.min : 10,
      maxzoom: zooms.max != null ? zooms.max : (kind === 'scan' ? 16 : 15)
    };
  }

  function overlaySource(kind) {
    const lm = layerMeta(kind);
    const archiveUrl = TILE_BASE + lm.file;
    return {
      type: 'raster',
      url: 'pmtiles://' + archiveUrl,
      tileSize: 256,
      minzoom: lm.minzoom,
      maxzoom: lm.maxzoom
    };
  }

  // Builds a complete MapLibre style with the basemap plus a chosen set of
  // raster overlays. `overlayState` is {kind: opacity} — layers are baked in
  // with their current opacity so a theme change (which needs setStyle) does
  // not lose scroll/crossfade/toggle progress.
  function buildStyle(dark, overlayKinds, overlayState) {
    const base = App.baseStyles[dark ? 'dark' : 'light'];
    // Deep-copy so each map/theme switch gets its own style object.
    const style = base ? JSON.parse(JSON.stringify(base))
                       : { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': dark ? '#1b1b1b' : '#f2f0eb' } }] };
    // The basemap style ships its own default camera; drop it so it can't override
    // each map's initial view (fitBounds / fly-to) or reset the view on theme change.
    delete style.center; delete style.zoom; delete style.bearing; delete style.pitch;
    // Insert the data overlays just below the basemap's first label (symbol) layer, so
    // labels draw fully on top with their halos instead of peeking through gaps.
    let insertAt = style.layers.findIndex((l) => l.type === 'symbol');
    if (insertAt < 0) insertAt = style.layers.length;
    const overlays = overlayKinds.map((kind) => {
      style.sources[kind] = overlaySource(kind);
      return {
        id: 'layer-' + kind,
        type: 'raster',
        source: kind,
        paint: { 'raster-opacity': overlayState[kind] != null ? overlayState[kind] : 0, 'raster-fade-duration': 0 }
      };
    });
    style.layers.splice(insertAt, 0, ...overlays);
    return style;
  }

  function addExtentOutline(map) {
    if (!App.extent) return;
    if (map.getSource('extent')) return;
    map.addSource('extent', { type: 'geojson', data: App.extent });
    map.addLayer({
      id: 'extent-line',
      type: 'line',
      source: 'extent',
      paint: { 'line-color': currentDark() ? '#ffffff' : '#0b0b0b', 'line-width': 2, 'line-opacity': 0.6, 'line-dasharray': [2, 2] }
    });
  }

  function boundsFromArray(b) {
    // [w, s, e, n]
    return [[b[0], b[1]], [b[2], b[3]]];
  }

  // Start every map on the study area (constructor option), so the view doesn't
  // depend on catching 'style.load' after the constructor returns.
  function initialView() {
    return (App.meta && App.meta.scan_bounds)
      ? { bounds: boundsFromArray(App.meta.scan_bounds), fitBoundsOptions: { padding: 0 } }
      : { center: [-95.35, 29.75], zoom: 11 };
  }

  // ---------------------------------------------------------------- hero map

  function initHeroMap() {
    const state = { scan: 0.9 };
    const map = new maplibregl.Map({
      container: 'map',
      ...initialView(),
      style: buildStyle(currentDark(), ['scan'], state),
      interactive: false,
      attributionControl: false
    });
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    // Use 'style.load' (fires once the style JSON is applied) rather than
    // 'load' (which waits on every source, including tile fetches) so a
    // missing/slow PMTiles archive can't block fitBounds indefinitely.
    map.once('style.load', () => {
      if (App.meta && App.meta.scan_bounds) {
        map.fitBounds(boundsFromArray(App.meta.scan_bounds), { padding: 0, animate: false });
      }
    });
    themeListeners.push((dark) => {
      map.setStyle(buildStyle(dark, ['scan'], state));
    });
    return map;
  }

  // ---------------------------------------------------------------- scrollytelling map (map2)

  function initScrollyMap() {
    const state = { scan: 1, zones1947: 0, lu2026: 0 };
    const map = new maplibregl.Map({
      container: 'map2',
      ...initialView(),
      style: buildStyle(currentDark(), ['scan', 'zones1947', 'lu2026'], state),
      interactive: false,
      attributionControl: false
    });
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    map.once('style.load', () => {
      if (App.meta && App.meta.scan_bounds) {
        map.fitBounds(boundsFromArray(App.meta.scan_bounds), { padding: 0, animate: false });
      }
    });
    themeListeners.push((dark) => { map.setStyle(buildStyle(dark, ['scan', 'zones1947', 'lu2026'], state)); });

    function setOpacity(kind, value) {
      state[kind] = value;
      if (map.getLayer('layer-' + kind)) {
        map.setPaintProperty('layer-' + kind, 'raster-opacity', value);
      }
    }

    return { map, setOpacity, state };
  }

  function initScrollytelling(scrollyMap, resultsAccuracyText) {
    const accuracyInline = document.getElementById('accuracy-inline');
    if (App.results && App.results.accuracy && resultsAccuracyText) {
      const ov = App.results.accuracy.overall;
      accuracyInline.textContent = ov != null ? Math.round(ov) + '%' : '86%';
    } else {
      accuracyInline.textContent = '86%';
    }

    if (typeof scrollama !== 'function') return;
    const scroller = scrollama();
    scroller
      .setup({ step: '#scrolly-1 .step', offset: 0.55, progress: true })
      .onStepEnter(({ element }) => {
        document.querySelectorAll('#scrolly-1 .step').forEach((s) => s.classList.remove('is-active'));
        element.classList.add('is-active');
        const step = element.dataset.step;
        if (step === 'plan') {
          scrollyMap.setOpacity('scan', 1);
          scrollyMap.setOpacity('zones1947', 0);
          scrollyMap.setOpacity('lu2026', 0);
        } else if (step === 'today') {
          scrollyMap.setOpacity('scan', 0);
          scrollyMap.setOpacity('zones1947', 0);
          scrollyMap.setOpacity('lu2026', 1);
        }
      })
      .onStepProgress(({ element, progress }) => {
        const step = element.dataset.step;
        if (step === 'decode') {
          scrollyMap.setOpacity('scan', 1 - progress);
          scrollyMap.setOpacity('zones1947', progress);
          scrollyMap.setOpacity('lu2026', 0);
        }
      });
    window.addEventListener('resize', () => scroller.resize());
  }

  // ---------------------------------------------------------------- swipe compare

  function initSwipe() {
    // Start from the checkbox's actual state (browsers may restore it on reload).
    const scanOn = document.getElementById('swipe-scan-toggle').checked;
    const stateBefore = { zones1947: scanOn ? 0 : 1, scan: scanOn ? 1 : 0 };
    const stateAfter = { lu2026: 1 };
    const beforeMap = new maplibregl.Map({
      container: 'map-before',
      ...initialView(),
      style: buildStyle(currentDark(), ['scan', 'zones1947'], stateBefore),
      attributionControl: false
    });
    const afterMap = new maplibregl.Map({
      container: 'map-after',
      ...initialView(),
      style: buildStyle(currentDark(), ['lu2026'], stateAfter),
      attributionControl: false
    });
    afterMap.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

    let compare = null;
    function fit() {
      if (App.meta && App.meta.scan_bounds) {
        const b = boundsFromArray(App.meta.scan_bounds);
        beforeMap.fitBounds(b, { animate: false });
      }
    }
    beforeMap.once('style.load', () => {
      fit();
      if (typeof maplibregl.Compare === 'function') {
        compare = new maplibregl.Compare(beforeMap, afterMap, '#compare-container', {});
        makeSwipeHandleKeyboardAccessible(compare);
      }
    });

    themeListeners.push((dark) => {
      beforeMap.setStyle(buildStyle(dark, ['scan', 'zones1947'], stateBefore));
      afterMap.setStyle(buildStyle(dark, ['lu2026'], stateAfter));
    });

    const scanToggle = document.getElementById('swipe-scan-toggle');
    scanToggle.addEventListener('change', () => {
      if (scanToggle.checked) {
        stateBefore.scan = 1; stateBefore.zones1947 = 0;
      } else {
        stateBefore.scan = 0; stateBefore.zones1947 = 1;
      }
      if (beforeMap.getLayer('layer-scan')) beforeMap.setPaintProperty('layer-scan', 'raster-opacity', stateBefore.scan);
      if (beforeMap.getLayer('layer-zones1947')) beforeMap.setPaintProperty('layer-zones1947', 'raster-opacity', stateBefore.zones1947);
      updateSwipeLegends(scanToggle.checked);
    });
    updateSwipeLegends(scanToggle.checked);

    document.querySelectorAll('.flyto-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const center = [parseFloat(btn.dataset.lon), parseFloat(btn.dataset.lat)];
        const zoom = parseFloat(btn.dataset.zoom);
        beforeMap.flyTo({ center, zoom, essential: true, duration: reduceMotion ? 0 : 1200 });
        afterMap.flyTo({ center, zoom, essential: true, duration: reduceMotion ? 0 : 1200 });
      });
    });

    updateSwipeLegends(false);
  }

  function makeSwipeHandleKeyboardAccessible(compare) {
    const container = document.getElementById('compare-container');
    const handle = container.querySelector('.compare-swiper-vertical, .compare-swiper-horizontal');
    if (!handle) return;
    handle.setAttribute('tabindex', '0');
    handle.setAttribute('role', 'slider');
    handle.setAttribute('aria-label', 'Swipe comparison handle: 1947 plan versus 2026 land use');
    handle.setAttribute('aria-valuemin', '0');
    handle.setAttribute('aria-valuenow', String(Math.round(compare.currentPosition || 0)));
    handle.addEventListener('keydown', (e) => {
      const bounds = container.getBoundingClientRect();
      const max = bounds.width;
      handle.setAttribute('aria-valuemax', String(Math.round(max)));
      let pos = compare.currentPosition != null ? compare.currentPosition : max / 2;
      const step = e.shiftKey ? 80 : 20;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') pos = Math.max(0, pos - step);
      else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') pos = Math.min(max, pos + step);
      else if (e.key === 'Home') pos = 0;
      else if (e.key === 'End') pos = max;
      else return;
      e.preventDefault();
      compare.setSlider(pos);
      handle.setAttribute('aria-valuenow', String(Math.round(pos)));
    });
  }

  function updateSwipeLegends(showScan) {
    const left = document.getElementById('legend-swipe-left');
    const right = document.getElementById('legend-swipe-right');
    left.innerHTML = '';
    right.innerHTML = '';
    if (showScan) {
      const p = document.createElement('div');
      p.className = 'legend__item';
      p.textContent = 'Original 1947 scan (unclassified)';
      left.appendChild(p);
    } else {
      ['Residential', 'Commercial', 'Industrial', 'Public/Park'].forEach((g) => {
        left.appendChild(legendDot(groupColor(g), g + ' district'));
      });
    }
    const luNames = App.palette.lu2026 && Object.keys(App.palette.lu2026).length
      ? Object.keys(App.palette.lu2026)
      : ['Single-Family Residential', 'Multi-Family Residential', 'Commercial', 'Office', 'Industrial', 'Public & Institutional', 'Transportation & Utility', 'Park & Open Spaces'];
    luNames.forEach((name) => right.appendChild(legendDot(luColor(name), name)));
  }

  function legendDot(color, label) {
    const item = document.createElement('span');
    item.className = 'legend__item';
    const dot = document.createElement('span');
    dot.className = 'legend__dot';
    dot.style.background = color;
    const text = document.createElement('span');
    text.textContent = label;
    item.appendChild(dot);
    item.appendChild(text);
    return item;
  }

  // ---------------------------------------------------------------- agreement map

  function initAgreeMap() {
    const state = { agree: 1 };
    const map = new maplibregl.Map({
      container: 'map-agree',
      ...initialView(),
      style: buildStyle(currentDark(), ['agree'], state)
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    map.once('style.load', () => {
      if (App.meta && App.meta.scan_bounds) map.fitBounds(boundsFromArray(App.meta.scan_bounds), { animate: false });
      addExtentOutline(map);
    });
    themeListeners.push((dark) => {
      map.setStyle(buildStyle(dark, ['agree'], state));
      map.once('styledata', () => addExtentOutline(map));
    });

    map.on('click', (e) => showAgreePopup(e.lngLat));

    const closeBtn = document.querySelector('.agree-popup-panel__close');
    closeBtn.addEventListener('click', () => { document.getElementById('agree-popup-panel').hidden = true; });

    return map;
  }

  function decodeLookup() {
    if (!App.lookup) return;
    if (typeof App.lookup.zones === 'string') App.lookup._zones = b64ToUint8(App.lookup.zones);
    if (typeof App.lookup.lu === 'string') App.lookup._lu = b64ToUint8(App.lookup.lu);
  }

  function showAgreePopup(lngLat) {
    const panel = document.getElementById('agree-popup-panel');
    const content = document.getElementById('agree-popup-content');
    const lk = App.lookup;
    if (!lk || !lk._zones || !lk._lu) {
      content.textContent = 'Land-use lookup data is not available yet.';
      panel.hidden = false;
      return;
    }
    const [x, y] = lngLatToMeters(lngLat.lng, lngLat.lat);
    const col = Math.floor((x - lk.x0) / lk.cell);
    const row = Math.floor((lk.y0 - y) / lk.cell);
    if (col < 0 || col >= lk.nx || row < 0 || row >= lk.ny) {
      content.textContent = 'Outside the mapped 1947 study area.';
      panel.hidden = false;
      return;
    }
    const idx = row * lk.nx + col;
    const zoneCode = lk._zones[idx];
    const luCode = lk._lu[idx];
    const zoneLetter = zoneCode ? ZONE_LETTERS[zoneCode - 1] : null;
    const zoneName = zoneLetter && lk.zone_names ? lk.zone_names[zoneLetter] : null;
    const luName = luCode && lk.lu_names ? lk.lu_names[luCode - 1] : null;

    // No parcel = street, freeway, bayou or other right-of-way; no 1947 zone = street/bayou on the old map.
    let status = !luCode ? '– Street or right-of-way (not counted)'
               : !zoneCode ? '– No 1947 district here (not counted)'
               : '– Undeveloped/neutral';
    if (zoneCode && luCode) {
      const isNeutral = lk.neutral && lk.neutral.includes(luCode);
      if (isNeutral) {
        status = '– Undeveloped/neutral';
      } else if (lk.strict && lk.strict[zoneLetter] && lk.strict[zoneLetter].includes(luCode)) {
        status = '✓ Matches plan (strict)';
      } else if (lk.cumulative && lk.cumulative[zoneLetter] && lk.cumulative[zoneLetter].includes(luCode)) {
        status = '≈ Permitted (cumulative)';
      } else {
        status = '✗ Differs';
      }
    } else if (!zoneCode) {
      content.textContent = 'Outside the mapped 1947 districts here.';
      panel.hidden = false;
      return;
    }

    content.innerHTML = '';
    const dl = document.createElement('dl');
    const rows = [
      ['1947 district', zoneLetter ? `${zoneLetter} · ${zoneName || ''}` : 'None (street or bayou)'],
      ['2026 land use', luName || 'No parcel (right-of-way)'],
      ['Agreement', status]
    ];
    rows.forEach(([dt, dd]) => {
      const dtEl = document.createElement('dt'); dtEl.textContent = dt;
      const ddEl = document.createElement('dd'); ddEl.textContent = dd;
      dl.appendChild(dtEl); dl.appendChild(ddEl);
    });
    content.appendChild(dl);
    panel.hidden = false;
  }

  // ---------------------------------------------------------------- legends (static content sections)

  function buildPlanLegend() {
    const el = document.getElementById('legend-plan');
    el.innerHTML = '';
    const districts = (App.results && App.results.districts) || ZONE_LETTERS.map((code) => ({ code, name: '', group: code <= 'D' ? 'Residential' : code <= 'G' ? 'Commercial' : 'Industrial' }));
    const groups = ['Residential', 'Commercial', 'Industrial'];
    groups.forEach((g) => {
      const label = document.createElement('div');
      label.className = 'legend__group-label';
      label.textContent = g;
      el.appendChild(label);
      districts.filter((d) => d.group === g).sort((a, b) => a.code.localeCompare(b.code)).forEach((d) => {
        const item = document.createElement('span');
        item.className = 'legend__item';
        const img = document.createElement('img');
        img.className = 'legend__swatch';
        img.src = `${DATA}legend_swatches/${d.code}.png`;
        img.alt = '';
        img.loading = 'lazy';
        img.onerror = () => { img.style.background = zoneColor(d.code); img.removeAttribute('src'); };
        const dot = document.createElement('span');
        dot.className = 'legend__dot';
        dot.style.background = zoneColor(d.code);
        const text = document.createElement('span');
        text.textContent = `${d.code} — ${(App.lookup && App.lookup.zone_names && App.lookup.zone_names[d.code]) || d.name || ''}`;
        item.appendChild(img);
        item.appendChild(dot);
        item.appendChild(text);
        el.appendChild(item);
      });
    });
  }

  function buildLu2026Legend() {
    const el = document.getElementById('legend-lu2026');
    el.innerHTML = '';
    const names = App.palette.lu2026 && Object.keys(App.palette.lu2026).length
      ? Object.keys(App.palette.lu2026)
      : (App.lookup && App.lookup.lu_names) || [];
    names.forEach((name) => el.appendChild(legendDot(luColor(name), name)));
  }

  function buildAgreeLegend() {
    const el = document.getElementById('legend-agree');
    el.innerHTML = '';
    const a = App.palette.agree || FALLBACK_PALETTE.agree;
    [
      [a.strict, 'Matches plan (strict)'],
      [a.cumulative, 'Permitted (cumulative)'],
      [a.mismatch, 'Differs from plan'],
      [a.neutral, 'Undeveloped / neutral']
    ].forEach(([color, label]) => el.appendChild(legendDot(color, label)));
  }

  // ---------------------------------------------------------------- charts: dot & whisker

  function chartDotWhisker() {
    const container = document.getElementById('chart-dotwhisker');
    const tooltip = document.getElementById('chart-dotwhisker-tooltip');
    const groups = (App.results && App.results.groups) || [];
    if (!groups.length) { container.innerHTML = '<p class="chart-card__note">Results not available yet.</p>'; return; }

    function render() {
      container.innerHTML = '';
      const width = Math.max(320, container.clientWidth);
      const rowH = 64;
      const margin = { top: 20, right: 40, bottom: 36, left: 110 };
      const height = groups.length * rowH + margin.top + margin.bottom;
      const svg = d3.select(container).append('svg')
        .attr('viewBox', `0 0 ${width} ${height}`)
        .attr('role', 'group')
        .attr('aria-label', 'Strict match percentage by 1947 group, with confidence intervals and chance baselines');

      const x = d3.scaleLinear().domain([0, 100]).range([margin.left, width - margin.right]);
      const y = d3.scaleBand().domain(groups.map((g) => g.group)).range([margin.top, height - margin.bottom]).padding(0.4);

      // gridlines
      svg.append('g').attr('class', 'grid')
        .selectAll('line').data(x.ticks(5)).join('line')
        .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
        .attr('y1', margin.top).attr('y2', height - margin.bottom);

      svg.append('g').selectAll('text').data(x.ticks(5)).join('text')
        .attr('x', (d) => x(d)).attr('y', height - margin.bottom + 20)
        .attr('text-anchor', 'middle').attr('font-size', 11)
        .text((d) => d + '%');

      const rows = svg.selectAll('.row').data(groups).join('g').attr('class', 'row');

      // null band (mean to p95)
      rows.append('rect')
        .attr('x', (d) => x(d.null_mean))
        .attr('width', (d) => Math.max(0, x(d.null_p95) - x(d.null_mean)))
        .attr('y', (d) => y(d.group) - 6)
        .attr('height', 12)
        .attr('fill', 'var(--null-band)');

      // CI whisker
      rows.append('line')
        .attr('x1', (d) => x(d.strict_lo)).attr('x2', (d) => x(d.strict_hi))
        .attr('y1', (d) => y(d.group) + y.bandwidth() / 2)
        .attr('y2', (d) => y(d.group) + y.bandwidth() / 2)
        .attr('stroke', (d) => groupColor(d.group)).attr('stroke-width', 2);
      // whisker caps
      [['strict_lo'], ['strict_hi']].forEach(([k]) => {
        rows.append('line')
          .attr('x1', (d) => x(d[k])).attr('x2', (d) => x(d[k]))
          .attr('y1', (d) => y(d.group) + y.bandwidth() / 2 - 6)
          .attr('y2', (d) => y(d.group) + y.bandwidth() / 2 + 6)
          .attr('stroke', (d) => groupColor(d.group)).attr('stroke-width', 2);
      });

      // chance baseline (hollow marker)
      rows.append('circle')
        .attr('cx', (d) => x(d.baseline)).attr('cy', (d) => y(d.group) + y.bandwidth() / 2)
        .attr('r', 6).attr('fill', 'var(--surface-1)').attr('stroke', 'var(--chance-marker)').attr('stroke-width', 2);

      // strict dot (filled, r>=4)
      const marks = rows.append('circle')
        .attr('class', 'mark')
        .attr('cx', (d) => x(d.strict)).attr('cy', (d) => y(d.group) + y.bandwidth() / 2)
        .attr('r', 7).attr('fill', (d) => groupColor(d.group)).attr('stroke', 'var(--surface-1)').attr('stroke-width', 2)
        .style('cursor', 'pointer');

      marks.append('title').text((d) => `${d.group}: ${fmtPct(d.strict)} strict match (${fmtPct(d.strict_lo)}–${fmtPct(d.strict_hi)}), chance ${fmtPct(d.baseline)}`);

      // direct label: value at end
      rows.append('text')
        .attr('x', (d) => x(d.strict_hi) + 14)
        .attr('y', (d) => y(d.group) + y.bandwidth() / 2 + 4)
        .attr('font-size', 12).attr('font-weight', 700).attr('fill', 'var(--text-primary)')
        .text((d) => fmtPct(d.strict));

      // y axis labels
      svg.append('g').selectAll('text.ylab').data(groups).join('text').attr('class', 'ylab')
        .attr('x', margin.left - 14).attr('y', (d) => y(d.group) + y.bandwidth() / 2 + 4)
        .attr('text-anchor', 'end').attr('font-size', 13).attr('font-weight', 600).attr('fill', 'var(--text-primary)')
        .text((d) => d.group);

      // hit targets for hover
      rows.append('rect')
        .attr('x', margin.left).attr('width', width - margin.left - margin.right)
        .attr('y', (d) => y(d.group)).attr('height', y.bandwidth())
        .attr('fill', 'transparent')
        .on('pointermove', (event, d) => {
          tooltip.hidden = false;
          tooltip.style.left = (event.clientX + 14) + 'px';
          tooltip.style.top = (event.clientY - 12) + 'px';
          tooltip.innerHTML = '';
          const rowLine = (label, val) => {
            const r = document.createElement('div'); r.className = 'chart-tooltip__row';
            const v = document.createElement('span'); v.className = 'chart-tooltip__value'; v.textContent = val;
            const l = document.createElement('span'); l.className = 'chart-tooltip__label'; l.textContent = ' ' + label;
            r.appendChild(v); r.appendChild(l);
            tooltip.appendChild(r);
          };
          const title = document.createElement('div'); title.style.fontWeight = 700; title.textContent = d.group;
          tooltip.appendChild(title);
          rowLine('strict match', fmtPct(d.strict) + ` (${fmtPct(d.strict_lo)}–${fmtPct(d.strict_hi)})`);
          rowLine('chance baseline', fmtPct(d.baseline));
          rowLine('spatial-null mean–p95', `${fmtPct(d.null_mean)}–${fmtPct(d.null_p95)}`);
        })
        .on('pointerleave', () => { tooltip.hidden = true; });
    }
    render();
    window.addEventListener('resize', debounce(render, 150));
  }

  // ---------------------------------------------------------------- chart: sankey

  function chartSankey() {
    const container = document.getElementById('chart-sankey');
    const tooltip = document.getElementById('chart-sankey-tooltip');
    const flows = (App.results && App.results.flows) || [];
    if (!flows.length || typeof d3.sankey !== 'function') {
      container.innerHTML = '<p class="chart-card__note">Flow data not available yet.</p>';
      return;
    }

    function render() {
      container.innerHTML = '';
      const width = Math.max(360, container.clientWidth);
      const height = 420;
      // Left (1947) and right (2026) node sets are kept distinct even when a
      // name (e.g. "Residential") appears on both sides — otherwise d3-sankey
      // treats them as the same node and the graph becomes circular.
      const leftNames = Array.from(new Set(flows.map((f) => f.from)));
      const rightNames = Array.from(new Set(flows.map((f) => f.to)));
      const leftKey = (n) => 'L:' + n;
      const rightKey = (n) => 'R:' + n;
      const keys = [...leftNames.map(leftKey), ...rightNames.map(rightKey)];
      const nodeIndex = new Map(keys.map((k, i) => [k, i]));
      const sankeyData = {
        nodes: keys.map((k) => ({ name: k.slice(2) })),
        links: flows.map((f) => ({ source: nodeIndex.get(leftKey(f.from)), target: nodeIndex.get(rightKey(f.to)), value: Math.max(f.acres, 0.01), sourceName: f.from, targetName: f.to }))
      };
      const sankeyGen = d3.sankey()
        .nodeWidth(16).nodePadding(14)
        .extent([[1, 10], [width - 1, height - 10]]);
      const graph = sankeyGen(sankeyData);

      const svg = d3.select(container).append('svg')
        .attr('viewBox', `0 0 ${width} ${height}`)
        .attr('role', 'group').attr('aria-label', 'Sankey diagram: 1947 zoning group to 2026 land-use group, by acreage');

      svg.append('g').attr('fill', 'none')
        .selectAll('path').data(graph.links).join('path')
        .attr('d', d3.sankeyLinkHorizontal())
        .attr('stroke', (d) => groupColor(d.source.name))
        .attr('stroke-opacity', 0.35)
        .attr('stroke-width', (d) => Math.max(1, d.width))
        .style('cursor', 'pointer')
        .on('pointermove', (event, d) => {
          tooltip.hidden = false;
          tooltip.style.left = (event.clientX + 14) + 'px';
          tooltip.style.top = (event.clientY - 12) + 'px';
          tooltip.innerHTML = `<div><span class="chart-tooltip__value">${Math.round(d.value).toLocaleString()} ac</span> <span class="chart-tooltip__label">${d.source.name} → ${d.target.name}</span></div>`;
        })
        .on('pointerleave', () => { tooltip.hidden = true; })
        .append('title').text((d) => `${d.source.name} → ${d.target.name}: ${Math.round(d.value).toLocaleString()} acres`);

      const node = svg.append('g').selectAll('g').data(graph.nodes).join('g');
      node.append('rect')
        .attr('x', (d) => d.x0).attr('y', (d) => d.y0)
        .attr('width', (d) => d.x1 - d.x0).attr('height', (d) => Math.max(1, d.y1 - d.y0))
        .attr('fill', (d) => groupColor(d.name))
        .attr('rx', 3)
        .append('title').text((d) => `${d.name}: ${Math.round(d.value).toLocaleString()} ac`);

      node.append('text')
        .attr('x', (d) => (d.x0 < width / 2 ? d.x1 + 8 : d.x0 - 8))
        .attr('y', (d) => (d.y0 + d.y1) / 2)
        .attr('dy', '0.35em')
        .attr('text-anchor', (d) => (d.x0 < width / 2 ? 'start' : 'end'))
        .attr('font-size', 12).attr('fill', 'var(--text-primary)')
        .text((d) => d.name);
    }
    render();
    window.addEventListener('resize', debounce(render, 150));
  }

  // ---------------------------------------------------------------- chart: district bars

  function chartDistricts() {
    const container = document.getElementById('chart-districts');
    const tooltip = document.getElementById('chart-districts-tooltip');
    const districts = (App.results && App.results.districts) || [];
    if (!districts.length) { container.innerHTML = '<p class="chart-card__note">District data not available yet.</p>'; return; }
    const sorted = [...districts].sort((a, b) => ZONE_LETTERS.indexOf(a.code) - ZONE_LETTERS.indexOf(b.code));

    function render() {
      container.innerHTML = '';
      const width = Math.max(320, container.clientWidth);
      const margin = { top: 20, right: 20, bottom: 36, left: 40 };
      const height = 320;
      const svg = d3.select(container).append('svg')
        .attr('viewBox', `0 0 ${width} ${height}`)
        .attr('role', 'group').attr('aria-label', 'Strict match percentage by district A through J');

      const x = d3.scaleBand().domain(sorted.map((d) => d.code)).range([margin.left, width - margin.right]).padding(0.35);
      const y = d3.scaleLinear().domain([0, 100]).range([height - margin.bottom, margin.top]);

      svg.append('g').attr('class', 'grid')
        .selectAll('line').data(y.ticks(5)).join('line')
        .attr('y1', (d) => y(d)).attr('y2', (d) => y(d))
        .attr('x1', margin.left).attr('x2', width - margin.right);

      svg.append('g').selectAll('text').data(y.ticks(5)).join('text')
        .attr('x', margin.left - 8).attr('y', (d) => y(d) + 4)
        .attr('text-anchor', 'end').attr('font-size', 11).text((d) => d + '%');

      const barW = Math.min(24, x.bandwidth());
      const bars = svg.selectAll('.bar').data(sorted).join('rect')
        .attr('x', (d) => x(d.code) + (x.bandwidth() - barW) / 2)
        .attr('width', barW)
        .attr('y', (d) => y(d.strict))
        .attr('height', (d) => Math.max(0, y(0) - y(d.strict)))
        .attr('rx', 4)
        .attr('fill', (d) => groupColor(d.group))
        .style('cursor', 'pointer');

      bars.append('title').text((d) => `${d.code} — ${d.name}: ${fmtPct(d.strict)} strict match`);
      bars.on('pointermove', (event, d) => {
        tooltip.hidden = false;
        tooltip.style.left = (event.clientX + 14) + 'px';
        tooltip.style.top = (event.clientY - 12) + 'px';
        tooltip.innerHTML = `<div style="font-weight:700">${d.code} — ${d.name}</div><div class="chart-tooltip__row"><span class="chart-tooltip__value">${fmtPct(d.strict)}</span><span class="chart-tooltip__label"> strict match</span></div>`;
      }).on('pointerleave', () => { tooltip.hidden = true; });

      svg.append('g').selectAll('text.xlab').data(sorted).join('text').attr('class', 'xlab')
        .attr('x', (d) => x(d.code) + x.bandwidth() / 2).attr('y', height - margin.bottom + 18)
        .attr('text-anchor', 'middle').attr('font-size', 12).attr('fill', 'var(--text-primary)')
        .text((d) => d.code);

      svg.append('line').attr('class', 'axis-baseline')
        .attr('x1', margin.left).attr('x2', width - margin.right)
        .attr('y1', y(0)).attr('y2', y(0));
    }
    render();
    window.addEventListener('resize', debounce(render, 150));
  }

  // ---------------------------------------------------------------- chart: distance decay

  function chartDecay() {
    const container = document.getElementById('chart-decay');
    const tooltip = document.getElementById('chart-decay-tooltip');
    const decay = (App.results && App.results.decay) || [];
    if (!decay.length) { container.innerHTML = '<p class="chart-card__note">Decay data not available yet.</p>'; return; }
    const seriesNames = ['Residential', 'Commercial', 'Industrial'];

    function render() {
      container.innerHTML = '';
      const width = Math.max(320, container.clientWidth);
      const margin = { top: 20, right: 90, bottom: 36, left: 44 };
      const height = 340;
      const svg = d3.select(container).append('svg')
        .attr('viewBox', `0 0 ${width} ${height}`)
        .attr('role', 'group').attr('aria-label', 'Strict match percentage versus shift distance, one line per group');

      const x = d3.scaleLinear().domain(d3.extent(decay, (d) => d.shift_ft)).range([margin.left, width - margin.right]);
      const yMax = d3.max(decay, (d) => Math.max(d.Residential || 0, d.Commercial || 0, d.Industrial || 0)) || 100;
      const y = d3.scaleLinear().domain([0, Math.min(100, Math.ceil(yMax / 10) * 10)]).range([height - margin.bottom, margin.top]);

      svg.append('g').attr('class', 'grid')
        .selectAll('line').data(y.ticks(5)).join('line')
        .attr('y1', (d) => y(d)).attr('y2', (d) => y(d)).attr('x1', margin.left).attr('x2', width - margin.right);
      svg.append('g').selectAll('text').data(y.ticks(5)).join('text')
        .attr('x', margin.left - 8).attr('y', (d) => y(d) + 4).attr('text-anchor', 'end').attr('font-size', 11)
        .text((d) => d + '%');
      svg.append('g').selectAll('text').data(x.ticks(6)).join('text')
        .attr('x', (d) => x(d)).attr('y', height - margin.bottom + 18).attr('text-anchor', 'middle').attr('font-size', 11)
        .text((d) => d.toLocaleString());
      svg.append('text').attr('x', (width) / 2).attr('y', height - 2).attr('text-anchor', 'middle')
        .attr('font-size', 11).attr('fill', 'var(--text-muted)').text('Shift distance (ft)');

      const line = d3.line().x((d) => x(d.shift_ft)).y((d) => y(d.value));
      const focus = svg.append('g').style('display', 'none');
      const focusLine = focus.append('line').attr('y1', margin.top).attr('y2', height - margin.bottom).attr('stroke', 'var(--baseline)');

      seriesNames.forEach((name) => {
        const pts = decay.filter((d) => d[name] != null).map((d) => ({ shift_ft: d.shift_ft, value: d[name] }));
        if (!pts.length) return;
        svg.append('path').datum(pts).attr('fill', 'none').attr('stroke', groupColor(name)).attr('stroke-width', 2)
          .attr('d', line);
        pts.forEach((p, i) => {
          if (i === pts.length - 1 || i === 0) {
            svg.append('circle').attr('cx', x(p.shift_ft)).attr('cy', y(p.value)).attr('r', 4)
              .attr('fill', groupColor(name)).attr('stroke', 'var(--surface-1)').attr('stroke-width', 2);
          }
        });
        const last = pts[pts.length - 1];
        svg.append('text').attr('x', x(last.shift_ft) + 8).attr('y', y(last.value) + 4)
          .attr('font-size', 12).attr('font-weight', 700).attr('fill', groupColor(name)).text(name);
      });

      // crosshair + shared tooltip across series
      const overlay = svg.append('rect').attr('x', margin.left).attr('y', margin.top)
        .attr('width', width - margin.left - margin.right).attr('height', height - margin.top - margin.bottom)
        .attr('fill', 'transparent');
      overlay.on('pointerenter', () => { focus.style('display', null); })
        .on('pointerleave', () => { focus.style('display', 'none'); tooltip.hidden = true; })
        .on('pointermove', (event) => {
          const [mx] = d3.pointer(event);
          const shift = x.invert(mx);
          const nearest = decay.reduce((a, b) => (Math.abs(b.shift_ft - shift) < Math.abs(a.shift_ft - shift) ? b : a));
          focusLine.attr('x1', x(nearest.shift_ft)).attr('x2', x(nearest.shift_ft));
          tooltip.hidden = false;
          tooltip.style.left = (event.clientX + 14) + 'px';
          tooltip.style.top = (event.clientY - 12) + 'px';
          tooltip.innerHTML = `<div style="font-weight:700">${nearest.shift_ft.toLocaleString()} ft shift</div>` +
            seriesNames.filter((n) => nearest[n] != null).map((n) =>
              `<div class="chart-tooltip__row"><span class="chart-tooltip__swatch" style="background:${groupColor(n)}"></span><span class="chart-tooltip__value">${fmtPct(nearest[n])}</span><span class="chart-tooltip__label"> ${n}</span></div>`
            ).join('');
        });
    }
    render();
    window.addEventListener('resize', debounce(render, 150));
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  // ---------------------------------------------------------------- boot

  async function main() {
    initTheme();

    const [baseLight, baseDark] = await Promise.all([
      fetchJSON(BASEMAP_STYLE_URLS.light, null),
      fetchJSON(BASEMAP_STYLE_URLS.dark, null)
    ]);
    App.baseStyles = { light: baseLight, dark: baseDark };

    const [meta, palette, results, lookup, extent] = await Promise.all([
      fetchJSON(DATA + 'meta.json', null),
      fetchJSON(DATA + 'palette.json', FALLBACK_PALETTE),
      fetchJSON(DATA + 'results.json', null),
      fetchJSON(DATA + 'lookup.json', null),
      fetchJSON(DATA + 'extent.geojson', null)
    ]);
    App.meta = meta;
    App.palette = palette || FALLBACK_PALETTE;
    App.results = results;
    App.lookup = lookup;
    App.extent = extent;
    decodeLookup();

    const heroMap = initHeroMap();
    const scrollyMap = initScrollyMap();
    initScrollytelling(scrollyMap);
    initSwipe();
    initAgreeMap();

    buildPlanLegend();
    buildLu2026Legend();
    buildAgreeLegend();

    chartDotWhisker();
    chartSankey();
    chartDistricts();
    chartDecay();

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!localStorage.getItem('h1947-theme')) onThemeChange();
    });
  }

  document.addEventListener('DOMContentLoaded', main);
})();

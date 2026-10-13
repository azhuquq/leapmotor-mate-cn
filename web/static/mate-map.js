/* China map tiles/styles adapted from ha-leapmotor-cn/src/lib/geo.ts and
 * src/cards/map.ts (AGPLv3). Leaflet's public coordinates remain WGS84:
 * only projection changes to GCJ02, including the inverse for clicks/drags.
 */
(function (root) {
  'use strict'
  const sources = Object.freeze({
    vector: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    satellite: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
    osm: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
  })
  const styles = ['auto', 'vector', 'satellite', 'dark']
  const names = {auto: '自動', vector: '路網', satellite: '衛星', dark: '深色'}
  const maps = new Set()
  let selected = 'auto'
  try {
    const saved = localStorage.getItem('mate.map.style')
    if (styles.includes(saved)) selected = saved
  } catch (_) {}

  function effectiveStyle(style) {
    if (style !== 'auto') return style
    const rgb = getComputedStyle(document.body).backgroundColor.match(/[\d.]+/g)
    return rgb && Number(rgb[0]) + Number(rgb[1]) + Number(rgb[2]) < 384 ? 'dark' : 'vector'
  }

  function crs(L) {
    const mercator = L.Projection.SphericalMercator
    const coordinates = root.MateCoordinates
    return L.extend({}, L.CRS.EPSG3857, {
      code: 'MATE:WGS84-GCJ02',
      projection: {
        bounds: mercator.bounds,
        project(at) {
          const pair = coordinates.wgs84ToGcj02(at.lat, at.lng)
          return mercator.project(L.latLng(pair[0], pair[1]))
        },
        unproject(point) {
          const at = mercator.unproject(point)
          const pair = coordinates.gcj02ToWgs84(at.lat, at.lng)
          return L.latLng(pair[0], pair[1])
        }
      }
    })
  }

  function create(element, options) {
    const L = root.L
    const map = L.map(element, Object.assign({}, options, {crs: crs(L), maxZoom: 18}))
    map.mateCoordinateSystem = 'WGS84'
    return map
  }

  function addTiles(map) {
    const L = root.L
    if (map._mateTiles) return map._mateTiles
    const state = {layer: null, control: null, select: null, notice: null, style: selected, fallback: false}
    map._mateTiles = state
    maps.add(map)

    if (!document.getElementById('mate-map-css')) {
      const css = document.createElement('style')
      css.id = 'mate-map-css'
      css.textContent = '.mate-map-dark{filter:invert(100%) hue-rotate(180deg) brightness(85%) contrast(90%)}' +
        '.mate-map-style{background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:6px;padding:4px;max-width:170px;font:12px sans-serif}' +
        '.mate-map-style select{background:#0f172a;color:#e2e8f0;border:0;padding:4px;max-width:100%}' +
        '.mate-map-style small{display:block;max-width:155px;font-size:10px;padding:2px;white-space:normal}'
      document.head.appendChild(css)
    }

    function apply() {
      const center = map._loaded ? map.getCenter() : null
      const zoom = map._loaded ? map.getZoom() : null
      if (state.layer) map.removeLayer(state.layer)
      // A fallback must switch the whole map to WGS84; mixing OSM tiles into
      // a GCJ02 grid would reintroduce the offset this adapter fixes.
      map.options.crs = state.fallback ? L.CRS.EPSG3857 : crs(L)
      map.mateTileCoordinateSystem = state.fallback ? 'WGS84' : 'GCJ02'
      const style = effectiveStyle(state.style)
      const url = state.fallback ? sources.osm : sources[style === 'satellite' ? 'satellite' : 'vector']
      const layer = L.tileLayer(url, {
        subdomains: '1234', maxZoom: 18, referrerPolicy: 'origin',
        attribution: state.fallback ? '© OpenStreetMap contributors' : '© 高德地圖',
        className: style === 'dark' ? 'mate-map-dark' : ''
      })
      state.layer = layer
      let failures = 0
      layer.on('tileerror', function () {
        if (state.layer !== layer || state.fallback || ++failures < 3) return
        state.fallback = true
        apply()
      })
      layer.addTo(map)
      if (center) map.setView(center, zoom, {reset: true, animate: false})
      if (state.select) state.select.value = state.style
      if (state.notice) {
        state.notice.textContent = state.fallback ? '高德暫不可用，已切換 OpenStreetMap' : ''
        state.notice.hidden = !state.fallback
      }
    }

    const StyleControl = L.Control.extend({
      options: {position: 'topright'},
      onAdd() {
        const box = L.DomUtil.create('div', 'mate-map-style')
        const select = document.createElement('select')
        select.setAttribute('aria-label', '地圖底圖')
        for (const style of styles) {
          const option = document.createElement('option')
          option.value = style
          option.textContent = names[style]
          select.appendChild(option)
        }
        const notice = document.createElement('small')
        notice.setAttribute('role', 'status')
        notice.hidden = true
        box.append(select, notice)
        state.select = select
        state.notice = notice
        select.value = state.style
        L.DomEvent.disableClickPropagation(box)
        L.DomEvent.disableScrollPropagation(box)
        select.addEventListener('change', function () {
          selected = select.value
          try { localStorage.setItem('mate.map.style', selected) } catch (_) {}
          for (const active of maps) active._mateTiles.setStyle(selected)
        })
        return box
      }
    })
    state.setStyle = function (style) {
      state.style = styles.includes(style) ? style : 'auto'
      state.fallback = false
      apply()
    }
    state.control = new StyleControl().addTo(map)
    apply()
    const media = root.matchMedia('(prefers-color-scheme: dark)')
    const themeChanged = function () { if (state.style === 'auto') apply() }
    media.addEventListener('change', themeChanged)
    map.on('unload', function () {
      media.removeEventListener('change', themeChanged)
      maps.delete(map)
    })
    return state
  }

  root.MateMap = Object.freeze({create, addTiles, sources, defaultCenter: [35.8617, 104.1954]})
})(globalThis)

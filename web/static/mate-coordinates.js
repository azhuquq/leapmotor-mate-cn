/* WGS84 <-> GCJ02 adapted from ha-leapmotor-cn/src/lib/geo.ts (AGPLv3).
 * Latitudes come first. Backend storage, exported routes and MQTT remain WGS84.
 */
(function (root) {
  'use strict'
const A_GCJ = 6378245.0;
const EE_GCJ = 0.00669342162296594323;

function outOfChina(lat, lon) {
  return !(lon > 73.66 && lon < 135.05 && lat > 3.86 && lat < 53.55);
}

function transformLat(x, y) {
  let r =
    -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  r += ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) * 2.0) / 3.0;
  r +=
    ((160.0 * Math.sin((y / 12.0) * Math.PI) + 320 * Math.sin((y * Math.PI) / 30.0)) * 2.0) / 3.0;
  return r;
}

/** GCJ-02 的**经度**多项式(与 transformLat 是两个不同公式)。 */
function transformLon(x, y) {
  let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  r += ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) * 2.0) / 3.0;
  r +=
    ((150.0 * Math.sin((x / 12.0) * Math.PI) + 300.0 * Math.sin((x * Math.PI) / 30.0)) * 2.0) /
    3.0;
  return r;
}

function delta(lat, lon) {
  let dLat = transformLat(lon - 105.0, lat - 35.0);
  let dLon = transformLon(lon - 105.0, lat - 35.0);
  const radLat = (lat / 180.0) * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - EE_GCJ * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / (((A_GCJ * (1 - EE_GCJ)) / (magic * sqrtMagic)) * Math.PI);
  dLon = (dLon * 180.0) / ((A_GCJ / sqrtMagic) * Math.cos(radLat) * Math.PI);
  return [dLat, dLon];
}

/** GCJ-02 → WGS-84(迭代一次压到亚米级)。 */
function gcj02ToWgs84(lat, lon) {
  if (outOfChina(lat, lon)) return [lat, lon];
  const [dLat, dLon] = delta(lat, lon);
  const wLat = lat - dLat;
  const wLon = lon - dLon;
  const [dLat2, dLon2] = delta(wLat, wLon);
  return [lat - dLat2, lon - dLon2];
}

/** WGS-84 → GCJ-02(国境外原样返回)。 */
function wgs84ToGcj02(lat, lon) {
  if (outOfChina(lat, lon)) return [lat, lon];
  const [dLat, dLon] = delta(lat, lon);
  return [lat + dLat, lon + dLon];
}


  const api = Object.freeze({outOfChina, wgs84ToGcj02, gcj02ToWgs84})
  root.MateCoordinates = api
  if (typeof module !== 'undefined' && module.exports) module.exports = api
})(globalThis)

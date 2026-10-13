"""High-level WGS84 data on a GCJ02 basemap, with no external tile traffic."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import mate_api  # configure the shared vendored runtime
from leapmotor_cn.protocol import wgs84_to_gcj02, gcj02_to_wgs84

ROOT = Path(__file__).resolve().parent.parent
POINTS = [(39.908823,116.39747),(31.2304,121.4737),(22.5431,114.0579),
          (30.5728,104.0668),(51.5074,-0.1278),(-33.8688,151.2093)]


def test_browser_conversion_matches_the_ingest_conversion():
    if not shutil.which('node'):pytest.skip('Node required for cross-language geo contract')
    script = "const g=require(process.argv[1]); const p=JSON.parse(process.argv[2]); console.log(JSON.stringify(p.map(at=>({forward:g.wgs84ToGcj02(...at),inverse:g.gcj02ToWgs84(...at)}))))"
    rows=json.loads(subprocess.check_output(['node','-e',script,str(ROOT/'web/static/mate-coordinates.js'),json.dumps(POINTS)],text=True))
    for point,row in zip(POINTS,rows):
        assert row['forward']==pytest.approx(wgs84_to_gcj02(*point),abs=1e-12)
        assert row['inverse']==pytest.approx(gcj02_to_wgs84(*point),abs=1e-12)
    # Outside the conversion area coordinates pass through unchanged.
    for point,row in zip(POINTS[-2:],rows[-2:]):assert row['forward']==row['inverse']==list(point)


@pytest.mark.parametrize('width',[390,1100])
def test_projection_styles_click_inverse_and_fallback_in_browser(width,tmp_path):
    pw=pytest.importorskip('playwright.sync_api')
    html='''<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/static/vendor/leaflet/leaflet.css">
      <script src="/static/vendor/leaflet/leaflet.js"></script>
      <script src="/static/mate-coordinates.js"></script><script src="/static/mate-map.js"></script>
      </head><body style="margin:0;background:#0f172a"><div id="map" style="height:450px;width:100%"></div>
      <script>
      window.original=[39.908823,116.39747];window.endpoint=[39.91,116.40];
      window.map=MateMap.create('map').setView(original,16);MateMap.addTiles(map);
      window.marker=L.marker(original).addTo(map);
      window.line=L.polyline([original,endpoint]).addTo(map);
      window.circle=L.circle(original,{radius:100}).addTo(map);
      window.clicked=null;map.on('click',e=>clicked=[e.latlng.lat,e.latlng.lng]);
      </script></body></html>'''
    requests=[]
    with pw.sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':width,'height':650})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        def serve(route):
            url=route.request.url
            if url=='http://mate-map.test/':return route.fulfill(body=html,content_type='text/html')
            if url.startswith('http://mate-map.test/static/'):
                file=ROOT/'web/static'/url.split('/static/',1)[1]
                return route.fulfill(body=file.read_bytes(),content_type='text/css' if file.suffix=='.css' else ('application/javascript' if file.suffix=='.js' else 'image/png'))
            requests.append(url)
            route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#bcc8d0"/><path d="M0 128h256M128 0v256" stroke="#fff" stroke-width="8"/></svg>',content_type='image/svg+xml')
        page.route('**/*',serve)
        page.goto('http://mate-map.test/')
        page.wait_for_function('map._mateTiles.layer.isLoading() === false')
        result=page.evaluate('''() => {
          const shifted=MateCoordinates.wgs84ToGcj02(...original);
          const actual=map.project(original,16),expected=L.CRS.EPSG3857.latLngToPoint(L.latLng(...shifted),16);
          const at=map.latLngToContainerPoint(original);
          const inverse=map.unproject(actual,16);
          return {delta:actual.distanceTo(expected),inverse:[inverse.lat,inverse.lng],
                  original:marker.getLatLng(),line:line.getLatLngs(),radius:circle.getRadius(),
                  source:map.mateTileCoordinateSystem,zoom:map.getMaxZoom(),
                  overflow:document.documentElement.scrollWidth>innerWidth};
        }''')
        assert result['delta']<1e-6
        assert result['inverse']==pytest.approx(POINTS[0],abs=2e-6)
        assert result['original']=={'lat':POINTS[0][0],'lng':POINTS[0][1]}
        assert result['line'][0]==result['original'] and result['radius']==100
        assert result['source']=='GCJ02' and result['zoom']==18
        assert not result['overflow']
        assert any('webrd0' in url and 'style=8' in url for url in requests)
        page.locator('select[aria-label="地圖底圖"]').select_option('satellite')
        assert page.evaluate('map._mateTiles.layer._url').startswith('https://webst0')
        assert page.evaluate('localStorage.getItem("mate.map.style")')=='satellite'
        page.locator('select[aria-label="地圖底圖"]').select_option('dark')
        assert page.locator('.mate-map-dark').count()==1
        # A failing provider switches EVERY layer to the WGS84 OSM grid.
        page.evaluate('map._mateTiles.layer.fire("tileerror");map._mateTiles.layer.fire("tileerror");map._mateTiles.layer.fire("tileerror")')
        assert page.evaluate('map.mateTileCoordinateSystem')=='WGS84'
        assert page.evaluate('map.project(original,16).distanceTo(L.CRS.EPSG3857.latLngToPoint(L.latLng(...original),16))')<1e-6
        at=page.evaluate('map.latLngToContainerPoint(original).add([40,0])')
        expected=page.evaluate('''at => {const p=map.containerPointToLatLng(at);return [p.lat,p.lng]}''',at)
        page.locator('#map').click(position={'x':at['x'],'y':at['y']})
        assert page.evaluate('clicked')==pytest.approx(expected,abs=1e-5)
        assert page.evaluate('marker.getLatLng()')==result['original']
        page.screenshot(path=str(tmp_path/f'cn-map-{width}.png'))
        assert errors==[]
        browser.close()


def test_charging_place_map_saves_wgs84_after_gcj02_display(tmp_path,monkeypatch):
    pw=pytest.importorskip('playwright.sync_api')
    import db as D,db_reader as W,main
    from starlette.testclient import TestClient
    monkeypatch.setenv('MATE_MARKET','CN')
    database=D.Database(str(tmp_path/'cn-map.db'))
    database.ensure_vehicle('SYNTHETIC-MAP','B01',2025)
    monkeypatch.setattr(W,'DB_PATH',str(tmp_path/'cn-map.db'))
    W.set_setting('setup_complete','1');W.set_setting('language','zh-TW')
    client=TestClient(main.app,base_url='http://mate-map.test')
    with pw.sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':390,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        def serve(route):
            req=route.request
            if not req.url.startswith('http://mate-map.test/'):
                return route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"/>',content_type='image/svg+xml')
            r=client.request(req.method,req.url,content=req.post_data,headers={'content-type':req.headers.get('content-type','')},follow_redirects=True)
            route.fulfill(status=r.status_code,body=r.content,headers={'content-type':r.headers.get('content-type','text/html')})
        page.route('**/*',serve)
        page.goto('http://mate-map.test/costs')
        page.evaluate('''() => {const make=L.map;L.map=function(...args){window.testPlaceMap=make(...args);return window.testPlaceMap}}''')
        page.locator('#charging-places summary').click()
        page.wait_for_function('window.testPlaceMap && testPlaceMap._loaded')
        page.evaluate('testPlaceMap.setView([39.908823,116.39747],16,{animate:false})')
        page.evaluate('testPlaceMap.on("click",e=>window.pickedPoint=[e.containerPoint.x,e.containerPoint.y])')
        page.locator('#charging-place-map').click(position={'x':120,'y':100})
        expected=page.evaluate('''() => {
          const pixels=testPlaceMap.containerPointToLayerPoint(pickedPoint).add(testPlaceMap.getPixelOrigin());
          const gcj=L.CRS.EPSG3857.pointToLatLng(pixels,testPlaceMap.getZoom());
          return MateCoordinates.gcj02ToWgs84(gcj.lat,gcj.lng);
        }''')
        form=page.locator('#new-place')
        entered=[float(form.locator('[name=latitude]').input_value()),float(form.locator('[name=longitude]').input_value())]
        assert entered==pytest.approx(expected,abs=1e-6)
        assert page.evaluate('testPlaceMap.mateTileCoordinateSystem')=='GCJ02'
        form.locator('[name=name]').fill('Synthetic map place')
        form.locator('[name=rate]').fill('0.25')
        with page.expect_navigation():form.locator('button[type=submit]').click()
        saved=database._conn.execute("SELECT latitude,longitude FROM charging_places WHERE name='Synthetic map place'").fetchone()
        assert tuple(saved)==tuple(entered)
        assert errors==[]
        browser.close()
    database._conn.close()

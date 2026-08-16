import json, math, random
from pathlib import Path
from irfaran import db, composite, raster

def main():
    random.seed(7)
    Path('/out/demo.db').unlink(missing_ok=True)
    conn = db.open_initialised('/out/demo.db')
    LON, LAT = -30.0, 20.0
    M = 1 / 111_320
    
    def road(x0, y0, x1, y1, jitter=6.0, step=18.0):
        span = math.hypot(x1 - x0, y1 - y0)
        n = max(int(span / step), 2)
        return [(LON + (x0 + (x1-x0)*t + random.gauss(0, jitter)) * M,
                 LAT + (y0 + (y1-y0)*t + random.gauss(0, jitter)) * M)
                for t in (i/n for i in range(n+1))]
    
    def add(points, layer, radius=20.0):
        cur = conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES ('workout', 'add', ?, ?, ?, NULL, '2024-05-01T08:00:00+00:00', NULL)",
            (json.dumps({'type': 'LineString', 'coordinates': [[a, b] for a, b in points]}),
             radius, json.dumps([layer])))
        raster.stamp_event(conn, conn.execute('SELECT * FROM events WHERE id=?', (cur.lastrowid,)).fetchone())
    
    years = ['2021', '2022', '2023', '2024']
    for i, y in enumerate(range(-600, 601, 300)):
        for _ in range(12 - abs(i-2)*3):
            add(road(-900, y, 900, y), random.choice(years))
    for i, x in enumerate(range(-800, 801, 400)):
        for _ in range(9 - abs(i-2)*2):
            add(road(x, -700, x, 700), random.choice(years))
    add(road(0, 0, 2600, 2100, jitter=10, step=40), '2024')
    add(road(2600, 2100, 4200, 1500, jitter=10, step=40), '2024')
    
    root = Path('/out/tiles')
    composite.render_views(conn, root, composite.available_views(conn), themes=('dark',), workers=4)
    print('events', conn.execute('SELECT count(*) FROM events').fetchone()[0],
          '| views', composite.available_views(conn),
          '| native tiles', len(composite.tiles_with_data(conn, None)))


if __name__ == "__main__":
    main()

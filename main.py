import sqlite3
import csv
import io
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# 1. VERİTABANI OLUŞTURMA VE BAŞLANGIÇ AYARLARI
def init_db():
    conn = sqlite3.connect('acil_kod.db')
    cursor = conn.cursor()
    
    # Konumlar Tablosu
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bina TEXT NOT NULL,
            kat TEXT NOT NULL,
            birim TEXT NOT NULL
        )
    ''')
    
    # Aktif Cihaz Kayıtları Tablosu
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS active_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT UNIQUE,
        role TEXT,
        fcm_token TEXT,
        last_seen TIMESTAMP       
    )
''')
    
    # Acil Kod Logları Tablosu
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS emergency_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kod_renk TEXT NOT NULL,
        location_id INTEGER,
        veren_kullanici_id INTEGER,
        kabul_eden_kullanici_id TEXT,
        baslangic_zaman DATETIME,       
        kabul_zaman DATETIME,
        durum TEXT DEFAULT 'AKTIF',
        FOREIGN KEY(location_id) REFERENCES locations(id)
    )
''')
    
    # Varsayılan Konum Yoksa Ekle
    cursor.execute("SELECT COUNT(*) FROM locations")
    if cursor.fetchone()[0] == 0:
        cursor.executemany(
            "INSERT INTO locations (bina, kat, birim) VALUES (?, ?, ?)",
            [
                ("Ana Bina", "Kat 1", "Pedodonti Polikliniği"),
                ("Ana Bina", "Zemin Kat", "Acil Servis"),
                ("D Blok", "Kat 2", "Protetik Diş Tedavisi")
            ]
        )
    
    conn.commit()
    conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

# 2. FastAPI UYGULAMASININ OLUŞTURULMASI
app = FastAPI(
    title="Hastane Acil Kod Sistemi & Yönetim Paneli",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    conn = sqlite3.connect('acil_kod.db')
    conn.row_factory = sqlite3.Row
    return conn

# 3. PYDANTIC MODEL TANIMLARI
class LocationCreate(BaseModel):
    bina: str
    kat: str
    birim: str

class KodTetikleRequest(BaseModel):
    kod_renk: str
    konum: str  # Flutter tarafından gönderilen birleşik konum metni veya ID

class DeviceRegister(BaseModel):
    device_id: str
    role: str
    fcm_token: str = None

class KodKapatRequest(BaseModel):
    log_id: int
    kabul_personel: str

# 4. REST API UÇ NOKTALARI

# Hem /api/locations hem /api/konumlar destekleniyor
@app.get("/api/locations")
@app.get("/api/konumlar")
def get_locations():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, bina, kat, birim FROM locations ORDER BY bina, kat, birim")
    rows = cursor.fetchall()
    conn.close()
    
    result = []
    for row in rows:
        r_dict = dict(row)
        r_dict["ad"] = f"{r_dict['bina']} - {r_dict['kat']} - {r_dict['birim']}"
        result.append(r_dict)
    return result

@app.post("/api/locations")
def add_location(loc: LocationCreate):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO locations (bina, kat, birim) VALUES (?, ?, ?)",
        (loc.bina.strip(), loc.kat.strip(), loc.birim.strip())
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return {"status": "success", "id": new_id, "message": "Konum başarıyla eklendi."}

@app.delete("/api/locations/{location_id}")
def delete_location(location_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM locations WHERE id = ?", (location_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Konum silindi."}

# Flutter Mobil / Web'den Gelen Kod Çağrıları İçin Tek Uç Nokta
@app.post("/api/kod-tetikle")
@app.post("/api/kod-ver")
def acil_kod_ver(req: KodTetikleRequest):
    conn = get_db()
    cursor = conn.cursor()
    
    # Konum metni üzerinden location_id bulma veya varsayılan ilk konumu atama
    cursor.execute("SELECT id FROM locations LIMIT 1")
    first_loc = cursor.fetchone()
    loc_id = first_loc["id"] if first_loc else 1

    cursor.execute('''
        INSERT INTO emergency_logs (kod_renk, location_id, veren_kullanici_id, durum)
        VALUES (?, ?, 1, 'AKTIF')
    ''', (req.kod_renk, loc_id))
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {"status": "success", "log_id": log_id, "message": f"{req.kod_renk} KOD çağrısı kaydedildi."}

@app.post("/api/register-device")
def register_device(device: DeviceRegister):
    # Anlık Türkiye saatini üretiyoruz
    tr_zaman = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO active_devices (device_id, role, fcm_token, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            role = excluded.role,
            fcm_token = excluded.fcm_token,
            last_seen = ?
    """, (device.device_id, device.role, device.fcm_token, tr_zaman, tr_zaman))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Cihaz {device.role} olarak kaydedildi."}

@app.get("/api/aktif-kod")
def get_aktif_kod():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT e.id, e.kod_renk, l.bina, l.kat, l.birim, e.baslangic_zaman
        FROM emergency_logs e
        LEFT JOIN locations l ON e.location_id = l.id
        WHERE e.durum = 'AKTIF'
        ORDER BY e.id DESC LIMIT 1
    """)
    row = cursor.fetchone()
    
    if row:
        conn.close()
        bina = row['bina'] if row['bina'] else 'Ana Bina'
        kat = row['kat'] if row['kat'] else 'Kat 1'
        birim = row['birim'] if row['birim'] else 'Poliklinik'
        return {
            "has_active": True,
            "status": "AKTIF",
            "log_id": row["id"],
            "kod_renk": row["kod_renk"],
            "konum": f"{bina} - {kat} - {birim}",
            "zaman": row["baslangic_zaman"]
        }
    
    cursor.execute("""
        SELECT e.id, e.kod_renk, l.bina, l.kat, l.birim, e.kabul_zaman, e.kabul_eden_kullanici_id
        FROM emergency_logs e
        LEFT JOIN locations l ON e.location_id = l.id
        WHERE e.durum = 'TAMAMLANDI'
        ORDER BY e.kabul_zaman DESC LIMIT 1
    """)
    accepted_row = cursor.fetchone()
    conn.close()
    
    if accepted_row:
        personel_adi = accepted_row["kabul_eden_kullanici_id"] if accepted_row["kabul_eden_kullanici_id"] else "Nöbetçi Ekip Üyesi"
        bina = accepted_row['bina'] if accepted_row['bina'] else 'Ana Bina'
        kat = accepted_row['kat'] if accepted_row['kat'] else 'Kat 1'
        birim = accepted_row['birim'] if accepted_row['birim'] else 'Poliklinik'
        return {
            "has_active": False,
            "recently_accepted": True,
            "accepted_by": personel_adi,
            "kod_renk": accepted_row["kod_renk"],
            "konum": f"{bina} - {kat} - {birim}"
        }

    return {"has_active": False, "recently_accepted": False}

@app.post("/api/kod-kapat")
def acil_kod_kapat(req: KodKapatRequest):
    # 1. Adım: Türkiye saatini string olarak üretiyoruz
    tr_zaman = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE emergency_logs 
        SET durum = 'TAMAMLANDI', 
            kabul_zaman = ?,    
            kabul_eden_kullanici_id = ?
        WHERE id = ?
    """, (tr_zaman, req.kabul_personel, req.log_id))  
    
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Acil kod çağrısı {req.kabul_personel} tarafından kapatıldı."}

@app.get("/api/admin/logs")
def get_all_logs():
    # Anlık Türkiye saatini string olarak alıyoruz
    suan_tr = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            e.id, 
            e.kod_renk, 
            COALESCE(l.bina || ' - ' || l.kat || ' - ' || l.birim, 'Genel Poliklinik') as konum,
            e.baslangic_zaman,
            e.kabul_zaman,
            e.durum,
            COALESCE(e.kabul_eden_kullanici_id, '-') as kabul_eden,
            ROUND((strftime('%s', COALESCE(e.kabul_zaman, ?)) - strftime('%s', e.baslangic_zaman))) as gecen_saniye
        FROM emergency_logs e
        LEFT JOIN locations l ON e.location_id = l.id
        ORDER BY e.id DESC LIMIT 50
    """, (suan_tr,))  
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# SKS UYUMLU CSV EXPORT ENDPOINT'İ
@app.get("/api/admin/export-csv")
def export_sks_csv():
    # Anlık Türkiye saatini string olarak alıyoruz
    suan_tr = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            e.id as 'Çağrı ID', 
            e.kod_renk as 'Kod Rengi', 
            COALESCE(l.bina || ' - ' || l.kat || ' - ' || l.birim, 'Genel Poliklinik') as 'Konum',
            e.baslangic_zaman as 'Çağrı Zamanı',
            COALESCE(e.kabul_zaman, '-') as 'Müdahale Zamanı',
            COALESCE(e.kabul_eden_kullanici_id, '-') as 'Müdahale Eden Personel',
            ROUND((strftime('%s', COALESCE(e.kabul_zaman, ?)) - strftime('%s', e.baslangic_zaman))) as 'Müdahale Süresi (Saniye)',
            e.durum as 'Durum'
        FROM emergency_logs e
        LEFT JOIN locations l ON e.location_id = l.id
        ORDER BY e.id DESC
    """, (suan_tr,))  
    
    rows = cursor.fetchall()
    conn.close()
    
    # (Devamındaki CSV oluşturma kodların buraya gelecek...)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    
    writer.writerow(['Çağrı ID', 'Kod Rengi', 'Konum', 'Çağrı Zamanı', 'Müdahale Zamanı', 'Müdahale Eden Personel', 'Müdahale Süresi (Saniye)', 'Durum'])
    
    for r in rows:
        writer.writerow([r['Çağrı ID'], r['Kod Rengi'], r['Konum'], r['Çağrı Zamanı'], r['Müdahale Zamanı'], r['Müdahale Eden Personel'], r['Müdahale Süresi (Saniye)'], r['Durum']])

    csv_data = "\ufeff" + output.getvalue()
    
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=SKS_Acil_Kod_Raporu.csv"}
    )

# 5. WEB ADMIN PANEL ARAYÜZÜ (HTML/JS)
@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    html_content = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Acil Kod Sistemi - SKS Yönetim Paneli</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f4f6f9; margin: 20px; }
        .container { max-width: 1100px; margin: 0 auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h2 { color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; margin-top: 0; }
        .section-title { font-size: 18px; font-weight: bold; margin-top: 25px; margin-bottom: 10px; color: #495057; display: flex; align-items: center; justify-content: space-between; }
        .form-group { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        input[type="text"] { flex: 1; min-width: 180px; padding: 10px; border: 1px solid #ccc; border-radius: 5px; }
        button { background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; }
        button:hover { background: #218838; }
        .btn-excel { background: #1D6F42; color: white; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; font-weight: bold; text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
        .btn-excel:hover { background: #155231; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 14px; }
        th { background-color: #007bff; color: white; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .badge { padding: 4px 10px; border-radius: 12px; font-weight: bold; color: white; font-size: 12px; display: inline-block; }
        .badge-MAVİ { background-color: #0056b3; }
        .badge-PEMBE { background-color: #e83e8c; }
        .badge-KIRMIZI { background-color: #dc3545; }
        .badge-BEYAZ { background-color: #6c757d; }
        .status-AKTIF { background-color: #ffc107; color: #212529; padding: 3px 8px; border-radius: 4px; font-weight: bold; animation: pulse 1.5s infinite; }
        .status-TAMAMLANDI { background-color: #28a745; color: white; padding: 3px 8px; border-radius: 4px; font-weight: bold; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .btn-delete { background: #dc3545; padding: 4px 8px; font-size: 12px; color: white; border: none; border-radius: 4px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <h2>🏥 Hastane Acil Kod Yönetim Paneli (SKS Canlı Takip)</h2>
        
        <div class="section-title">
            <span>🚨 Canlı & Geçmiş Acil Kod Takip Ekranı</span>
            <a href="/api/admin/export-csv" class="btn-excel">📊 Excel / CSV Raporu İndir</a>
        </div>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Kod Rengi</th>
                    <th>Konum</th>
                    <th>Çağrı Zamanı</th>
                    <th>Müdahale Eden Personel</th>
                    <th>SKS Müdahale Süresi</th>
                    <th>Durum</th>
                </tr>
            </thead>
            <tbody id="logsTableBody">
                <tr><td colspan="7">Çağrı verileri yükleniyor...</td></tr>
            </tbody>
        </table>

        <div class="section-title" style="margin-top: 40px;">
            <span>📍 Hastane Konum / Birim Tanımlama</span>
        </div>
        <div class="form-group">
            <input type="text" id="bina" placeholder="Bina Adı (ör: Ana Bina, D Blok)">
            <input type="text" id="kat" placeholder="Kat Bilgisi (ör: Kat 2, Zemin Kat)">
            <input type="text" id="birim" placeholder="Birim / Poliklinik (ör: Pedodonti 3)">
            <button onclick="konumEkle()">+ Konum Ekle</button>
        </div>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Bina</th>
                    <th>Kat</th>
                    <th>Birim / Poliklinik</th>
                    <th>İşlem</th>
                </tr>
            </thead>
            <tbody id="konumTableBody">
                <tr><td colspan="5">Konumlar yükleniyor...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        function formatSaniye(saniye) {
            if (!saniye || saniye < 0) return '0 sn';
            const dk = Math.floor(saniye / 60);
            const sn = saniye % 60;
            return dk > 0 ? `${dk} dk ${sn} sn` : `${sn} sn`;
        }

        async function acilKodTakipYukle() {
            try {
                const res = await fetch('/api/admin/logs');
                if (!res.ok) return;
                const logs = await res.json();
                const tbody = document.getElementById('logsTableBody');
                tbody.innerHTML = '';

                if (logs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">Henüz tetiklenmiş acil kod kaydı bulunmuyor.</td></tr>';
                    return;
                }

                logs.forEach(log => {
                    const durumClass = log.durum === 'AKTIF' ? 'status-AKTIF' : 'status-TAMAMLANDI';
                    const sureText = formatSaniye(log.gecen_saniye);
                    
                    tbody.innerHTML += `
                        <tr>
                            <td>#${log.id}</td>
                            <td><span class="badge badge-${log.kod_renk}">${log.kod_renk} KOD</span></td>
                            <td><b>${log.konum || 'Bilinmiyor'}</b></td>
                            <td>${log.baslangic_zaman}</td>
                            <td><b>${log.kabul_eden}</b></td>
                            <td><span style="color: #d9534f; font-weight: bold;">${sureText}</span></td>
                            <td><span class="${durumClass}">${log.durum}</span></td>
                        </tr>
                    `;
                });
            } catch (err) {
                console.error(err);
            }
        }

        async function konumlarıYukle() {
            try {
                const res = await fetch('/api/locations');
                if (!res.ok) return;
                const locations = await res.json();
                const tbody = document.getElementById('konumTableBody');
                tbody.innerHTML = '';
                
                if (locations.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;">Henüz kayıtlı konum yok.</td></tr>';
                    return;
                }

                locations.forEach(loc => {
                    tbody.innerHTML += `
                        <tr>
                            <td>${loc.id}</td>
                            <td>${loc.bina}</td>
                            <td>${loc.kat}</td>
                            <td>${loc.birim}</td>
                            <td><button class="btn-delete" onclick="konumSil(${loc.id})">Sil</button></td>
                        </tr>
                    `;
                });
            } catch (err) {
                console.error(err);
            }
        }

        async function konumEkle() {
            const bina = document.getElementById('bina').value;
            const kat = document.getElementById('kat').value;
            const birim = document.getElementById('birim').value;

            if (!bina || !kat || !birim) {
                alert('Lütfen tüm alanları doldurun!');
                return;
            }

            try {
                const res = await fetch('/api/locations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ bina, kat, birim })
                });

                if (!res.ok) throw new Error('Ekleme başarısız!');

                document.getElementById('bina').value = '';
                document.getElementById('kat').value = '';
                document.getElementById('birim').value = '';
                konumlarıYukle();
            } catch (err) {
                alert('Hata: ' + err.message);
            }
        }

        async function konumSil(id) {
            if (confirm('Bu konumu silmek istediğinize emin misiniz?')) {
                try {
                    await fetch('/api/locations/' + id, { method: 'DELETE' });
                    konumlarıYukle();
                } catch (err) {
                    alert('Silme hatası: ' + err.message);
                }
            }
        }

        window.onload = () => {
            konumlarıYukle();
            acilKodTakipYukle();
            setInterval(acilKodTakipYukle, 3000);
        };
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

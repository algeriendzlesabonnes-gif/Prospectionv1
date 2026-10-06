# -*- coding: utf-8 -*-
"""Version WEB de PROSPECT-FR (téléphone / tablette / PC). Utilise prospect.py.
Variables d'environnement : APP_PASSWORD (obligatoire), DATABASE_URL (optionnel, base Postgres gratuite)."""
import csv, hashlib, hmac, io, json, os, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from functools import wraps
from flask import Flask, Response, jsonify, redirect, request, session

import prospect as P

P.CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/cache")
PASSWORD = os.environ.get("APP_PASSWORD")
if not PASSWORD:
    raise SystemExit("Définis la variable d'environnement APP_PASSWORD")
DB_URL = os.environ.get("DATABASE_URL")
STATUTS = ["À appeler", "Appelé - pas de réponse", "À rappeler", "Intéressé", "Refusé", "Client"]

app = Flask(__name__)
app.secret_key = hashlib.sha256(("k" + PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))


# ------------------------------- BASE DE DONNÉES ------------------------------
def run(sql, args=(), fetch=False):
    if DB_URL:
        import psycopg
        c = psycopg.connect(DB_URL, autocommit=True)
        sql = sql.replace("?", "%s")
    else:
        c = sqlite3.connect(os.environ.get("DB_PATH", "/tmp/prospects.db"), timeout=30)
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        if not DB_URL:
            c.commit()
        return rows
    finally:
        c.close()


run("""CREATE TABLE IF NOT EXISTS prospects (osm_id TEXT PRIMARY KEY, dep TEXT, score INTEGER, name TEXT,
       city TEXT, data TEXT, statut TEXT DEFAULT 'À appeler')""")


def save(r):
    row = P.to_row(r)
    run("""INSERT INTO prospects(osm_id,dep,score,name,city,data) VALUES (?,?,?,?,?,?)
           ON CONFLICT (osm_id) DO UPDATE SET score=excluded.score, data=excluded.data""",
        (r["osm_id"], r["dep"], int(r["score"]), r["name"], r.get("city", ""), json.dumps(row, ensure_ascii=False)))


def where():
    sql, a = "WHERE score >= ?", [int(request.args.get("min") or 0)]
    for key, col in (("statut", "statut"), ("dep", "dep")):
        if request.args.get(key):
            sql += f" AND {col} = ?"
            a.append(request.args[key])
    q = (request.args.get("q") or "").strip().lower()
    if q:
        sql += " AND (lower(name) LIKE ? OR lower(city) LIKE ?)"
        a += [f"%{q}%", f"%{q}%"]
    return sql, a


# ---------------------------------- ANALYSE -----------------------------------
JOB = {"run": False, "msg": "Prêt", "done": 0, "total": 0}


class Args:
    sirene = True


def worker(deps, limit, force):
    JOB.update(run=True, done=0, total=0, msg="Démarrage...")
    try:
        for dep in deps:
            JOB["msg"] = f"Dép. {dep} : téléchargement des données (quelques minutes)"
            recs = P.dedupe_and_filter_chains(P.parse_osm(P.fetch_osm(dep), dep))
            recs = [r for r in recs if r["phone"] or r["email"] or r["website"]]
            if not force:
                known = {x[0] for x in run("SELECT osm_id FROM prospects WHERE dep = ?", (dep,), True)}
                recs = [r for r in recs if r["osm_id"] not in known]
            if limit:
                recs = recs[:limit]
            JOB["total"] += len(recs)
            JOB["msg"] = f"Dép. {dep} : analyse des entreprises"
            with ThreadPoolExecutor(max_workers=6) as ex:
                for f in as_completed([ex.submit(P.enrich, r, Args) for r in recs]):
                    JOB["done"] += 1
                    try:
                        r = f.result()
                        if r["audit_state"] != "inconnu" and (r["phone"] or r["email_ok"]):
                            save(r)
                    except Exception:
                        pass
        JOB["msg"] = "Terminé ✅"
    except Exception as e:
        JOB["msg"] = f"Erreur : {e} (relance, ce qui est fait est conservé)"
    finally:
        JOB["run"] = False


# ----------------------------------- ROUTES -----------------------------------
def auth(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("ok"):
            return (jsonify(error="auth"), 401) if request.path.startswith("/api") else redirect("/login")
        return f(*a, **k)
    return w


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        time.sleep(1)  # freine les essais de mots de passe
        if hmac.compare_digest(request.form.get("p", ""), PASSWORD):
            session.permanent, session["ok"] = True, True
            return redirect("/")
        err = "Mot de passe incorrect"
    return Response(LOGIN.replace("__E__", err), mimetype="text/html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/health")
def health():
    return "ok"


@app.route("/")
@auth
def home():
    return Response(PAGE.replace("__ST__", json.dumps(STATUTS)), mimetype="text/html")


@app.route("/api/job")
@auth
def job():
    return jsonify(JOB)


@app.route("/api/start", methods=["POST"])
@auth
def start():
    if JOB["run"]:
        return jsonify(error="Une analyse est déjà en cours"), 409
    b = request.get_json(force=True, silent=True) or {}
    raw = str(b.get("deps", "")).strip().upper()
    deps = P.DEPARTEMENTS if raw == "ALL" else [d.strip().zfill(2) if d.strip().isdigit() else d.strip() for d in raw.split(",") if d.strip()]
    if not deps or any(d not in P.DEPARTEMENTS for d in deps):
        return jsonify(error="Département invalide (ex : 42 ou 42,69 ou all)"), 400
    try:
        limit = int(b.get("limit") or 0)
    except ValueError:
        limit = 0
    threading.Thread(target=worker, args=(deps, limit, bool(b.get("force"))), daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/prospects")
@auth
def prospects():
    w, a = where()
    total = run(f"SELECT COUNT(*) FROM prospects {w}", a, True)[0][0]
    rows = run(f"SELECT osm_id, data, statut FROM prospects {w} ORDER BY score DESC, osm_id LIMIT 40 OFFSET ?",
               a + [int(request.args.get("off") or 0)], True)
    return jsonify(total=total, items=[{"id": r[0], "d": json.loads(r[1]), "statut": r[2]} for r in rows])


@app.route("/api/statut", methods=["POST"])
@auth
def statut():
    b = request.get_json(force=True, silent=True) or {}
    if b.get("statut") not in STATUTS:
        return jsonify(error="statut invalide"), 400
    run("UPDATE prospects SET statut = ? WHERE osm_id = ?", (b["statut"], str(b.get("id"))))
    return jsonify(ok=True)


@app.route("/export.csv")
@auth
def export():
    w, a = where()
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=P.COLUMNS, delimiter=";")
    wr.writeheader()
    for r in run(f"SELECT data, statut FROM prospects {w} ORDER BY score DESC", a, True):
        d = json.loads(r[0])
        d["statut_appel"] = r[1]
        # protection contre les formules Excel malveillantes venant de données externes
        wr.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for k, v in d.items()})
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=prospects.csv"})


# ------------------------------------ PAGES -----------------------------------
LOGIN = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connexion</title><style>body{font-family:system-ui;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f5f7}
form{background:#fff;padding:24px;border-radius:14px;width:min(320px,90vw);box-shadow:0 2px 12px #0002}
input,button{width:100%;padding:12px;margin-top:10px;font-size:16px;border-radius:8px;border:1px solid #ccc;box-sizing:border-box}
button{background:#2563eb;color:#fff;border:0}p{color:#c00}</style></head><body><form method="post"><h3>Prospection FR</h3>
<input type="password" name="p" placeholder="Mot de passe" autofocus><button>Entrer</button><p>__E__</p></form></body></html>"""

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prospection FR</title><style>
:root{--bg:#f4f5f7;--c:#fff;--t:#111;--m:#667;--b:#2563eb;--l:#e3e5ea}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--c:#1a1d24;--t:#eee;--m:#99a;--l:#2a2e38}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui;background:var(--bg);color:var(--t)}
header{display:flex;justify-content:space-between;padding:12px 16px;background:var(--c);border-bottom:1px solid var(--l)}
header a{color:var(--m);text-decoration:none}main{max-width:760px;margin:auto;padding:12px}
.box{background:var(--c);border:1px solid var(--l);border-radius:14px;padding:14px;margin-bottom:14px}
h3{margin:0 0 8px}input,select,button,.btn{font-size:16px;padding:10px;border-radius:8px;border:1px solid var(--l);background:var(--c);color:var(--t);margin:4px 0}
input,select{width:100%}button,.btn{cursor:pointer;text-decoration:none;display:inline-block}
#go{background:var(--b);color:#fff;border:0;width:100%}.bar{height:6px;background:var(--l);border-radius:4px;margin-top:8px}
.bar i{display:block;height:100%;width:0;background:var(--b);border-radius:4px}#job{color:var(--m);font-size:14px;margin-top:6px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.card{border:1px solid var(--l);border-radius:12px;padding:12px;margin:10px 0}
.top{display:flex;justify-content:space-between;gap:8px}.sub,.pb{color:var(--m);font-size:14px;margin-top:4px}
.badge{padding:2px 9px;border-radius:99px;color:#fff;font-weight:600;height:fit-content}.sa{background:#16a34a}.sb{background:#d97706}
.act{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}.act .btn{padding:8px 10px;font-size:14px}#more{width:100%;display:none}
</style></head><body><header><b>Prospection FR</b><a href="/logout">Quitter</a></header><main>
<section class="box"><h3>Lancer une analyse</h3>
<input id="deps" value="42" placeholder="Départements : 42 ou 42,69 ou all">
<input id="lim" type="number" placeholder="Limite pour tester (vide = tout)">
<label><input type="checkbox" id="force" style="width:auto"> Refaire les fiches déjà analysées</label>
<button id="go">Lancer</button><div id="job"></div><div class="bar"><i id="fill"></i></div></section>
<section class="box"><h3>Prospects <span id="count"></span></h3>
<div class="row"><select id="min"><option value="90">Score ≥ 90</option><option value="80" selected>Score ≥ 80</option><option value="60">Score ≥ 60</option><option value="0">Tous</option></select>
<select id="st"><option value="">Tous statuts</option></select></div>
<div class="row"><input id="q" placeholder="Rechercher nom / ville"><input id="dep" placeholder="Département (ex 42)"></div>
<a id="exp" class="btn" href="/export.csv">⬇️ Exporter en CSV</a><div id="list"></div><button id="more">Voir plus</button></section></main>
<script>
const ST=__ST__,$=id=>document.getElementById(id);let off=0,n=0;
const api=async(u,o)=>{const r=await fetch(u,o);if(r.status==401){location='/login';return null}return r.json()};
const qs=()=>new URLSearchParams({min:$('min').value,statut:$('st').value,q:$('q').value,dep:$('dep').value});
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
ST.forEach(s=>{const o=el('option','',s);o.value=s;$('st').append(o)});
async function load(reset){if(reset){off=0;$('list').innerHTML=''}const p=qs();p.set('off',off);const d=await api('/api/prospects?'+p);if(!d)return;
 $('count').textContent='('+d.total+')';d.items.forEach(add);off+=d.items.length;$('more').style.display=off<d.total?'block':'none';$('exp').href='/export.csv?'+qs()}
function add(p){const d=p.d,c=el('div','card'),top=el('div','top');
 top.append(el('b','',d.nom),el('span','badge s'+(d.score_besoin>=90?'a':'b'),d.score_besoin));c.append(top);
 c.append(el('div','sub',[d.ville,d.categorie,d.effectif&&d.effectif+' sal.'].filter(Boolean).join(' · ')));
 c.append(el('div','pb',d.problemes_constates));const act=el('div','act');
 if(d.telephone){const a=el('a','btn','📞 '+d.telephone);a.href='tel:'+d.telephone.replace(/ /g,'');act.append(a)}
 if(d.email){const a=el('a','btn','✉️ Email');a.href='mailto:'+d.email;act.append(a)}
 if(d.site_web){let u=d.site_web;if(!/^https?:\\/\\//i.test(u))u='http://'+u;const a=el('a','btn','🌐 Site');a.href=u;a.target='_blank';a.rel='noopener noreferrer';act.append(a)}
 const g=el('a','btn','🔎 Vérifier');g.href=d.recherche_google;g.target='_blank';g.rel='noopener noreferrer';act.append(g);
 const cp=el('button','','📋 Accroche');cp.onclick=()=>{navigator.clipboard.writeText(d.accroche_appel);cp.textContent='✅ Copié'};act.append(cp);c.append(act);
 const sel=el('select');ST.forEach(s=>{const o=el('option','',s);o.value=s;if(s==p.statut)o.selected=true;sel.append(o)});
 sel.onchange=()=>api('/api/statut',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:p.id,statut:sel.value})});
 c.append(sel);$('list').append(c)}
async function poll(){const j=await api('/api/job');if(!j)return;$('job').textContent=j.msg+(j.total?' — '+j.done+'/'+j.total:'');
 $('fill').style.width=(j.total?100*j.done/j.total:0)+'%';if(j.run&&++n%5==0)load(true);setTimeout(poll,j.run?3000:10000)}
$('go').onclick=async()=>{const r=await api('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({deps:$('deps').value,limit:$('lim').value,force:$('force').checked})});if(r&&r.error)$('job').textContent=r.error;else{n=0;poll()}};
['min','st'].forEach(i=>$(i).onchange=()=>load(true));['q','dep'].forEach(i=>$(i).oninput=()=>{clearTimeout(window._d);window._d=setTimeout(()=>load(true),400)});
$('more').onclick=()=>load(false);load(true);poll();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

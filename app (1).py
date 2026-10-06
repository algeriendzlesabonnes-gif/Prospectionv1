# -*- coding: utf-8 -*-
"""Version WEB de PROSPECT-FR (téléphone / tablette / PC). Utilise prospect.py.
Variables d'environnement : APP_PASSWORD (obligatoire), DATABASE_URL (optionnel, base Postgres gratuite)."""
import requests
import csv, hashlib, hmac, io, json, os, sqlite3, threading, time
from collections import deque
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
JOB = {"run": False, "msg": "Prêt", "done": 0, "total": 0, "phase": -1, "found": 0, "log": deque(maxlen=200)}


class Args:
    sirene = False


def L(msg):
    JOB["log"].append(time.strftime("%H:%M:%S ") + msg)


def step(i, total, msg):
    JOB.update(phase=i, done=0, total=total, msg=msg)
    L(msg)


def worker(deps, limit, force):
    JOB.update(run=True, done=0, total=0, found=0, phase=0, msg="Démarrage")
    JOB["log"].clear()
    try:
        for dep in deps:
            step(0, 0, f"Dép. {dep} : téléchargement des données OpenStreetMap")
            recs = P.dedupe_and_filter_chains(P.parse_osm(P.fetch_osm(dep, log=L), dep))
            recs = [r for r in recs if r["phone"] or r["email"] or r["website"]]
            if not force:
                known = {x[0] for x in run("SELECT osm_id FROM prospects WHERE dep = ?", (dep,), True)}
                recs = [r for r in recs if r["osm_id"] not in known]
            nosite = [r for r in recs if not r["website"]][: limit or None]
            withsite = [r for r in recs if r["website"]][: limit or None]
            L(f"{len(nosite)} entreprises sans site, {len(withsite)} avec site à auditer")
            kept = []

            def handle(r):
                if r["audit_state"] != "inconnu" and (r["phone"] or r["email_ok"]):
                    save(r)
                    JOB["found"] += 1
                    if r["score"] >= 80:
                        kept.append(r)

            step(1, len(nosite), f"Dép. {dep} : enregistrement des entreprises sans site")
            for r in nosite:
                handle(P.enrich(r, Args))
                JOB["done"] += 1
            L(f"✅ {JOB['found']} prospects déjà disponibles (liste ci-dessous)")

            step(2, len(withsite), f"Dép. {dep} : audit des sites web existants")
            with ThreadPoolExecutor(max_workers=8) as ex:
                for f in as_completed([ex.submit(P.enrich, r, Args) for r in withsite]):
                    JOB["done"] += 1
                    try:
                        handle(f.result())
                    except Exception as e:
                        L(f"⚠ fiche ignorée ({type(e).__name__})")

            top = sorted(kept, key=lambda r: -r["score"])[:300]
            step(3, len(top), f"Dép. {dep} : vérification au registre officiel")
            for r in top:
                JOB["done"] += 1
                try:
                    sir = P.sirene_match(r)
                    if sir:
                        r["sirene"], r["registre"] = sir, f"actif - SIREN {sir['siren']}"
                        save(r)
                except Exception:
                    pass
        JOB.update(phase=4, msg="Terminé ✅")
        L("🎉 Terminé")
    except Exception as e:
        JOB["msg"] = f"Erreur : {e}"
        L(f"❌ {e}")
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
    d = dict(JOB)
    d["log"] = list(JOB["log"])
    return jsonify(d)


@app.route("/api/stats")
@auth
def stats():
    r = run("SELECT COUNT(*), SUM(CASE WHEN score >= 90 THEN 1 ELSE 0 END), SUM(CASE WHEN score >= 80 THEN 1 ELSE 0 END) FROM prospects", (), True)[0]
    return jsonify(all=r[0] or 0, a=r[1] or 0, b=r[2] or 0)


@app.route("/api/diag", methods=["POST"])
@auth
def diag():
    L("🔧 Diagnostic des connexions...")
    tests = [
        ("OpenStreetMap", lambda: requests.post(P.OVERPASS_URLS[0], data={"data": "[out:json][timeout:15];node(1);out;"}, headers={"User-Agent": P.UA}, timeout=25).status_code),
        ("OpenStreetMap (secours)", lambda: requests.post(P.OVERPASS_URLS[1], data={"data": "[out:json][timeout:15];node(1);out;"}, headers={"User-Agent": P.UA}, timeout=25).status_code),
        ("Registre officiel", lambda: P.sirene_get({"q": "boulangerie", "per_page": 1}) is not None),
        ("Accès aux sites web", lambda: P.http_get("https://example.com", timeout=10).status_code),
    ]
    for name, fn in tests:
        t0 = time.time()
        try:
            res = fn()
            L(f"{'✅' if res in (200, True) else '⚠️'} {name} : {res} ({time.time() - t0:.1f}s)")
        except Exception as e:
            L(f"❌ {name} : {type(e).__name__}")
    return jsonify(ok=True)


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

PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0d14"><title>Prospection FR</title><style>
:root{--bg:#0b0d14;--c:#ffffff0d;--l:#ffffff1a;--t:#eef0f6;--m:#9aa3b8;--a:#6366f1;--b:#22d3ee;--ok:#34d399;--w:#fbbf24}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--t);min-height:100vh;
background-image:radial-gradient(60vw 40vh at 10% -10%,#6366f133,transparent),radial-gradient(50vw 40vh at 100% 0,#22d3ee22,transparent)}
header{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:center;padding:12px 18px;backdrop-filter:blur(14px);background:#0b0d14aa;border-bottom:1px solid var(--l)}
.logo{font-weight:800;letter-spacing:-.3px;display:flex;gap:8px;align-items:center}.logo i{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--a),var(--b));display:inline-block;animation:spin 8s linear infinite}
header a{color:var(--m);text-decoration:none;font-size:14px}main{max-width:820px;margin:auto;padding:16px}
h1{font-size:clamp(26px,6vw,38px);line-height:1.1;margin:18px 0 6px;letter-spacing:-1px;animation:up .6s both}
h1 span{background:linear-gradient(90deg,var(--a),var(--b),var(--a));background-size:200%;-webkit-background-clip:text;color:transparent;animation:flow 5s linear infinite}
.sub{color:var(--m);margin:0 0 18px;animation:up .6s .1s both}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.st{background:var(--c);border:1px solid var(--l);border-radius:16px;padding:14px;animation:up .6s .15s both;backdrop-filter:blur(10px)}
.st b{font-size:clamp(22px,6vw,32px);display:block}.st small{color:var(--m)}
.box{background:var(--c);border:1px solid var(--l);border-radius:18px;padding:16px;margin-bottom:14px;backdrop-filter:blur(10px);animation:up .6s .2s both}
h3{margin:0 0 10px;font-size:17px}input,select,button,.btn{font:inherit;font-size:15px;padding:11px 12px;border-radius:12px;border:1px solid var(--l);background:#ffffff0f;color:var(--t);margin:4px 0;outline:none;transition:.2s}
input,select{width:100%}input:focus,select:focus{border-color:var(--a);box-shadow:0 0 0 3px #6366f133}option{color:#111}
button,.btn{cursor:pointer;text-decoration:none;display:inline-block}.btn:hover,button:hover{transform:translateY(-1px);background:#ffffff1c}
#go{width:100%;border:0;color:#fff;font-weight:700;background:linear-gradient(90deg,var(--a),var(--b));background-size:200%;padding:13px}
#go:hover{background-position:100%;box-shadow:0 8px 24px #6366f155}#go[disabled]{opacity:.6;cursor:wait}
.steps{display:flex;gap:6px;margin:12px 0 8px}.steps div{flex:1;text-align:center;font-size:11px;color:var(--m);padding-top:6px;border-top:3px solid var(--l);transition:.4s}
.steps .on{color:var(--t);border-color:var(--b)}.steps .done{border-color:var(--ok);color:var(--ok)}
.bar{height:8px;background:var(--l);border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;width:0;border-radius:9px;transition:width .6s;
background:linear-gradient(90deg,var(--a),var(--b),var(--a));background-size:200%;animation:flow 2s linear infinite}
#msg{font-size:14px;color:var(--m);margin-top:8px;display:flex;gap:8px;align-items:center}.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);animation:pulse 1.4s infinite}
#log{margin:10px 0 0;background:#05060a;border:1px solid var(--l);border-radius:12px;padding:10px;font:12px/1.5 ui-monospace,Consolas,monospace;color:#9fe8c0;max-height:170px;overflow:auto;white-space:pre-wrap}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.card{background:#ffffff08;border:1px solid var(--l);border-radius:16px;padding:14px;margin:10px 0;animation:up .45s both;transition:.25s}
.card:hover{transform:translateY(-3px);border-color:#6366f188;box-shadow:0 10px 30px #0006}.top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.top b{font-size:16px}.badge{min-width:44px;text-align:center;padding:4px 10px;border-radius:99px;font-weight:800;color:#06210f;background:linear-gradient(135deg,var(--ok),#a7f3d0)}.badge.w{background:linear-gradient(135deg,var(--w),#fde68a);color:#3b2a00}
.meta,.pb{color:var(--m);font-size:13.5px;margin-top:5px}.act{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}.act .btn,.act button{padding:8px 11px;font-size:13.5px;margin:0}
.call{background:linear-gradient(90deg,#16a34a,#22c55e)!important;border:0!important;color:#fff!important;font-weight:700}
#more{width:100%;display:none}.empty{text-align:center;color:var(--m);padding:26px 8px}
#toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,90px);background:#111827;border:1px solid var(--l);padding:10px 16px;border-radius:12px;transition:.35s;z-index:9}#toast.on{transform:translate(-50%,0)}
@keyframes up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}@keyframes flow{to{background-position:200% 0}}
@keyframes pulse{50%{opacity:.3;transform:scale(.7)}}@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body><header><div class="logo"><i></i>Prospection FR</div><a href="/logout">Quitter</a></header><main>
<h1>Trouvez les entreprises qui ont <span>besoin d'un site web</span></h1>
<p class="sub">Toute la France · données officielles et contacts vérifiés · 100 % gratuit</p>
<div class="stats"><div class="st"><b id="s1">0</b><small>Prospects chauds</small></div><div class="st"><b id="s2">0</b><small>Priorité A (90+)</small></div><div class="st"><b id="s3">0</b><small>Total analysé</small></div></div>
<section class="box"><h3>🚀 Lancer une analyse</h3>
<div class="row"><input id="deps" value="42" placeholder="Départements : 42 ou 42,69"><input id="lim" type="number" placeholder="Limite test (vide = tout)"></div>
<label style="font-size:14px;color:var(--m)"><input type="checkbox" id="force" style="width:auto"> Refaire les fiches déjà analysées</label>
<button id="go">Lancer l'analyse</button>
<div class="steps" id="steps"><div>Données</div><div>Sans site</div><div>Audit sites</div><div>Registre</div></div>
<div class="bar"><i id="fill"></i></div><div id="msg">Prêt</div><pre id="log">En attente...</pre>
<button id="diag" style="margin-top:10px;font-size:13px;padding:8px 12px">🔧 Tester les connexions</button></section>
<section class="box"><h3>📇 Prospects <span id="count" style="color:var(--m);font-weight:400"></span></h3>
<div class="row"><select id="min"><option value="90">Score 90+</option><option value="80" selected>Score 80+</option><option value="60">Score 60+</option><option value="0">Tous</option></select>
<select id="st"><option value="">Tous les statuts</option></select></div>
<div class="row"><input id="q" placeholder="🔍 Nom ou ville"><input id="dep" placeholder="Département (ex 42)"></div>
<a id="exp" class="btn" href="/export.csv">⬇️ Exporter en CSV</a><div id="list"></div><button id="more">Voir plus</button></section></main><div id="toast"></div>
<script>
const ST=__ST__,$=id=>document.getElementById(id);let off=0,n=0,last='';
const api=async(u,o)=>{const r=await fetch(u,o);if(r.status==401){location='/login';return null}return r.json()};
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
const qs=()=>new URLSearchParams({min:$('min').value,statut:$('st').value,q:$('q').value,dep:$('dep').value});
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const toast=m=>{const t=$('toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1800)};
function count(id,v){const e=$(id),s=+e.dataset.v||0;e.dataset.v=v;const t0=performance.now();(function f(t){const k=Math.min(1,(t-t0)/700);e.textContent=Math.round(s+(v-s)*k);if(k<1)requestAnimationFrame(f)})(t0)}
ST.forEach(s=>{const o=el('option','',s);o.value=s;$('st').append(o)});
async function stats(){const s=await api('/api/stats');if(s){count('s1',s.b);count('s2',s.a);count('s3',s.all)}}
async function load(reset){if(reset){off=0;$('list').innerHTML=''}const p=qs();p.set('off',off);const d=await api('/api/prospects?'+p);if(!d)return;
 $('count').textContent='('+d.total+')';d.items.forEach((x,i)=>add(x,i));off+=d.items.length;$('more').style.display=off<d.total?'block':'none';$('exp').href='/export.csv?'+qs();
 if(reset&&!d.total)$('list').append(el('div','empty','Aucun prospect pour l\'instant. Lance une analyse : les premiers résultats arrivent dès que les données sont téléchargées.'))}
function add(p,i){const d=p.d,c=el('div','card');c.style.animationDelay=Math.min(i,10)*40+'ms';const top=el('div','top');
 top.append(el('b','',d.nom),el('span','badge'+(d.score_besoin>=90?'':' w'),d.score_besoin));c.append(top);
 c.append(el('div','meta',[d.ville,d.categorie,d.effectif&&d.effectif+' sal.',d.dirigeant].filter(Boolean).join(' · ')));
 c.append(el('div','pb',d.problemes_constates));const act=el('div','act');
 if(d.telephone){const a=el('a','btn call','📞 '+d.telephone);a.href='tel:'+d.telephone.replace(/ /g,'');act.append(a)}
 if(d.email){const a=el('a','btn','✉️ Email');a.href='mailto:'+d.email;act.append(a)}
 if(d.site_web){let u=d.site_web;if(!u.startsWith('http'))u='http://'+u;const a=el('a','btn','🌐 Site');a.href=u;a.target='_blank';a.rel='noopener noreferrer';act.append(a)}
 const g=el('a','btn','🔎 Vérifier');g.href=d.recherche_google;g.target='_blank';g.rel='noopener noreferrer';act.append(g);
 const cp=el('button','','📋 Accroche');cp.onclick=()=>{navigator.clipboard.writeText(d.accroche_appel);toast('Accroche copiée ✅')};act.append(cp);c.append(act);
 const sel=el('select');ST.forEach(s=>{const o=el('option','',s);o.value=s;if(s==p.statut)o.selected=true;sel.append(o)});
 sel.onchange=()=>post('/api/statut',{id:p.id,statut:sel.value}).then(()=>toast('Statut enregistré'));c.append(sel);$('list').append(c)}
async function poll(){const j=await api('/api/job');if(!j)return;
 $('msg').replaceChildren(...(j.run?[el('span','dot')]:[]),document.createTextNode(j.msg+(j.total?' — '+j.done+'/'+j.total:'')+(j.found?' · '+j.found+' prospects':'')));
 $('fill').style.width=(j.run||j.phase==4?(j.phase==4?100:(j.total?100*j.done/j.total:5)):0)+'%';
 [...$('steps').children].forEach((s,i)=>s.className=j.phase==4||i<j.phase?'done':(i==j.phase&&j.run?'on':''));
 const t=j.log.join(String.fromCharCode(10));if(t&&t!==last){last=t;$('log').textContent=t;$('log').scrollTop=1e9}
 $('go').disabled=j.run;if(j.run&&++n%4==0){load(true);stats()}setTimeout(poll,j.run?2000:8000)}
$('go').onclick=async()=>{const r=await post('/api/start',{deps:$('deps').value,limit:$('lim').value,force:$('force').checked});
 if(r&&r.error)toast(r.error);else{n=0;toast('Analyse lancée');poll()}};
$('diag').onclick=async()=>{toast('Test en cours...');await post('/api/diag');poll()};
['min','st'].forEach(i=>$(i).onchange=()=>load(true));['q','dep'].forEach(i=>$(i).oninput=()=>{clearTimeout(window._d);window._d=setTimeout(()=>load(true),400)});
$('more').onclick=()=>load(false);load(true);stats();poll();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

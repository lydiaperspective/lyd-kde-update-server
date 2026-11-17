import os, sqlite3, secrets, hashlib, hmac, time, json, re
from datetime import datetime, timezone
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify, abort, flash

DB = os.getenv('STATE_DB', '/state/manager.db')
app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_SECURE=True, PERMANENT_SESSION_LIFETIME=3600)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS builds (id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, upstream_branch TEXT NOT NULL, build_iso INTEGER NOT NULL, status TEXT NOT NULL, queued_at TEXT NOT NULL, started_at REAL, finished_at REAL, commit_sha TEXT, previous_sha TEXT, log_path TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0, changed_files TEXT DEFAULT '[]');
'''
@contextmanager
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c=sqlite3.connect(DB, timeout=30); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def setting(k, default):
    with db() as c:
        r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
    return r['value'] if r else str(default)

def set_setting(k,v):
    with db() as c: c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v)))

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def hash_pw(pw, salt=None):
    salt=salt or secrets.token_bytes(16); key=hashlib.pbkdf2_hmac('sha256',pw.encode(),salt,310000)
    return salt.hex()+':'+key.hex()
def verify_pw(pw, saved):
    try:
        s,k=saved.split(':'); return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',pw.encode(),bytes.fromhex(s),310000).hex(),k)
    except Exception: return False

def require_login(fn):
    @wraps(fn)
    def inner(*a,**kw):
        if not session.get('user'): return redirect(url_for('login', next=request.path))
        return fn(*a,**kw)
    return inner

def csrf():
    token=session.get('csrf')
    if not token: token=secrets.token_urlsafe(32); session['csrf']=token
    return token

def check_csrf():
    if not hmac.compare_digest(str(request.form.get('_csrf','')),str(session.get('csrf',''))): abort(400,'Invalid CSRF token')

BASE='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KDE Linux Update Manager</title><style>
:root{color-scheme:dark;--bg:#10131a;--panel:#191f2b;--line:#2b3444;--muted:#9aa8bc;--accent:#7c9cff;--good:#56c596;--bad:#ff7777}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e8edf6;font:15px system-ui,sans-serif}header{padding:18px 4vw;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}header a{color:var(--muted);text-decoration:none;margin-left:16px}.wrap{max-width:1200px;margin:28px auto;padding:0 20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}.muted{color:var(--muted)}.big{font-size:26px;font-weight:650}.btn{background:#324a83;color:white;border:0;padding:9px 14px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}.btn.secondary{background:#303948}.btn.danger{background:#873d4b}input,select{background:#0e131c;color:#e8edf6;border:1px solid var(--line);border-radius:7px;padding:9px;width:100%;margin:5px 0 12px}label{display:block;color:#c5cfde;margin-top:8px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}code,pre{font-family:ui-monospace,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b0f16;border-radius:8px;padding:14px;max-height:560px;overflow:auto}.status{font-weight:650}.success{color:var(--good)}.failed,.cancelled{color:var(--bad)}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.row form{margin:0}.small{font-size:13px}.bar{height:7px;background:#303948;border-radius:8px;overflow:hidden}.bar span{display:block;background:var(--accent);height:100%;width:100%;animation:pulse 1.4s infinite alternate}@keyframes pulse{from{opacity:.45}to{opacity:1}}@media(max-width:700px){table{font-size:13px}td,th{padding:7px}}
</style></head><body><header><strong>KDE Linux · Update Manager</strong><nav><a href="{{url_for('index')}}">Dashboard</a><a href="{{url_for('settings_page')}}">Settings</a><a href="{{url_for('logout')}}">Sign out</a></nav></header><main class="wrap">{% with msgs=get_flashed_messages() %}{% for m in msgs %}<div class="panel">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main></body></html>'''

def page(body, **ctx): return render_template_string(BASE,body=render_template_string(body,csrf=csrf(),**ctx))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        saved=setting('password_hash','')
        if request.form.get('username')==setting('username',os.getenv('MANAGER_USERNAME','admin')) and saved and verify_pw(request.form.get('password',''),saved):
            session.clear(); session.permanent=True; session['user']=request.form['username']; csrf(); return redirect(request.args.get('next') or url_for('index'))
        time.sleep(1); flash('Invalid username or password.')
    return render_template_string('''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in</title><style>body{background:#10131a;color:#e8edf6;font:16px system-ui;display:grid;place-items:center;min-height:100vh}.box{background:#191f2b;padding:28px;border:1px solid #2b3444;border-radius:12px;width:min(380px,92vw)}input{display:block;width:100%;padding:12px;margin:10px 0;background:#0e131c;color:white;border:1px solid #344055;border-radius:7px}button{padding:11px;width:100%;background:#405eaa;color:white;border:0;border-radius:7px}</style></head><body><form class="box" method="post"><h2>Update Manager</h2><p>Sign in to manage builds.</p><input name="username" placeholder="Username" required autocomplete="username"><input type="password" name="password" placeholder="Password" required autocomplete="current-password"><button>Sign in</button></form></body></html>''')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
@require_login
def index():
    with db() as c:
        builds=c.execute('SELECT * FROM builds ORDER BY id DESC LIMIT 30').fetchall()
        active=c.execute("SELECT * FROM builds WHERE status IN ('queued','running','cancelling') ORDER BY id LIMIT 1").fetchone()
    rows=''.join(f'''<tr><td>#{b['id']}</td><td>{b['branch']}</td><td class="status {b['status']}">{b['status']}</td><td>{(b['commit_sha'] or '')[:12]}</td><td>{b['queued_at'][:19].replace('T',' ')}</td><td><a href="{url_for('build_detail',bid=b['id'])}">Details</a></td></tr>''' for b in builds)
    active_html=f'''<p><b>#{active['id']} · {active['branch']}</b> — {active['status']}</p><div class="bar"><span></span></div><p class="muted small">Build worker processes one branch at a time. Live log refreshes automatically.</p>''' if active else '<p class="muted">No build currently running.</p>'
    body='''<div class="grid"><section class="panel"><div class="muted">Worker</div><div class="big">{{worker}}</div></section><section class="panel"><div class="muted">Queued / running</div><div class="big">{{active_count}}</div></section><section class="panel"><div class="muted">Configured branches</div><div class="big">{{branches}}</div></section></div><section class="panel"><h2>Build control</h2>{{active_html|safe}}<div class="row"><form method="post" action="{{url_for('start_build')}}"><input type="hidden" name="_csrf" value="{{csrf}}"><label>Branch</label><select name="branch">{% for b in branch_list %}<option>{{b}}</option>{% endfor %}</select><button class="btn">Start build</button></form>{% if active %}<form method="post" action="{{url_for('cancel_build',bid=active['id'])}}"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn danger">Stop current build</button></form>{% endif %}</div></section><section class="panel"><h2>Recent builds</h2><table><thead><tr><th>ID</th><th>Branch</th><th>Status</th><th>Commit</th><th>Queued</th><th></th></tr></thead><tbody>{{rows|safe}}</tbody></table></section>'''
    with db() as c: ac=c.execute("SELECT count(*) n FROM builds WHERE status IN ('queued','running','cancelling')").fetchone()['n']
    bl=[x for x in setting('branches',setting('target_branch','master')).split(',') if x]
    return page(body,worker=setting('worker_status','Waiting'),active_count=ac,branches=len(bl),branch_list=bl,active=active,active_html=active_html,rows=rows)

@app.route('/build/<int:bid>')
@require_login
def build_detail(bid):
    with db() as c: b=c.execute('SELECT * FROM builds WHERE id=?',(bid,)).fetchone()
    if not b: abort(404)
    log=''
    if b['log_path'] and os.path.isfile(b['log_path']):
        try:
            with open(b['log_path'],errors='replace') as f: log=f.read()[-60000:]
        except OSError: pass
    changed=json.loads(b['changed_files'] or '[]')
    body='''<section class="panel"><h2>Build #{{b['id']}} · {{b['branch']}}</h2><p class="status {{b['status']}}">{{b['status']}}</p><p>Commit: <code>{{b['commit_sha'] or 'pending'}}</code></p><p>Upstream branch: {{b['upstream_branch']}} · ISO: {{'enabled' if b['build_iso'] else 'disabled'}}</p><p>Started: {{b['started_at'] or '—'}} · Finished: {{b['finished_at'] or '—'}}</p>{% if b['status'] in ['queued','running','cancelling'] %}<form method="post" action="{{url_for('cancel_build',bid=b['id'])}}"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn danger">Cancel build</button></form>{% endif %}</section><section class="panel"><h3>Changed files ({{changed|length}})</h3><pre>{{changed|join('\\n') if changed else 'No change list available yet.'}}</pre></section><section class="panel"><h3>Build log</h3><pre id="log">{{log}}</pre></section><script>setTimeout(()=>location.reload(),5000)</script>'''
    return page(body,b=b,changed=changed,log=log)

@app.post('/build/start')
@require_login
def start_build():
    check_csrf(); branch=request.form.get('branch','').strip()
    allowed=[x for x in setting('branches',setting('target_branch','master')).split(',') if x]
    if branch not in allowed: abort(400,'Branch is not configured')
    with db() as c:
        active=c.execute("SELECT id FROM builds WHERE status IN ('queued','running','cancelling')").fetchone()
        if active: flash('A build is already queued or running.'); return redirect(url_for('index'))
        c.execute('INSERT INTO builds(branch,upstream_branch,build_iso,status,queued_at) VALUES(?,?,?,?,?)',(branch,setting('upstream_branch','master'),int(setting('build_iso','1')=='1'),'queued',now()))
    flash(f'Build queued for {branch}.'); return redirect(url_for('index'))

@app.post('/build/<int:bid>/cancel')
@require_login
def cancel_build(bid):
    check_csrf()
    with db() as c: c.execute("UPDATE builds SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END WHERE id=? AND status IN ('queued','running','cancelling')",(bid,))
    flash('Cancellation requested. The worker will clean up the active build.'); return redirect(url_for('build_detail',bid=bid))

@app.route('/settings',methods=['GET','POST'])
@require_login
def settings_page():
    if request.method=='POST':
        check_csrf(); branches=request.form.get('branches','').strip(); items=[x.strip() for x in branches.split(',') if x.strip()]
        if not items or any(not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}',x) or x.startswith('/') or x.endswith('/') or '..' in x.split('/') for x in items): flash('Enter valid Git branch names (comma-separated).')
        else:
            set_setting('branches',','.join(dict.fromkeys(items))); set_setting('upstream_branch',request.form.get('upstream_branch','master').strip() or 'master'); set_setting('poll_interval',max(60,int(request.form.get('poll_interval','604800')))); set_setting('retention',max(1,min(50,int(request.form.get('retention','4'))))); set_setting('history_retention',max(10,min(1000,int(request.form.get('history_retention','100'))))); set_setting('build_iso','1' if request.form.get('build_iso')=='on' else '0')
            flash('Settings saved.'); return redirect(url_for('settings_page'))
    body='''<section class="panel"><h2>Build settings</h2><form method="post"><input type="hidden" name="_csrf" value="{{csrf}}"><label>Branches (comma-separated)</label><input name="branches" value="{{branches}}" required><label>Upstream branch</label><input name="upstream_branch" value="{{upstream}}"><label>Automatic build/check interval (seconds)</label><input type="number" min="60" name="poll_interval" value="{{poll}}"><label>Previous system updates to retain</label><input type="number" min="1" max="50" name="retention" value="{{retention}}"><label>Build history entries to retain</label><input type="number" min="10" max="1000" name="history_retention" value="{{history}}"><label><input style="width:auto" type="checkbox" name="build_iso" {% if iso %}checked{% endif %}> Also build bootable ISO</label><p class="muted small">The ISO option controls the bootable ISO/ESP work; normal update artifacts and the signed update feed are still built.</p><button class="btn">Save settings</button></form></section><section class="panel"><h3>Security / networking</h3><p>Manager listens on port 5055 inside the container. Compose binds it to host 127.0.0.1 only. Publish it to your tailnet with Tailscale Serve; do not expose port 5055 publicly.</p><p>Authentication uses a password hash stored in the persistent state volume. Set the initial username/password and session secret in your environment file before first startup.</p></section>'''
    return page(body,branches=setting('branches',setting('target_branch','master')),upstream=setting('upstream_branch','master'),poll=setting('poll_interval','604800'),retention=setting('retention','4'),history=setting('history_retention','100'),iso=setting('build_iso','1')=='1')

@app.route('/health')
def health(): return jsonify(ok=True)

@app.before_request
def initialize():
    if request.endpoint in ('static',): return
    with db() as c:
        # Bootstrap password hash only on first run. Password is never stored in clear text.
        if not c.execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
            user=os.getenv('MANAGER_USERNAME','admin'); pw=os.getenv('MANAGER_PASSWORD','')
            if not pw: raise RuntimeError('Set MANAGER_PASSWORD before starting the manager for the first time')
            c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',('username',user)); c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',('password_hash',hash_pw(pw)))
        for k,v in [('target_branch',os.getenv('TARGET_BRANCH','master')),('upstream_branch',os.getenv('UPSTREAM_BRANCH','master')),('branches',os.getenv('TARGET_BRANCH','master')),('build_iso','1'),('retention',os.getenv('DEFAULT_RETENTION','4')),('history_retention',os.getenv('DEFAULT_HISTORY_RETENTION','100')),('poll_interval',os.getenv('POLL_INTERVAL','604800'))]: c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,str(v)))

if __name__=='__main__': app.run(host='0.0.0.0',port=5055)

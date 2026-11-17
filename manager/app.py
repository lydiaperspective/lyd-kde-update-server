import os, sqlite3, secrets, hashlib, hmac, time, json, re, html
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
CREATE TABLE IF NOT EXISTS builds (id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, upstream_branch TEXT NOT NULL, build_iso INTEGER NOT NULL, status TEXT NOT NULL, queued_at TEXT NOT NULL, started_at REAL, finished_at REAL, commit_sha TEXT, previous_sha TEXT, log_path TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0, changed_files TEXT DEFAULT '[]', job_id INTEGER, trigger TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, upstream_branch TEXT NOT NULL DEFAULT 'master', interval_seconds INTEGER NOT NULL DEFAULT 86400, enabled INTEGER NOT NULL DEFAULT 0, build_iso INTEGER NOT NULL DEFAULT 1, next_run_at REAL, last_run_at REAL, created_at TEXT NOT NULL);
'''
@contextmanager
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c=sqlite3.connect(DB, timeout=30); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    try:
        c.executescript(SCHEMA)
        columns={r['name'] for r in c.execute('PRAGMA table_info(builds)').fetchall()}
        if 'job_id' not in columns: c.execute('ALTER TABLE builds ADD COLUMN job_id INTEGER')
        if 'trigger' not in columns: c.execute("ALTER TABLE builds ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
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
</style></head><body><header><strong>KDE Linux · Update Manager</strong><nav><a href="{{url_for('index')}}">Dashboard</a><a href="{{url_for('jobs_page')}}">Build jobs</a><a href="{{url_for('settings_page')}}">Settings</a><a href="{{url_for('logout')}}">Sign out</a></nav></header><main class="wrap">{% with msgs=get_flashed_messages() %}{% for m in msgs %}<div class="panel">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main></body></html>'''

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
        jobs=c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        ac=c.execute("SELECT count(*) n FROM builds WHERE status IN ('queued','running','cancelling')").fetchone()['n']
    rows=''.join(f'''<tr><td>#{b['id']}</td><td>{b['branch']}</td><td class="status {b['status']}">{b['status']}</td><td>{(b['commit_sha'] or '')[:12]}</td><td>{b['queued_at'][:19].replace('T',' ')}</td><td><a href="{url_for('build_detail',bid=b['id'])}">Details</a></td></tr>''' for b in builds)
    active_html=f'''<p><b>#{active['id']} · {active['branch']}</b> — {active['status']}</p><div class="bar"><span></span></div><p class="muted small">The worker runs one build at a time. Live logs refresh automatically.</p>''' if active else '<p class="muted">No build currently running.</p>'
    job_rows=''.join(f'''<tr><td>{html.escape(j['name'])}</td><td><code>{j['branch']}</code></td><td>{j['interval_seconds']//3600}h</td><td>{'<span class="success">Enabled</span>' if j['enabled'] else '<span class="muted">Disabled</span>'}</td><td>{datetime.fromtimestamp(j['next_run_at'],timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if j['enabled'] and j['next_run_at'] else '—'}</td></tr>''' for j in jobs)
    body='''<div class="grid"><section class="panel"><div class="muted">Worker</div><div class="big">{{worker}}</div></section><section class="panel"><div class="muted">Queued / running</div><div class="big">{{active_count}}</div></section><section class="panel"><div class="muted">Build jobs</div><div class="big">{{job_count}}</div></section></div><section class="panel"><h2>Current build</h2>{{active_html|safe}}{% if active %}<form method="post" action="{{url_for('cancel_build',bid=active['id'])}}"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn danger">Stop current build</button></form>{% endif %}<p><a class="btn" href="{{url_for('jobs_page')}}">Manage build jobs</a></p></section><section class="panel"><h2>Configured build jobs</h2><p class="muted small">Scheduled builds are disabled by default. Enabling a job schedules its first run after one full interval.</p><table><thead><tr><th>Name</th><th>Branch</th><th>Interval</th><th>Status</th><th>Next run</th></tr></thead><tbody>{{job_rows|safe}}</tbody></table></section><section class="panel"><h2>Recent builds</h2><table><thead><tr><th>ID</th><th>Branch</th><th>Status</th><th>Commit</th><th>Queued</th><th></th></tr></thead><tbody>{{rows|safe}}</tbody></table></section>'''
    return page(body,worker=setting('worker_status','Waiting'),active_count=ac,job_count=len(jobs),active=active,active_html=active_html,rows=rows,job_rows=job_rows)

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
    with db() as c:
        job=c.execute('SELECT * FROM jobs WHERE branch=?',(branch,)).fetchone()
        if not job: abort(400,'Branch does not have a configured build job')
        active=c.execute("SELECT id FROM builds WHERE status IN ('queued','running','cancelling')").fetchone()
        if active: flash('A build is already queued or running.'); return redirect(url_for('index'))
        c.execute('INSERT INTO builds(branch,upstream_branch,build_iso,status,queued_at,job_id,trigger) VALUES(?,?,?,?,?,?,?)',(branch,job['upstream_branch'],job['build_iso'],'queued',now(),job['id'],'manual'))
    flash(f'Manual build queued for {branch}.'); return redirect(url_for('index'))

@app.post('/build/<int:bid>/cancel')
@require_login
def cancel_build(bid):
    check_csrf()
    with db() as c: c.execute("UPDATE builds SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END WHERE id=? AND status IN ('queued','running','cancelling')",(bid,))
    flash('Cancellation requested. The worker will clean up the active build.'); return redirect(url_for('build_detail',bid=bid))

@app.route('/jobs',methods=['GET','POST'])
@require_login
def jobs_page():
    if request.method=='POST':
        check_csrf()
        name=request.form.get('name','').strip() or request.form.get('branch','').strip()
        branch=request.form.get('branch','').strip()
        upstream=request.form.get('upstream_branch','master').strip() or 'master'
        try: interval=max(3600,min(31536000,int(request.form.get('interval_hours','24'))*3600))
        except ValueError: abort(400,'Invalid schedule interval')
        valid=lambda x: bool(re.fullmatch(r'[A-Za-z0-9._/-]{1,200}',x)) and not x.startswith('/') and not x.endswith('/') and '..' not in x.split('/')
        if not valid(branch): abort(400,'Enter a valid Git branch name')
        if not valid(upstream): abort(400,'Enter a valid upstream branch name')
        with db() as c:
            try:
                c.execute('INSERT INTO jobs(name,branch,upstream_branch,interval_seconds,enabled,build_iso,next_run_at,created_at) VALUES(?,?,?,?,0,?,NULL,?)',(name,branch,upstream,interval,int(request.form.get('build_iso')=='on'),now()))
            except sqlite3.IntegrityError:
                flash('A build job already exists for that branch.'); return redirect(url_for('jobs_page'))
        flash(f'Added disabled build job for {branch}. It will not run until enabled.')
        return redirect(url_for('jobs_page'))
    with db() as c: jobs=c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
    body='''<section class="panel"><h2>Add build job</h2><p class="muted">Each job tracks one fork branch and has its own schedule. New jobs are disabled until you enable them.</p><form method="post"><input type="hidden" name="_csrf" value="{{csrf}}"><label>Job name</label><input name="name" placeholder="KDE Linux Main"><label>Fork branch</label><input name="branch" placeholder="main" required><label>Upstream branch</label><input name="upstream_branch" value="{{default_upstream}}" required><label>Run interval (hours)</label><input type="number" name="interval_hours" min="1" max="8760" value="24" required><label><input style="width:auto" type="checkbox" name="build_iso"> Also build bootable ISO</label><button class="btn">Add disabled job</button></form></section><section class="panel"><h2>Build jobs</h2>{% if jobs %}<table><thead><tr><th>Name / branch</th><th>Schedule</th><th>Status</th><th>Next run</th><th>Actions</th></tr></thead><tbody>{% for j in jobs %}<tr><td><b>{{j['name']}}</b><br><code>{{j['branch']}}</code><br><span class="muted small">Upstream: {{j['upstream_branch']}}</span></td><td>Every {{j['interval_seconds']//3600}} hour(s)</td><td>{% if j['enabled'] %}<span class="success">Enabled</span>{% else %}<span class="muted">Disabled</span>{% endif %}</td><td>{% if j['enabled'] and j['next_run_at'] %}{{utc(j['next_run_at'])}}{% else %}—{% endif %}</td><td><div class="row"><form method="post" action="{{url_for('toggle_job',jid=j['id'])}}"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn">{{'Disable' if j['enabled'] else 'Enable'}}</button></form><form method="post" action="{{url_for('run_job',jid=j['id'])}}"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn">Run now</button></form><form method="post" action="{{url_for('delete_job',jid=j['id'])}}" onsubmit="return confirm('Delete this build job? Existing build history will be kept.')"><input type="hidden" name="_csrf" value="{{csrf}}"><button class="btn danger">Delete</button></form></div></td></tr>{% endfor %}</tbody></table>{% else %}<p class="muted">No build jobs configured yet.</p>{% endif %}</section><section class="panel"><h3>Scheduling behavior</h3><ul><li>Jobs are disabled by default.</li><li>Enabling a job schedules its first run one full interval from now.</li><li>Disabling a job cancels its waiting scheduled build, but does not interrupt a build already running.</li><li>Only one build runs at a time; due jobs wait for the worker queue.</li><li>Manual “Run now” is independent of the schedule.</li></ul></section>'''
    return page(body,jobs=jobs,default_upstream=setting('upstream_branch','master'),utc=lambda t:datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))

@app.post('/jobs/<int:jid>/toggle')
@require_login
def toggle_job(jid):
    check_csrf()
    with db() as c:
        job=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not job: abort(404)
        enabled=not bool(job['enabled'])
        next_run=time.time()+job['interval_seconds'] if enabled else None
        c.execute('UPDATE jobs SET enabled=?,next_run_at=? WHERE id=?',(int(enabled),next_run,jid))
        if not enabled: c.execute("UPDATE builds SET status='cancelled',cancel_requested=1,finished_at=? WHERE job_id=? AND trigger='scheduled' AND status='queued'",(now(),jid))
    flash(('Enabled' if enabled else 'Disabled')+f" scheduled builds for {job['branch']}.")
    return redirect(url_for('jobs_page'))

@app.post('/jobs/<int:jid>/run')
@require_login
def run_job(jid):
    check_csrf()
    with db() as c:
        job=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not job: abort(404)
        active=c.execute("SELECT id FROM builds WHERE status IN ('queued','running','cancelling')").fetchone()
        if active: flash('A build is already queued or running.'); return redirect(url_for('jobs_page'))
        c.execute('INSERT INTO builds(branch,upstream_branch,build_iso,status,queued_at,job_id,trigger) VALUES(?,?,?,?,?,?,?)',(job['branch'],job['upstream_branch'],job['build_iso'],'queued',now(),jid,'manual'))
    flash(f'Manual build queued for {job["branch"]}.'); return redirect(url_for('index'))

@app.post('/jobs/<int:jid>/delete')
@require_login
def delete_job(jid):
    check_csrf()
    with db() as c:
        job=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not job: abort(404)
        active=c.execute("SELECT id FROM builds WHERE job_id=? AND status IN ('running','cancelling')",(jid,)).fetchone()
        if active: flash('Cannot delete a job while its build is running. Disable it instead.'); return redirect(url_for('jobs_page'))
        c.execute("UPDATE builds SET status='cancelled',cancel_requested=1,finished_at=? WHERE job_id=? AND status='queued'",(now(),jid))
        c.execute('DELETE FROM jobs WHERE id=?',(jid,))
    flash(f'Deleted build job for {job["branch"]}. Existing build history was retained.')
    return redirect(url_for('jobs_page'))

@app.route('/settings',methods=['GET','POST'])
@require_login
def settings_page():
    if request.method=='POST':
        check_csrf()
        try:
            retention=max(1,min(50,int(request.form.get('retention','4'))))
            history=max(10,min(1000,int(request.form.get('history_retention','100'))))
        except ValueError: abort(400,'Invalid retention value')
        upstream=request.form.get('upstream_branch','master').strip() or 'master'
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}',upstream) or upstream.startswith('/') or upstream.endswith('/') or '..' in upstream.split('/'):
            abort(400,'Enter a valid upstream branch name')
        set_setting('upstream_branch',upstream); set_setting('retention',retention); set_setting('history_retention',history)
        flash('Settings saved. Existing jobs keep their own upstream branch and schedule.')
        return redirect(url_for('settings_page'))
    body='''<section class="panel"><h2>Global settings</h2><form method="post"><input type="hidden" name="_csrf" value="{{csrf}}"><label>Default upstream branch for new jobs</label><input name="upstream_branch" value="{{upstream}}"><label>Previous system updates to retain per branch</label><input type="number" min="1" max="50" name="retention" value="{{retention}}"><label>Build history entries to retain</label><input type="number" min="10" max="1000" name="history_retention" value="{{history}}"><button class="btn">Save settings</button></form></section><section class="panel"><h3>Security / networking</h3><p>Manager listens on port 5055 inside the container. Compose binds it to host 127.0.0.1 only. Publish it to your tailnet with Tailscale Serve; do not expose port 5055 publicly.</p><p>Authentication uses a password hash stored in the persistent state volume. Set the initial username/password and session secret in your environment file before first startup.</p></section>'''
    return page(body,upstream=setting('upstream_branch','master'),retention=setting('retention','4'),history=setting('history_retention','100'))

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
        for k,v in [('target_branch',os.getenv('TARGET_BRANCH','main')),('upstream_branch',os.getenv('UPSTREAM_BRANCH','master')),('branches',os.getenv('TARGET_BRANCH','main')),('build_iso','1'),('retention',os.getenv('DEFAULT_RETENTION','4')),('history_retention',os.getenv('DEFAULT_HISTORY_RETENTION','100')),('poll_interval',os.getenv('POLL_INTERVAL','604800'))]: c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,str(v)))
        if not c.execute("SELECT 1 FROM settings WHERE key='jobs_migrated'").fetchone():
            vals={r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}
            for branch in [x.strip() for x in vals.get('branches',vals.get('target_branch','main')).split(',') if x.strip()]:
                c.execute('INSERT OR IGNORE INTO jobs(name,branch,upstream_branch,interval_seconds,enabled,build_iso,next_run_at,created_at) VALUES(?,?,?,?,0,?,NULL,?)',(branch,branch,vals.get('upstream_branch','master'),max(3600,int(vals.get('poll_interval','604800'))),int(vals.get('build_iso','1')=='1'),now()))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('jobs_migrated','1')")

if __name__=='__main__': app.run(host='0.0.0.0',port=5055)

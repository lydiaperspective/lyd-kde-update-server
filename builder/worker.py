#!/usr/bin/env python3
"""Single-build-at-a-time supervisor. Commands are data in SQLite, never shell text."""
import os, sqlite3, time, subprocess, signal, pathlib, re, json, shutil, tempfile, hashlib, datetime
from contextlib import contextmanager
DB='/state/manager.db'; ROOT='/workspace'; SOURCES=f'{ROOT}/sources'; OUTPUT=f'{ROOT}/output'; STATE='/state'; GH_USER=os.getenv('GITHUB_USERNAME',''); GH_TOKEN=os.getenv('GITHUB_TOKEN',''); FORK=os.environ['FORK_REPO']; UPSTREAM=os.environ.get('UPSTREAM_REPO','https://invent.kde.org/kde-linux/kde-linux.git'); GPG='/gnupg';
os.makedirs(SOURCES,exist_ok=True); os.makedirs(OUTPUT,exist_ok=True); os.makedirs(STATE,exist_ok=True)
@contextmanager
def conn():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 try:
  c.executescript('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS builds (id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, upstream_branch TEXT NOT NULL, build_iso INTEGER NOT NULL, status TEXT NOT NULL, queued_at TEXT NOT NULL, started_at REAL, finished_at REAL, commit_sha TEXT, previous_sha TEXT, log_path TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0, changed_files TEXT DEFAULT '[]', job_id INTEGER, trigger TEXT NOT NULL DEFAULT 'manual');
  CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, upstream_branch TEXT NOT NULL DEFAULT 'master', interval_seconds INTEGER NOT NULL DEFAULT 86400, enabled INTEGER NOT NULL DEFAULT 0, build_iso INTEGER NOT NULL DEFAULT 1, next_run_at REAL, last_run_at REAL, created_at TEXT NOT NULL);''')
  cols={r['name'] for r in c.execute('PRAGMA table_info(builds)').fetchall()}
  if 'job_id' not in cols: c.execute('ALTER TABLE builds ADD COLUMN job_id INTEGER')
  if 'trigger' not in cols: c.execute("ALTER TABLE builds ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
  yield c
  c.commit()
 except Exception:
  c.rollback(); raise
 finally:
  c.close()
def set_status(i,status,**kw):
 with conn() as c:
  sets=['status=?']; vals=[status]
  for k,v in kw.items(): sets.append(f'{k}=?'); vals.append(v)
  vals.append(i); c.execute(f"UPDATE builds SET {','.join(sets)} WHERE id=?",vals)
def log(msg, f): f.write(f'[{datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}] {msg}\n'); f.flush()
def safe_branch(branch):
 if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}',branch) or branch.startswith('/') or '..' in branch.split('/') or branch.endswith('/') or branch.endswith('.'): raise ValueError('Unsafe branch name')
 subprocess.run(['git','check-ref-format','--branch',branch],check=True,stdout=subprocess.DEVNULL)
 return hashlib.sha256(branch.encode()).hexdigest()[:16]
def run(cmd,cwd,env,logfile,jobid, cancel_check=True):
 log('$ '+' '.join(cmd),logfile)
 p=subprocess.Popen(cmd,cwd=cwd,env=env,stdout=logfile,stderr=subprocess.STDOUT,start_new_session=True,text=True)
 while p.poll() is None:
  if cancel_check:
   with conn() as c: row=c.execute('SELECT cancel_requested FROM builds WHERE id=?',(jobid,)).fetchone()
   if row and row['cancel_requested']:
    log('Cancellation requested; terminating build process group.',logfile)
    try: os.killpg(p.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: p.wait(timeout=25)
    except subprocess.TimeoutExpired:
     try: os.killpg(p.pid,signal.SIGKILL)
     except ProcessLookupError: pass
     p.wait()
    raise InterruptedError('Build cancelled')
  time.sleep(2)
 if p.returncode: raise subprocess.CalledProcessError(p.returncode,cmd)
def git_env():
 e=os.environ.copy()
 if GH_USER and GH_TOKEN: e['GIT_CONFIG_COUNT']='1'; e['GIT_CONFIG_KEY_0']='url.https://'+GH_USER+':'+GH_TOKEN+'@github.com/.insteadOf'; e['GIT_CONFIG_VALUE_0']='https://github.com/'
 return e
def init_gpg():
 pathlib.Path(GPG).mkdir(exist_ok=True); os.chmod(GPG,0o700)
 env=os.environ.copy(); env['GNUPGHOME']=GPG
 keys=subprocess.run(['gpg','--batch','--with-colons','--list-secret-keys'],env=env,capture_output=True,text=True).stdout
 fprs=re.findall(r'^fpr:::::::::([A-F0-9]+):$',keys,re.M)
 wanted=os.getenv('GPG_SIGNING_KEY','').strip()
 fingerprint_file=pathlib.Path(GPG,'signing-key-fingerprint')
 if not wanted and fingerprint_file.exists(): wanted=fingerprint_file.read_text().strip()
 if wanted:
  check=subprocess.run(['gpg','--batch','--list-secret-keys',wanted],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  if check.returncode: raise RuntimeError('Configured GPG_SIGNING_KEY is not present in the persistent key volume')
  fprs=[wanted]
 if not fprs:
  name=os.getenv('GPG_KEY_NAME','KDE Linux Update Server'); email=os.getenv('GPG_KEY_EMAIL','')
  cfg='Key-Type: eddsa\nKey-Curve: ed25519\nKey-Usage: sign\nName-Real: '+name+'\n'+(('Name-Email: '+email+'\n') if email else '')+'Expire-Date: '+os.getenv('GPG_KEY_EXPIRE','0')+'\n%no-protection\n%commit\n'
  cp=subprocess.run(['gpg','--batch','--generate-key'],input=cfg,text=True,env=env,capture_output=True)
  if cp.returncode: raise RuntimeError(cp.stderr)
  keys=subprocess.run(['gpg','--batch','--with-colons','--list-secret-keys'],env=env,capture_output=True,text=True).stdout; fprs=re.findall(r'^fpr:::::::::([A-F0-9]+):$',keys,re.M)
 if not fprs: raise RuntimeError('No GPG signing key available')
 if len(fprs)>1: raise RuntimeError('Multiple GPG signing keys exist; set GPG_SIGNING_KEY in .env to choose one')
 fingerprint_file.write_text(fprs[0]+'\n')
 return fprs[0],env
def publish(repo,branch,outdir,retention,fpr,gpg_env,logfile):
 mk=pathlib.Path(repo)/'mkosi.output'; out=pathlib.Path(outdir); out.mkdir(parents=True,exist_ok=True)
 efis=[p for p in mk.glob('kde-linux_*.efi') if re.fullmatch(r'kde-linux_\d+\.efi',p.name)]
 erofs=list(mk.glob('kde-linux_*_root-x86-64.erofs')); caibx=list(mk.glob('kde-linux_*_root-x86-64.caibx'))
 if not efis or not erofs or not caibx: raise RuntimeError('Required EFI, root EROFS, or CAIBX artifact is missing')
 # Stage the public artifact directory and replace it only after all copies succeed.
 stage=pathlib.Path(tempfile.mkdtemp(prefix='.publish-',dir=str(out.parent)))
 try:
  if out.exists(): shutil.copytree(out,stage,dirs_exist_ok=True,ignore=shutil.ignore_patterns('sysupdate'))
  for p in mk.iterdir():
   if p.is_file(): shutil.copy2(p,stage/p.name)
  feedroot=out/'sysupdate'; feedroot.mkdir(exist_ok=True); feed=feedroot/'v2'; fs=pathlib.Path(tempfile.mkdtemp(prefix='.v2-stage-',dir=str(feedroot)))
  try:
   if feed.exists(): shutil.copytree(feed,fs,dirs_exist_ok=True)
   for p in efis+erofs+caibx: shutil.copy2(p,fs/p.name)
   versions=sorted([re.fullmatch(r'kde-linux_(\d+)\.efi',p.name).group(1) for p in fs.glob('kde-linux_*.efi') if re.fullmatch(r'kde-linux_(\d+)\.efi',p.name)],reverse=True)
   for v in versions[retention:]:
    for p in fs.glob(f'kde-linux_{v}*'): p.unlink(missing_ok=True)
   sums=''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in sorted(fs.glob('kde-linux_*')) if p.is_file())
   (fs/'SHA256SUMS').write_text(sums)
   subprocess.run(['gpg','--batch','--yes','--local-user',fpr,'--detach-sign','--output',str(fs/'SHA256SUMS.gpg'),str(fs/'SHA256SUMS')],env=gpg_env,check=True)
   subprocess.run(['gpg','--verify',str(fs/'SHA256SUMS.gpg'),str(fs/'SHA256SUMS')],env=gpg_env,check=True,stdout=logfile,stderr=logfile)
   backup=feedroot/'.v2-backup'
   if backup.exists(): shutil.rmtree(backup)
   if feed.exists(): feed.rename(backup)
   try: fs.rename(feed)
   except Exception:
    if backup.exists(): backup.rename(feed)
    raise
   if backup.exists(): shutil.rmtree(backup)
  finally:
   if fs.exists(): shutil.rmtree(fs,ignore_errors=True)
  # publish the normal output files individually; feed swap above is atomic.
  for p in stage.iterdir():
   if p.name=='sysupdate': continue
   dst=out/p.name
   if p.is_dir(): shutil.copytree(p,dst,dirs_exist_ok=True)
   else: shutil.copy2(p,dst)
 finally: shutil.rmtree(stage,ignore_errors=True)
def build(job):
 i=job['id']; branch=job['branch']; key=safe_branch(branch); repo=f'{SOURCES}/{key}'; logpath=f'{STATE}/build-{i}.log'; pathlib.Path(logpath).touch();
 with open(logpath,'a',buffering=1) as lf:
  set_status(i,'running',started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),log_path=logpath)
  log(f'Starting build #{i} for {branch}',lf)
  try:
   env=git_env(); os.makedirs(repo,exist_ok=True)
   if not pathlib.Path(repo,'.git').exists():
    legacy=pathlib.Path('/workspace/legacy-source')
    if branch==os.getenv('TARGET_BRANCH') and (legacy/'.git').exists():
     log('Reusing existing single-branch source volume for the configured primary branch.',lf)
     shutil.copytree(legacy,repo,dirs_exist_ok=True)
    else:
     run(['git','clone','--branch',branch,FORK,repo],ROOT,env,lf,i)
   run(['git','remote','set-url','origin',FORK],repo,env,lf,i)
   if subprocess.run(['git','remote','get-url','upstream'],cwd=repo,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode: run(['git','remote','add','upstream',UPSTREAM],repo,env,lf,i)
   run(['git','fetch','--prune','origin'],repo,env,lf,i); run(['git','fetch','--prune','upstream'],repo,env,lf,i)
   run(['git','switch',branch],repo,env,lf,i)
   prev=''; marker=pathlib.Path(repo,'.git/last-successful-build')
   if marker.exists(): prev=marker.read_text().strip()
   try:
    run(['git','merge','--no-edit',f'origin/{branch}'],repo,env,lf,i)
    run(['git','merge','--no-edit',f"upstream/{job['upstream_branch']}"],repo,env,lf,i)
   except (subprocess.CalledProcessError, InterruptedError):
    subprocess.run(['git','merge','--abort'],cwd=repo,env=env,stdout=lf,stderr=lf)
    raise
   sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(); set_status(i,'running',commit_sha=sha,previous_sha=prev)
   # No-op build if same successful source commit; keep an auditable history entry.
   if sha==prev:
    log('Source commit is unchanged from last successful build; nothing to build.',lf); set_status(i,'success',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat()); return
   # Patch a temporary copy: never dirty the checked-out fork, so future fetch/merge cycles stay clean.
   build_script=pathlib.Path(repo,'build.sh')
   patched_script=build_script
   if build_script.exists() and 'BUILD_ISO=' not in build_script.read_text(errors='ignore'):
    patch='/usr/local/share/kde-update-manager/build-iso-cancel.patch'
    patch_dir=pathlib.Path(tempfile.mkdtemp(prefix=f'build-{i}-',dir=STATE))
    patched_script=patch_dir/'build.sh'; shutil.copy2(build_script,patched_script)
    subprocess.run(['git','apply','-p1',patch],cwd=patch_dir,check=True)
   fpr,gpg_env=init_gpg()
   env.update(os.environ); env['GNUPGHOME']=GPG; env['BUILD_ISO']='1' if job['build_iso'] else '0'; env['CI']='1'
   run(['bash',str(patched_script)],repo,env,lf,i)
   with conn() as c: cancel_now=c.execute('SELECT cancel_requested FROM builds WHERE id=?',(i,)).fetchone()['cancel_requested']
   if cancel_now: raise InterruptedError('Build cancelled before publishing')
   with conn() as c:
    rr=c.execute("SELECT value FROM settings WHERE key='retention'").fetchone()
   retention=int(rr['value']) if rr else int(os.getenv('DEFAULT_RETENTION','4'))
   publish(repo,branch,f'{OUTPUT}/{branch}',retention,fpr,gpg_env,lf)
   pubdir=pathlib.Path('/key-export'); pubdir.mkdir(exist_ok=True)
   pubname=os.getenv('GPG_PUBLIC_KEY_FILE','kde-linux-signing-public.asc')
   if pathlib.Path(pubname).name != pubname: raise RuntimeError('GPG_PUBLIC_KEY_FILE must be a filename, not a path')
   with open(pubdir/pubname,'wb') as keyfile:
    subprocess.run(['gpg','--batch','--armor','--export',fpr],env=gpg_env,check=True,stdout=keyfile)
   marker.write_text(sha+'\n')
   if 'patch_dir' in locals(): shutil.rmtree(patch_dir,ignore_errors=True)
   changed=[]
   if prev:
    cp=subprocess.run(['git','diff','--name-status',prev,sha],cwd=repo,capture_output=True,text=True)
    changed=cp.stdout.splitlines() if cp.returncode==0 else ['Could not calculate diff from previous successful build.']
   with conn() as c: c.execute('UPDATE builds SET changed_files=? WHERE id=?',(json.dumps(changed),i))
   log('Build and signed feed published successfully.',lf); set_status(i,'success',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
  except InterruptedError:
   if 'patch_dir' in locals(): shutil.rmtree(patch_dir,ignore_errors=True)
   if pathlib.Path(repo).exists() and not pathlib.Path(repo,'.git').exists(): shutil.rmtree(repo,ignore_errors=True)
   log('Build cancelled. Published artifacts were not replaced.',lf); set_status(i,'cancelled',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
  except Exception as e:
   if 'patch_dir' in locals(): shutil.rmtree(patch_dir,ignore_errors=True)
   log(f'BUILD FAILED: {e}',lf); set_status(i,'failed',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),error=str(e))

def export_public_key():
 fpr, env=init_gpg()
 pubdir=pathlib.Path('/key-export'); pubdir.mkdir(exist_ok=True)
 pubname=os.getenv('GPG_PUBLIC_KEY_FILE','kde-linux-signing-public.asc')
 if pathlib.Path(pubname).name != pubname: raise RuntimeError('GPG_PUBLIC_KEY_FILE must be a filename, not a path')
 with open(pubdir/pubname,'wb') as keyfile:
  subprocess.run(['gpg','--batch','--armor','--export',fpr],env=env,check=True,stdout=keyfile)

def main():
 try: export_public_key()
 except Exception as e: print('GPG initialization/export warning:',e,flush=True)
 while True:
  try:
   # Migrate legacy comma-separated branch settings to disabled jobs. This is
   # deliberately opt-in: old installations must not suddenly auto-build.
   with conn() as c:
    c.execute("INSERT INTO settings(key,value) VALUES('worker_status','Online') ON CONFLICT(key) DO UPDATE SET value='Online'")
    migrated=c.execute("SELECT 1 FROM settings WHERE key='jobs_migrated'").fetchone()
    if not migrated:
     vals={r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}
     branches=[x.strip() for x in vals.get('branches',os.getenv('TARGET_BRANCH','main')).split(',') if x.strip()]
     for branch in branches:
      c.execute('INSERT OR IGNORE INTO jobs(name,branch,upstream_branch,interval_seconds,enabled,build_iso,next_run_at,created_at) VALUES(?,?,?,?,0,?,NULL,?)',(branch,branch,vals.get('upstream_branch',os.getenv('UPSTREAM_BRANCH','master')),max(3600,int(vals.get('poll_interval',os.getenv('POLL_INTERVAL','604800')))),int(vals.get('build_iso','1')=='1'),datetime.datetime.now(datetime.timezone.utc).isoformat()))
     c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('jobs_migrated','1')")
    active=c.execute("SELECT id FROM builds WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone()
    if not active:
     due=c.execute('SELECT * FROM jobs WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1',(time.time(),)).fetchone()
     if due:
      queued=c.execute("SELECT id FROM builds WHERE job_id=? AND trigger='scheduled' AND status IN ('queued','running','cancelling')",(due['id'],)).fetchone()
      if not queued:
       c.execute('INSERT INTO builds(branch,upstream_branch,build_iso,status,queued_at,job_id,trigger) VALUES(?,?,?,?,?,?,?)',(due['branch'],due['upstream_branch'],due['build_iso'],'queued',datetime.datetime.now(datetime.timezone.utc).isoformat(),due['id'],'scheduled'))
       next_run=time.time()+int(due['interval_seconds'])
       c.execute('UPDATE jobs SET last_run_at=?,next_run_at=? WHERE id=?',(time.time(),next_run,due['id']))
       print(f"Queued scheduled build for {due['branch']}; next run in {due['interval_seconds']} seconds",flush=True)
   with conn() as c:
    job=c.execute("SELECT * FROM builds WHERE status='queued' AND cancel_requested=0 ORDER BY id LIMIT 1").fetchone()
   if job: build(dict(job))
   else: time.sleep(3)
   with conn() as c:
    rr=c.execute("SELECT value FROM settings WHERE key='history_retention'").fetchone()
    keep=int(rr['value']) if rr else int(os.getenv('DEFAULT_HISTORY_RETENTION','100'))
    old=c.execute("SELECT id,log_path FROM builds WHERE status IN ('success','failed','cancelled') ORDER BY id DESC LIMIT -1 OFFSET ?",(keep,)).fetchall()
    for x in old:
     if x['log_path']:
      try: os.unlink(x['log_path'])
      except OSError: pass
     c.execute('DELETE FROM builds WHERE id=?',(x['id'],))
  except Exception as e:
   print('Worker loop error:',e,flush=True); time.sleep(5)
if __name__=='__main__': main()

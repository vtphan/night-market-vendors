# Development & Deployment

## Part 1 — Local Development

### First-time setup

```bash
cd /path/to/VendorRegistration
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements-lock.txt
```

> Use Python 3.11 to match the VPS. If `python3.11` isn't available, `python3` works but pinned versions in the lock file may differ.

### Running the dev server

Always activate the venv first, then run uvicorn:

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

If you see `ModuleNotFoundError`, you're likely running the system Python instead of the venv. Check with `which uvicorn` — it should point to `venv/bin/uvicorn`, not `/opt/anaconda3/...` or `/usr/bin/...`.

### Installing a new package locally

```bash
source venv/bin/activate       # if not already active
pip install <package-name>
```

Then add the package name to `requirements.txt` (unpinned) and commit. The lock file will be regenerated on the VPS during deployment (see Part 2).

If uvicorn is already running, stop and restart it after installing — `--reload` watches file changes, not environment changes.

### Resetting the database (dev only)

During development, schema changes don't need migrations. Delete the DB and restart:

```bash
rm data/app.db
uvicorn app.main:app --reload --port 8000
# create_all() recreates tables, seed.py repopulates booth types and event settings
```

To populate test data:

```bash
python scripts/seed_registrations.py medium --reset
```

### Stripe webhook forwarding (local dev)

```bash
stripe listen --forward-to 127.0.0.1:8000/api/webhooks/stripe
```

---

## Part 2 — VPS Deployment

**VPS info:** `vphan@82.25.86.134`, app at `/home/vphan/night-market-vendors`. See `docs/deployment.md` for full infrastructure details.

### Standard deploy (code changes only)

No dependencies changed, no schema changes:

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
sudo systemctl restart vendor-registration
```

No need to activate the venv — the systemd service uses `venv/bin/uvicorn` directly.

### Deploy with new packages

When `requirements.txt` has new packages that aren't in the lock file yet:

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate            # needed for pip
pip install -r requirements.txt
pip freeze > requirements-lock.txt
deactivate                          # optional, not needed for restart
sudo systemctl restart vendor-registration
```

Then pull the updated lock file back to your local machine:

```bash
# On your local machine:
cd /path/to/VendorRegistration
git pull                            # or: scp vphan@82.25.86.134:/home/vphan/night-market-vendors/requirements-lock.txt .
source venv/bin/activate
pip install -r requirements-lock.txt
```

The VPS is the source of truth for pinned versions. Commit the updated `requirements-lock.txt`.

### Deploy with lock file already up to date

If `requirements-lock.txt` was already updated and committed (e.g., after a previous deploy):

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
pip install -r requirements-lock.txt
deactivate
sudo systemctl restart vendor-registration
```

### Schema changes (new database columns)

**SQLAlchemy's `create_all()` does NOT add columns to existing tables.** It only creates tables that don't exist yet.

For production, new columns are handled via `ALTER TABLE` statements in the app's startup (`app/main.py` lifespan function). When you add a new column to a model:

1. Add the column to the model in `app/models.py`
2. Add an `ALTER TABLE` migration block in `app/main.py` lifespan (see existing examples for `concern_status`, `timezone`, `org_name`, etc.)
3. Deploy normally — the app auto-adds the column on next startup

**Never delete `data/app.db` on the VPS** — it contains real registration data. DB deletion is only for local development.

---

## Part 3 — Troubleshooting

### Check service status

```bash
sudo systemctl status vendor-registration
```

### View recent logs

```bash
sudo journalctl -u vendor-registration -n 50 --no-pager
```

### Follow logs in real time

```bash
sudo journalctl -u vendor-registration -f
```

### Force sync to remote (discard VPS-only changes)

Use this if the VPS working directory has uncommitted changes (e.g., a `requirements-lock.txt` that wasn't pulled back) and you want to match the remote exactly:

```bash
cd /home/vphan/night-market-vendors
git fetch origin
git reset --hard origin/master
```

> **Warning:** This discards all uncommitted changes on the VPS, including any `requirements-lock.txt` that was generated but not committed. Make sure you've pulled any needed files to your local machine first.

### Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: No module named 'xxx'` | venv not activated (for pip) or system Python running uvicorn | Activate venv: `source venv/bin/activate`, then `pip install -r requirements-lock.txt`. On VPS, restart the service. |
| `database is locked` | Concurrent writes to SQLite | Usually transient. If persistent, check for stuck processes: `fuser data/app.db` |
| Service won't start after deploy | Syntax error or missing env var | Check logs: `sudo journalctl -u vendor-registration -n 20 --no-pager` |

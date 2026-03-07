# Code Update on VPS

## Standard deploy (code changes only)

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
sudo systemctl restart vendor-registration
```

## Deploy with dependency changes

If `requirements.txt` or `requirements-lock.txt` changed:

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
pip install -r requirements-lock.txt
sudo systemctl restart vendor-registration
```

## When a new dependency was added locally

If you added a new package locally and pushed `requirements.txt` (but the lock file hasn't been regenerated yet):

```bash
ssh vphan@82.25.86.134
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
pip install -r requirements.txt
pip freeze > requirements-lock.txt
sudo systemctl restart vendor-registration
```

Then pull the updated `requirements-lock.txt` back to your local machine and run `pip install -r requirements-lock.txt` so both environments match. The VPS is always the source of truth for pinned versions.

## Force sync to remote (discard local VPS changes)

```bash
git fetch origin
git reset --hard origin/master
```

## Reset test data

If testing, delete data or re-seed. Make sure to backup the database first.

```bash
rm data/app.db
# or
python scripts/seed_registrations.py medium --reset
```

## Check service status

```bash
sudo systemctl status vendor-registration
```

## View service logs

If something goes wrong after a deploy, check the recent logs:

```bash
sudo journalctl -u vendor-registration -n 50 --no-pager
```

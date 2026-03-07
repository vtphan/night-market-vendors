1. Get the latest code from https://github.com/vtphan/night-market-vendors

git pull

or

git fetch origin
git reset --hard origin/master

2. source venv/bin/activate

3. pip install -r requirements.txt

4. If testing, delete data in data/  or python scripts/seed_registration medium --reset 
   Make sure to backup the database first.

5. Restart service
sudo systemctl restart vendor-registration

6. Check service status
sudo systemctl status vendor-registration

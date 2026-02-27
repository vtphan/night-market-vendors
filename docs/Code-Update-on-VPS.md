1. Get the latest code from https://github.com/vtphan/night-market-vendors

git pull

or

git fetch origin
git reset --hard origin/master

2. If testing, delete data in data/   . Make sure to backup the database first.

3. Restart service
sudo systemctl restart vendor-registration

4. Check service status
sudo systemctl status vendor-registration

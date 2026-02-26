# VPS Deployment Guide

## Infrastructure

| Component | Detail |
|-----------|--------|
| VPS | Hostinger, `82.25.86.134` |
| Domain | `vendors.nightmarketmemphis.com` (subdomain of site hosted on separate server `145.223.105.193`) |
| OS | Ubuntu (Python 3.11) |
| Reverse proxy | Nginx with Let's Encrypt SSL |
| Process manager | systemd |
| App directory | `/home/vphan/night-market-vendors` |
| Git repo | `https://github.com/vtphan/night-market-vendors` |

## Google Credentials

https://console.cloud.google.com/apis/credentials

Dev:
Authorized JavaScript origins: http://127.0.0.1:8000
Authorized redirect URIs: http://127.0.0.1:8000/auth/google/callback

VPS:

## DNS
Authorized JavaScript origins: https://vendors.nightmarketmemphis.com
Authorized redirect URIs: https://vendors.nightmarketmemphis.com/auth/google/callback

An **A record** for `vendors` pointing to `82.25.86.134` was added in the Hostinger DNS panel for `nightmarketmemphis.com`. The main domain lives on a different server — DNS is the only link between them.

## Application Setup

```bash
cd /home/vphan/night-market-vendors
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The SQLite database directory must exist before first run:

```bash
mkdir -p /home/vphan/night-market-vendors/data
```

The `.env` file is created from `.env.example` with production values. Key setting: `APP_URL=https://vendors.nightmarketmemphis.com`.

## systemd Service

File: `/etc/systemd/system/vendor-registration.service`

```ini
[Unit]
Description=Vendor Registration FastAPI App
After=network.target

[Service]
User=vphan
Group=vphan
WorkingDirectory=/home/vphan/night-market-vendors
EnvironmentFile=/home/vphan/night-market-vendors/.env
ExecStart=/home/vphan/night-market-vendors/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vendor-registration
sudo systemctl start vendor-registration
sudo systemctl status vendor-registration   # verify
sudo journalctl -u vendor-registration -f   # view logs
```

## Nginx Configuration

File: `/etc/nginx/sites-available/vendors.nightmarketmemphis.com`
Symlinked to: `/etc/nginx/sites-enabled/vendors.nightmarketmemphis.com`

```nginx
server {
    listen 443 ssl;
    server_name vendors.nightmarketmemphis.com;

    ssl_certificate /etc/letsencrypt/live/vendors.nightmarketmemphis.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vendors.nightmarketmemphis.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name vendors.nightmarketmemphis.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
        allow all;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
```

SSL certificate was obtained via:

```bash
sudo certbot --nginx -d vendors.nightmarketmemphis.com
```

Certbot auto-renews via its systemd timer. Verify with `sudo certbot renew --dry-run`.

## Deploying Updates

```bash
cd /home/vphan/night-market-vendors
git pull
source venv/bin/activate
pip install -r requirements.txt   # only if dependencies changed
sudo systemctl restart vendor-registration
```

## Previous Setup (replaced)

The VPS previously served `901asiannightmarket.com` (a Go app on port 8081). That Nginx config was removed from `sites-enabled` and the app was taken down. The DNS A records for `901asiannightmarket.com` still point to this VPS but are no longer served.

## Setup Date

February 24, 2026.

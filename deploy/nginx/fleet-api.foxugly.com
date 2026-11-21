# fleet-api.foxugly.com: nginx reverse proxy to gunicorn (127.0.0.1:8014).
#
# Installed by the deploy from root's mirror (fleet-deploy-privileged.sh) as
# /etc/nginx/sites-available/fleet-api.foxugly.com, symlinked into sites-enabled,
# checked by `nginx -t` before the reload (rollback to .deploybak otherwise).
# Edit this file in the repo, never on the box.
#
# Shape of Facilitation_server's facilitation-api.conf. TLS uses the shared
# wildcard lineage `foxugly.com`, never a per-subdomain certbot cert (§3.6).
# No /static/, no /media/, no /admin/: Fleet_server has neither admin nor static
# files, and its tree (/srv/fleet/Fleet_server, fleet:fleet 750) is not readable
# by nginx on purpose.

# --- http context (this file is included through sites-enabled) --------------
# Names prefixed `fleet_`: zones are global to nginx (FoxHouse style, so the
# vhost and its zone are installed and rolled back as one file).
# /api/v1/auth/ (login, TOTP, refresh, me, logout): a coarse per-IP ceiling in
# front of the DRF throttles (login 10/h, mfa 5/min, refresh 6/min), which stay
# the fine-grained guard. 30 r/min with a burst of 20 never bites a human.
limit_req_zone $binary_remote_addr zone=fleet_auth:10m rate=30r/m;

server {
    listen 80;
    server_name fleet-api.foxugly.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name fleet-api.foxugly.com;

    ssl_certificate     /etc/letsencrypt/live/foxugly.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/foxugly.com/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # The only source of HSTS for this vhost: Django sends none
    # (SECURE_HSTS_SECONDS = 0, config/settings.py), so the header goes out once.
    # nginx does not merge add_header across levels: a location that sets one
    # must repeat both (OPERATIONS.md §3.19). None does here.
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options "nosniff" always;

    # JSON API only: small bodies.
    client_max_body_size 1m;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_min_length 1024;
    gzip_types text/plain application/json application/vnd.oai.openapi;

    location ^~ /api/v1/auth/ {
        limit_req zone=fleet_auth burst=20 nodelay;
        limit_req_status 429;

        proxy_pass http://127.0.0.1:8014;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }

    # Read API (/api/v1/), /health/. Collections run in Celery, never in the
    # request, so the default proxy timeouts are enough.
    location / {
        proxy_pass http://127.0.0.1:8014;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }

    access_log /var/log/nginx/fleet-api-access.log;
    error_log  /var/log/nginx/fleet-api-error.log;
}

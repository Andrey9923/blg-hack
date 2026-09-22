# Deploy at hack.amdm.life

The application runs in the host network namespace and listens only on
`127.0.0.1:8001`. Only Nginx ports 80 and 443 need to be publicly reachable.

Create an operator account before enabling the service (run as `pichotiner`
from the checkout):

```bash
python3 -m web.app --database results/operator.sqlite3 --set-user alice
```

The unit enables authentication and persists runs to `results/operator.sqlite3`.
Do not remove `--auth` when placing the loopback server behind a public proxy.
Complete HTTPS setup before signing in; login credentials and bearer tokens
must travel over TLS. The provided Nginx configuration starts with HTTP only;
Certbot below adds TLS. Arrange request/login rate limits at the proxy for a
public deployment. SQLite serializes writes, and calculations remain synchronous.
The proxy allows 8 MiB request bodies and waits up to 300 seconds per response.

After the DNS A record for `hack.amdm.life` resolves to `95.165.159.101`, run:

```bash
sudo install -m 0644 deploy/hack-planner.service /etc/systemd/system/hack-planner.service
sudo install -m 0644 deploy/hack.amdm.life.nginx /etc/nginx/sites-available/hack.amdm.life
sudo ln -s /etc/nginx/sites-available/hack.amdm.life /etc/nginx/sites-enabled/hack.amdm.life
sudo systemctl daemon-reload
sudo systemctl enable --now hack-planner.service
sudo nginx -t
sudo systemctl reload nginx
curl http://hack.amdm.life/health
sudo certbot --nginx -d hack.amdm.life --redirect
curl https://hack.amdm.life/health
```

If the enabled-site symlink already exists, omit the `ln -s` command.

## Daily consistent backups

Adjust user and checkout paths in the supplied units before installing:

```bash
sudo install -m 0644 deploy/hack-planner-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/hack-planner-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hack-planner-backup.timer
sudo systemctl start hack-planner-backup.service
```

The task keeps 14 backup files and the latest 100 completed runs per owner,
preserving unfinished runs and ancestors of retained branches. Every cleanup is
preceded by a successful SQLite backup. This timer does not need to stop the app.

On Windows, use Task Scheduler (daily trigger): program = full path to `python.exe`,
Start in = checkout path, arguments:

```text
-m web.maintenance --database "C:\path\blg-hack\results\operator.sqlite3" --directory "C:\path\blg-hack\backups" --keep-backups 14 --keep-runs 100
```

Run the task once manually and verify its exit status and backup before relying
on the daily schedule. Timer installation on a remote host is an operator action.

## Request limits

The application CLI defaults to 120 API requests/minute per peer IP and returns
429 with Retry-After. Behind Nginx, configure per-client limits in the proxy and
set a suitable shared upstream `--rate-limit` (or 0 when the proxy enforces it).
For example, inside the Nginx `http` context:

```nginx
limit_req_zone $binary_remote_addr zone=planner_api:10m rate=2r/s;
```

Inside the site's `location /`:

```nginx
limit_req zone=planner_api burst=30 nodelay;
limit_req_status 429;
```

`--cors-origin https://frontend.example` enables one external frontend origin.
The UI shipped with the application is same-origin and needs no CORS setting.

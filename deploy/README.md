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

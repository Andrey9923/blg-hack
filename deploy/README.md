# Deploy at hack.amdm.life

The application runs in the host network namespace and listens only on
`127.0.0.1:8001`. Only Nginx ports 80 and 443 need to be publicly reachable.

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

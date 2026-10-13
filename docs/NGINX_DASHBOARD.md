# Dashboard behind nginx at /nautilus/

Dashboard container listens on 127.0.0.1:8097 (compose.dashboard.yml). The app rejects any Host
header except 127.0.0.1/localhost, so nginx MUST send `Host 127.0.0.1`.
Replace the old Nautilus `location /nautilus/` block (and `location = /nautilus`) with:

    location = /nautilus { return 301 /nautilus/; }
    location ^~ /nautilus/ {
        auth_basic "Optopsy Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd-nautilus;
        proxy_pass http://127.0.0.1:8097/;
        proxy_http_version 1.1;
        proxy_set_header Host 127.0.0.1;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Accept-Encoding "";
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        proxy_buffering off;
        sub_filter_once off;
        sub_filter 'fetch("/api/status"' 'fetch("/nautilus/api/status"';
        add_header Cache-Control "no-store" always;
    }

Then: `sudo nginx -t && sudo systemctl reload nginx` and open https://tn88seval.in/nautilus/

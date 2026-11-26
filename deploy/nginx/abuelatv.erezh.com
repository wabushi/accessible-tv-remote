# Drop-in replacement for /etc/nginx/sites-enabled/abuelatv.erezh.com on the
# server (wabu-proxy). The HTTPS server block is unchanged from the certbot
# generated one; only the port-80 block differs.
#
# Why: some networks let the ESP32 open outbound TCP on port 80 only (443
# times out before TLS starts). So port 80 now serves exactly two endpoints in
# plain HTTP, POST /api/poll and POST /api/ack, which is all the firmware uses.
# Every other path on port 80 still redirects to HTTPS, so the web UI,
# /api/commands, recording, playback and device deletion are never reachable
# over plain HTTP.
#
# Plain-HTTP device traffic is unencrypted and unauthenticated: it carries
# device IDs, names and key names, and anyone on the path could forge or replay
# polls/acks. Flash boards with http://abuelatv.erezh.com (no port).
#
# X-Forwarded-Proto is set explicitly on every proxied location so a client
# cannot spoof it: the backend only hands Wi-Fi credentials to a remote, or
# accepts them from the web UI, when the request arrived over HTTPS (or a
# trusted LAN). A board that can only reach port 80 therefore cannot have its
# Wi-Fi changed remotely.
#
# Apply with:  sudo nginx -t && sudo systemctl reload nginx

server {
    server_name abuelatv.erezh.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    listen [::]:443 ssl; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/abuelatv.erezh.com/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/abuelatv.erezh.com/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot

}

server {
    listen 80;
    listen [::]:80;
    server_name abuelatv.erezh.com;

    client_max_body_size 16k;

    # ESP32 device endpoints: exact paths, POST only.
    location = /api/poll {
        limit_except POST { deny all; }
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /api/ack {
        limit_except POST { deny all; }
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /api/wifi/result {
        limit_except POST { deny all; }
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Everything else (web UI, /api/commands, ...) must use HTTPS.
    location / {
        return 301 https://$host$request_uri;
    }
}

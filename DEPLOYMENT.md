# Write or Perish – Secure Deployment Instructions

This document details the steps and recommended tools to securely deploy the Write or Perish web application. This app uses a [Flask](https://flask.palletsprojects.com/) backend with a [React](https://reactjs.org/) frontend and a PostgreSQL database. It is designed to help users create and share personal journals that form a rich archive of human experience.

> **Important:** The code provided in this repository is set up for development. Before going live, please follow the recommendations below to harden your deployment.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation & Tooling](#installation--tooling)
- [Environment Variables & Secrets](#environment-variables--secrets)
- [Code Updates for Production](#code-updates-for-production)
- [Deployment Architecture](#deployment-architecture)
  - [Using Gunicorn as WSGI Server](#using-gunicorn-as-wsgi-server)
  - [Setting Up Nginx as a Reverse Proxy](#setting-up-nginx-as-a-reverse-proxy)
  - [Securing with TLS (Let’s Encrypt)](#securing-with-tls-lets-encrypt)
- [Additional Security Recommendations](#additional-security-recommendations)
- [Troubleshooting & Monitoring](#troubleshooting--monitoring)

---

## Prerequisites

- A Linux-based server (Ubuntu 20.04+ is recommended)
- Sudo (or root) privileges on the deployment server
- A registered domain name for your app (used for TLS and DNS configuration)
- PostgreSQL installed and configured with secure credentials

---

## Installation & Tooling

### Python & Dependencies

1. **Install Python Packages:**  
   Ensure that you have installed your required Python packages in a virtual environment. Update your `requirements.txt` with production tools:

   ```bash
   pip install -r requirements.txt
   ```

2. **Recommended New Tools:**

   - **Gunicorn:**  
     A production-grade WSGI server for running your Flask app.
     
     ```bash
     pip install gunicorn
     ```
     
     Add Gunicorn to your `requirements.txt`:

     ```
     gunicorn==20.1.0
     ```

   - **Security Scanners (Optional but Recommended):**  
     - [Bandit](https://pypi.org/project/bandit/): Code security analyzer.
       
       ```bash
       pip install bandit
       ```
     
     - [Safety](https://pypi.org/project/safety/): Scans for known vulnerabilities in your dependencies.
       
       ```bash
       pip install safety
       ```

### Node & Frontend Dependencies

1. Ensure you have Node.js and npm (or yarn) installed.
2. Install the React dependencies:

   ```bash
   npm install
   ```

3. Build the production bundle for the frontend:

   ```bash
   npm run build
   ```

---

## Environment Variables & Secrets

Create a `.env` file in your project root (or use your secrets management system) with values similar to the following:

```dotenv
FLASK_ENV=production
SECRET_KEY=your-very-strong-secret-key
DATABASE_URL=postgresql://username:password@localhost/writeorperish
TWITTER_API_KEY=your-twitter-api-key
TWITTER_API_SECRET=your-twitter-api-secret
OPENAI_API_KEY=your-openai-api-key
FRONTEND_URL=https://yourdomain.com
LLM_NAME=OpenAI  # or your configured model name
```

> **Note:**  
> • Never commit secrets or production data to version control.  
> • Use your platform’s secrets manager or environment variable configuration to inject these values.

---

## Code Updates for Production

### Disable Debug Mode

In `backend/app.py`, replace:

```python
if __name__ == '__main__':
    app.run(debug=True, port=5010)
```

with either one of the following:

- **For Development Convenience:**  
  Remove `debug=True` once you switch to production:

  ```python
  if __name__ == '__main__':
      app.run()
  ```

- **Or — Recommended:**  
  Do not use this file at all in production; instead, run your Flask app via Gunicorn.

### Secure Flask Configuration

In `backend/config.py`, add or update the following settings to enforce secure cookies:

```python
class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "this-should-be-changed")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "postgresql://localhost/writeorperish")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # OAuth and API Keys
    TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY")
    TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
    
    # Production Security Settings
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = True
```

---

## Deployment Architecture

### Using Gunicorn as WSGI Server

Run the Flask app using Gunicorn rather than the Flask development server.

Example command:

```bash
gunicorn -w 4 -b 127.0.0.1:8000 backend.app:app
```

- `-w 4` specifies 4 worker processes.
- Bind to `127.0.0.1:8000` — the reverse proxy (Nginx) will communicate with Gunicorn on this port.

Make sure to configure a process manager using **systemd** or **Supervisor** to restart Gunicorn automatically if the process dies.

### Setting Up Nginx as a Reverse Proxy

1. **Install Nginx:**

   ```bash
   sudo apt update
   sudo apt install nginx
   ```

2. **Configure Nginx:**  
   Create a configuration file at `/etc/nginx/sites-available/writeorperish` with contents similar to:

   ```nginx
server {
   listen 80;
   server_name 35.224.144.192;
   root /home/hrosspet/write-or-perish/frontend/build;
   index index.html index.htm;

   location / {
       try_files $uri $uri/ /index.html;
   }

   location /api/ {
       proxy_pass http://127.0.0.1:8000/;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }


}
   ```

3. **Enable the Site & Reload Nginx:**

   ```bash
   sudo ln -s /etc/nginx/sites-available/writeorperish /etc/nginx/sites-enabled/
   sudo nginx -t             # Test configuration
   sudo systemctl reload nginx
   ```

### Securing with TLS (Let’s Encrypt)

1. **Install Certbot for Nginx:**

   ```bash
   sudo apt install certbot python3-certbot-nginx
   ```

2. **Run Certbot to Obtain and Configure Certificates:**

   ```bash
   sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
   ```

3. Certbot will automatically update the Nginx configuration to enforce HTTPS and install a certificate. Be sure the renewal cron jobs (or systemd timers) are set up to renew the certificates automatically.

---

## Additional Security Recommendations

- **Enforce HTTPS Everywhere:**  
  Configure your reverse proxy (Nginx) and Flask to accept only secure connections.

- **CORS Configuration:**  
  Double-check that the CORS settings allow only your trusted frontend requests.

- **Rate Limiting & Logging:**  
  Consider middleware libraries such as Flask-Limiter for rate limiting. Use a logging service (Sentry, Graylog, etc.) to track errors without exposing sensitive information to users.

- **Regularly Update & Scan Dependencies:**  
  Use tools such as:
  - [Bandit](https://bandit.readthedocs.io/en/latest/)  
  - [Safety](https://pyup.io/safety/)
  
- **Network Protection:**  
  Use firewalls (like UFW on Ubuntu) to restrict access to only necessary ports (80/443, etc.).

---

## Troubleshooting & Monitoring

- **Nginx Logs:**  
  Located in `/var/log/nginx/` (access.log and error.log).

- **Gunicorn Logs:**  
  Configure logging via your process manager (supervisor, systemd) for inspection.

- **Flask Logs:**  
  Make sure to use a logging framework that does not leak sensitive data in production.

For more detailed guidance, refer to:
- Gunicorn documentation: https://docs.gunicorn.org
- Nginx documentation: https://nginx.org/en/docs/
- Let’s Encrypt Certbot: https://certbot.eff.org/

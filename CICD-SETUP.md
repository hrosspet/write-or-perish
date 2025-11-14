# CI/CD Setup Guide for Write or Perish

This guide walks you through setting up continuous deployment for the Write or Perish application using GitHub Actions.

## Overview

The CI/CD pipeline automates:
- Frontend building (React)
- Backend deployment (Flask + Gunicorn)
- Database migrations
- Service restarts

## Architecture

- **GitHub Actions**: Builds frontend and triggers deployment
- **GCP VM**: Hosts the application (Nginx + Gunicorn + PostgreSQL)
- **SSH**: Secure connection from GitHub Actions to VM
- **Systemd**: Manages Gunicorn as a system service (optional)

---

## Initial Setup (One-Time)

### 1. Generate SSH Key for Deployment

On your local machine or VM:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy_key
```

This creates:
- `~/.ssh/github_deploy_key` (private key - for GitHub Secrets)
- `~/.ssh/github_deploy_key.pub` (public key - for VM)

### 2. Add Public Key to VM

SSH into your VM and add the public key to authorized keys:

```bash
ssh your-vm-user@your-vm-ip

# On the VM:
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cat >> ~/.ssh/authorized_keys << 'EOF'
# Paste the content of github_deploy_key.pub here
EOF
chmod 600 ~/.ssh/authorized_keys
```

### 3. Configure GitHub Secrets

Go to your GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**

Add these secrets:

| Secret Name | Description | Example Value |
|-------------|-------------|---------------|
| `SSH_PRIVATE_KEY` | Private SSH key content | Content of `github_deploy_key` file |
| `VM_HOST` | VM IP address or hostname | `35.123.45.67` or `writeorperish.org` |
| `VM_USER` | SSH username on VM | `hrosspet` |

To get the private key content:
```bash
cat ~/.ssh/github_deploy_key
```

Copy the entire output including `-----BEGIN OPENSSH PRIVATE KEY-----` and `-----END OPENSSH PRIVATE KEY-----`.

### 4. Set Up VM Directory Structure

SSH into your VM and create necessary directories:

```bash
cd /home/hrosspet/write-or-perish
mkdir -p logs
mkdir -p frontend/build
```

### 5. Make Deployment Script Executable

On your VM:

```bash
chmod +x /home/hrosspet/write-or-perish/deploy.sh
```

### 6. Configure Nginx for Passwordless Reload (Optional)

To allow the deployment script to reload Nginx without a password prompt:

```bash
# Add to sudoers
sudo visudo
```

Add this line (replace `hrosspet` with your username):
```
hrosspet ALL=(ALL) NOPASSWD: /usr/bin/systemctl reload nginx
```

### 7. Install Systemd Service (Optional but Recommended)

This allows better management of the Gunicorn process:

```bash
# Copy service file to systemd directory
sudo cp /home/hrosspet/write-or-perish/write-or-perish.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable write-or-perish

# Start the service
sudo systemctl start write-or-perish

# Check status
sudo systemctl status write-or-perish
```

If using systemd, update `deploy.sh` to use systemctl instead of manual process management:

Replace the Gunicorn start/stop section with:
```bash
sudo systemctl restart write-or-perish
```

---

## How to Deploy

### Automatic Deployment

Push to the `main` branch:
```bash
git push origin main
```

The deployment workflow will automatically:
1. Build the frontend
2. Upload to VM
3. Pull latest backend code
4. Replace frontend build
5. Restart Gunicorn

### Manual Deployment

Go to GitHub: **Actions → Deploy to Production → Run workflow**

Click "Run workflow" on the main branch.

---

## Workflows

### 1. CI Workflow (`.github/workflows/ci.yml`)

**Triggers**: All branches except `main`, and PRs to `main`

**Jobs**:
- **test-backend**: Lints and tests Python code
- **test-frontend**: Tests and builds React app
- **security-check**: Scans for security vulnerabilities

### 2. Deploy Workflow (`.github/workflows/deploy.yml`)

**Triggers**: Push to `main` or manual trigger

**Steps**:
1. Checkout code
2. Setup Node.js and build frontend
3. Create deployment package
4. Setup SSH connection
5. Upload frontend build to VM
6. Execute deployment script on VM
7. Cleanup

---

## Deployment Script (`deploy.sh`)

Located at `/home/hrosspet/write-or-perish/deploy.sh`

**Actions**:
1. Activates Python virtual environment (creates if missing)
2. Installs/updates Python dependencies
3. Runs database migrations
4. Stops existing Gunicorn process
5. Starts new Gunicorn with 15-minute timeout for LLM calls
6. Reloads Nginx
7. Logs deployment details to `deployment.log`

**Logs**: Check `/home/hrosspet/write-or-perish/deployment.log` for deployment history

---

## Monitoring and Troubleshooting

### Check Deployment Status

On GitHub:
- Go to **Actions** tab
- Click on the latest workflow run
- Review logs for each step

### Check Application Status on VM

```bash
# Check if Gunicorn is running
ps aux | grep gunicorn

# Check Gunicorn logs
tail -f /home/hrosspet/write-or-perish/logs/gunicorn-error.log
tail -f /home/hrosspet/write-or-perish/logs/gunicorn-access.log

# Check deployment log
tail -f /home/hrosspet/write-or-perish/deployment.log

# If using systemd:
sudo systemctl status write-or-perish
sudo journalctl -u write-or-perish -f
```

### Check Nginx Status

```bash
sudo systemctl status nginx
sudo nginx -t  # Test configuration
tail -f /var/log/nginx/error.log
```

### Common Issues

**Issue: SSH Connection Failed**
- Verify SSH key is added to GitHub Secrets correctly
- Check VM firewall allows SSH (port 22)
- Verify SSH key is in `~/.ssh/authorized_keys` on VM

**Issue: Permission Denied on deploy.sh**
```bash
chmod +x /home/hrosspet/write-or-perish/deploy.sh
```

**Issue: Gunicorn Won't Start**
- Check Python dependencies installed: `pip list`
- Check virtual environment exists: `ls /home/hrosspet/write-or-perish/venv`
- Review error logs: `cat /home/hrosspet/write-or-perish/logs/gunicorn-error.log`

**Issue: Frontend Not Updating**
- Verify build files extracted: `ls /home/hrosspet/write-or-perish/frontend/build`
- Check Nginx configuration points to correct path
- Clear browser cache

**Issue: Database Migration Failed**
- Check database credentials in `.env.production`
- Verify PostgreSQL is running: `sudo systemctl status postgresql`
- Check migrations directory exists

---

## Rolling Back

If a deployment causes issues:

1. **Revert Git Commit**:
```bash
git revert HEAD
git push origin main
```
This will trigger a new deployment with the previous code.

2. **Manual Rollback on VM**:
```bash
cd /home/hrosspet/write-or-perish
git log  # Find previous commit hash
git checkout <previous-commit-hash>
bash deploy.sh
```

---

## Security Best Practices

1. **Rotate SSH Keys Regularly**: Generate new deployment keys every 6-12 months
2. **Review Logs**: Check deployment and access logs regularly for suspicious activity
3. **Update Dependencies**: Run `pip list --outdated` and update packages regularly
4. **Monitor Secrets**: Never commit `.env` or `.env.production` files
5. **Limit SSH Access**: Use firewall rules to restrict SSH to specific IPs if possible

---

## Future Enhancements

Consider adding:
- **Slack/Email Notifications**: Notify on deployment success/failure
- **Health Checks**: Verify application is responding after deployment
- **Staging Environment**: Test deployments before production
- **Automated Backups**: Backup database before deployment
- **Blue-Green Deployment**: Zero-downtime deployments
- **Docker**: Containerize the application for consistency

---

## Need Help?

- Check GitHub Actions logs for detailed error messages
- Review VM logs: `deployment.log`, `gunicorn-error.log`
- Verify all secrets are configured correctly
- Ensure VM has all required dependencies installed

For issues, refer to:
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Gunicorn Documentation](https://docs.gunicorn.org)
- [Nginx Documentation](https://nginx.org/en/docs/)

# 🐳 Docker Deployment Guide

Run the trading bot + live web dashboard in a single Docker container.
Works on any machine: your laptop, a VPS, Oracle Cloud free tier, etc.

---

## Prerequisites

Install these once on your machine:

- **Docker Desktop** (Mac/Windows): https://www.docker.com/products/docker-desktop
- **Docker Engine** (Linux/Ubuntu):
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```

Verify Docker is working:
```bash
docker --version
# Docker version 25.x.x
```

---

## Step 1 — Get the code

```bash
git clone https://github.com/myaichallange-lgtm/crypto-scalping-bot
cd crypto-scalping-bot
```

---

## Step 2 — Configure your API keys

Copy the example config and fill in your keys:

```bash
cp .env.example .env
```

Open `.env` in any text editor and set your values:

```env
BINANCE_API_KEY=your_testnet_api_key_here
BINANCE_API_SECRET=your_testnet_api_secret_here
BINANCE_TESTNET=true

INITIAL_BALANCE_USDT=100
MAX_RISK_PER_TRADE=0.02
DAILY_DRAWDOWN_LIMIT=0.15
LEVERAGE=10
TRADING_PAIRS=BTC/USDT,ETH/USDT,SOL/USDT
LOG_LEVEL=INFO
```

> 🔑 Get free testnet keys at: https://testnet.binancefuture.com
> Register → API Management → Generate HMAC_SHA256 Key

---

## Step 3 — Build and start

```bash
docker compose up --build
```

That's it. Docker will:
1. Build the image (takes ~60s the first time)
2. Start the trading bot
3. Start the web dashboard on port 8765

**Open your browser:** http://localhost:8765

---

## Useful commands

```bash
# Start in background (detached mode)
docker compose up -d --build

# Check if container is running
docker compose ps

# View live logs
docker compose logs -f

# View only bot errors
docker compose logs -f | grep ERROR

# Stop the bot
docker compose down

# Restart the bot
docker compose restart

# Rebuild after code changes
docker compose up --build -d
```

---

## Accessing the dashboard

| Where you're running | Dashboard URL |
|---|---|
| Your own laptop/PC | http://localhost:8765 |
| Remote VPS / server | http://YOUR_SERVER_IP:8765 |
| Oracle Cloud | http://YOUR_ORACLE_IP:8765 (open port 8765 in Security List) |

---

## Persistent data

Your trade journals and logs are saved **outside** the container (they won't be lost if you restart or rebuild):

```
crypto-scalping-bot/
├── data/
│   └── journal_YYYY-MM-DD.json   ← trade log, persists between restarts
└── logs/
    └── bot_YYYY-MM-DD.log        ← full bot log, persists between restarts
```

---

## Running on Oracle Cloud Free Tier

Oracle gives you a free ARM Ubuntu VM with 4 cores and 24GB RAM — plenty for the bot.

1. Create a free account at https://cloud.oracle.com/free
2. Launch an **Ampere ARM Ubuntu 22.04** VM (Always Free tier)
3. SSH into your VM:
   ```bash
   ssh ubuntu@YOUR_ORACLE_IP
   ```
4. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   newgrp docker
   ```
5. Open port 8765 in Oracle's Security List (VCN → Security List → Add Ingress Rule → Port 8765)
6. Clone and run:
   ```bash
   git clone https://github.com/myaichallange-lgtm/crypto-scalping-bot
   cd crypto-scalping-bot
   cp .env.example .env
   nano .env          # paste your keys
   docker compose up -d --build
   ```
7. Open browser: http://YOUR_ORACLE_IP:8765

---

## Going live (real money)

When ready to trade with real funds:

1. Create a real Binance account at https://binance.com
2. Complete KYC verification
3. Generate Futures API keys (enable Futures, disable Withdrawals)
4. Update `.env`:
   ```env
   BINANCE_API_KEY=your_live_key
   BINANCE_API_SECRET=your_live_secret
   BINANCE_TESTNET=false
   ```
5. Rebuild and restart:
   ```bash
   docker compose up --build -d
   ```

⚠️ **Start small on live. Always test on testnet first.**

---

## Troubleshooting

**Port 8765 already in use:**
```bash
docker compose down
# or change the port in docker-compose.yml: "8766:8765"
```

**Container keeps restarting:**
```bash
docker compose logs --tail=50
# Check for API key errors or missing .env file
```

**Can't reach dashboard on remote server:**
- Check your firewall allows port 8765
- On Oracle Cloud: add Ingress Rule for port 8765 in Security List
- On DigitalOcean/Hetzner: `ufw allow 8765`

**Reset trade journal:**
```bash
echo '{"date":"'$(date +%Y-%m-%d)'","daily_pnl":0,"trade_count":0,"wins":0,"losses":0,"win_rate":0}' > data/journal_$(date +%Y-%m-%d).json
docker compose restart
```

===== START README.MD CONTENT =====
# guns.lol Username Availability Checker

**Created by HighSkize Productions**  
*Fast, respectful, and read‑only tool to find short usernames available on guns.lol.*

---

## ⚠️ Disclaimer & Educational Purpose

This script is provided **for educational purposes only**.  
It demonstrates asynchronous network programming, API interaction, and polite rate‑limiting practices.  

It does **not**:

- Create accounts or claim usernames automatically
- Bypass any security or authentication
- Store, transmit, or misuse personal data
- Perform any write action on guns.lol

The tool simply asks the same public availability endpoint that the guns.lol website itself uses when you type a username. Use it responsibly and in accordance with guns.lol’s Terms of Service.

---

## ✨ Features

- Scans 2‑, 3‑, and 4‑character usernames (letters only or alphanumeric)
- Asynchronous checks with `asyncio` + `aiohttp` for high speed
- Configurable concurrency and request delay (polite by design)
- Exponential backoff with jitter for transient errors
- Live one‑line progress dashboard
- Saves found usernames **immediately** to a text file
- Optional Discord webhook notifications (instant alerts)
- Continuous loop mode to catch names that become free later
- Pre‑flight connectivity check with clear error messages
- Cross‑platform – works on Windows, macOS, Linux, Chromebook

---

## 📁 What’s inside

| File            | Purpose                                      |
|-----------------|----------------------------------------------|
| `main.py`       | Entry point, CLI, orchestration              |
| `config.py`     | Settings, endpoint, webhook                  |
| `checker.py`    | Network layer, retry, response parsing       |
| `generator.py`  | Generates candidate usernames                |
| `notifier.py`   | Discord webhook alerts                       |
| `preflight.py`  | Connectivity and DNS checks                  |
| `stats.py`      | Live statistics dashboard                    |
| `requirements.txt` | Dependencies                              |
| `LICENSE`       | Custom license (see below)                   |

---

## 🚀 Quick Start

**For full step‑by‑step instructions, see [SETUP.md](SETUP.md).**

1. **Install Python 3.10+** if you don’t have it.
2. **Install dependencies**  
   ```bash
   pip install -r requirements.txt
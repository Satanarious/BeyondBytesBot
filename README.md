# BBbot — BeyondBytes Bot

A Telegram bot for a **single group with topics** (Beyond Bytes). It has two features:

1. **Per-topic RSS feeds** — each topic can be subscribed to RSS sources. The bot
   checks every feed quietly, collects new articles per topic, and **once a day**
   posts a single digest per topic: Google Gemini summarizes the **top 5 stories**,
   each summary carrying the article link to read more. The collected links are
   stored server-side, processed by Gemini, then **deleted after the digest is
   posted**. `/updatetime` controls feed-check frequency, `/timetopost` the daily
   posting time.
2. **Giveaways / gifting** — a wizard that builds a giveaway announcement with a
   name, description, image and duration, and a **🎟 Join button** — only members
   who tap it are enrolled. Giveaways are bound to the topic they were
   created in: the announcement, the Join button, enrollment and the draw all
   happen in that topic only, and different topics can run their own giveaways.
   Once the giveaway goes live, the entire creation wizard chat is deleted, and
   at the end the winner is drawn randomly, tagged, and congratulated.

| Command | What it does |
|---|---|
| `/subscribe <rss_url>` | Subscribe the **current topic** to an RSS feed |
| `/unsubscribe <id\|url>` | Remove a subscription (IDs from `/list`) |
| `/list` | List RSS sources subscribed in the current topic |
| `/listall` | List all RSS sources in every topic of the group |
| `/updatetime` | Show/change how often RSS feeds are checked, e.g. `/updatetime 90` (seconds) or `/updatetime 2m`, `/updatetime 1h` — saved and applied immediately |
| `/timetopost` | Show/change the daily digest posting time: `/timetopost 08:30` (24 h clock, UTC) — saved and applied immediately |
| `/createnewgiveaway` | Start the giveaway wizard in the current topic |
| `/listgiveaways` | Show running giveaways (with IDs, time left, participants) |
| `/cancelgiveaway <id>` | Cancel a running giveaway |
| `/cancel` | Cancel the raffle wizard mid-way |

Everything is stored in SQLite (`data/bbbot.db`) — subscriptions and raffles
survive restarts. RSS is checked every 5 minutes (configurable); already published
articles are never re-sent and the backlog is skipped when you first subscribe.

**Note:** enrollment is join-button only — a member is in the draw if and only
if they tap 🎟 on the announcement. (Telegram doesn't let bots read the group
member list, so there's no true "silent auto-enroll-everyone" mode.) If a
giveaway ends with zero participants, it announces that no winner was drawn.

---

## 1. Project layout

```
BBbot/
├─ bot.py            entry point: wiring, commands, polling
├─ rss.py            /subscribe, /unsubscribe, /list, /listall, /updatetime + background poller (collector)
├─ digest.py         daily Gemini summary job + /timetopost
├─ giveaway.py       giveaway wizard, Join button, draw job
├─ db.py             SQLite storage (subscriptions, seen links, raffles, participants)
├─ utils.py          small helpers (topic IDs, time formatting)
├─ bbbot.service     systemd unit template for deployment
├─ requirements.txt
└─ .env.example      configuration template
```

## 2. Step 1 — Create the bot with @BotFather

1. Open Telegram, search for **@BotFather**, press `/start`.
2. Send `/newbot`. BotFather asks:
   - **Name** — shows in the profile: type `BeyondBytes Bot` (this is display only).
   - **Username** — must end in `bot` and be globally unique, e.g.
     `beyondbytes_bb_bot` (try variations until accepted).
3. Copy the **HTTP API token** BotFather gives you (`123456789:AAE...`).
   This is the bot's password — never share or commit it.
4. Make it group-friendly:
   - `/setprivacy` → select your bot → **Disable**
     (lets it read normal group messages, needed for auto-enrollment and topic
     name detection; making it an admin also achieves this).
   - `/setcommands` → select bot → paste:
     ```
     subscribe - Subscribe this topic to an RSS feed
     unsubscribe - Unsubscribe /unsubscribe <id|url>
     list - List RSS sources in this topic
     listall - List RSS sources in all topics
     updatetime - Change RSS check interval: /updatetime 5m
     timetopost - Set daily digest time: /timetopost 08:30 (UTC)
     createnewgiveaway - Start a giveaway in this topic
     listgiveaways - Show running giveaways
     cancelgiveaway - Cancel a giveaway: /cancelgiveaway <id>
     cancel - Cancel the current operation
     ```
5. Optional: `/setuserpic` to add a profile image.

### Set up the group

1. The group must be a **supergroup with Topics enabled** → group settings →
   **Topics → enable**.
2. Add the bot to Beyond Bytes (Add member → your bot).
3. **Promote it to admin** with at least "Post Messages" (recommended so it can
   always post into every topic), and optionally "Invite Users".

## 3. Step 2 — Run it locally (quick test)

```bash
cd BBbot
python -m venv .venv
# Windows:        .venv\Scripts\activate        Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # (Linux: cp .env.example .env)
#   edit .env and paste the BotFather token
python bot.py
```

Then in your group, run `/subscribe <url>` in a topic and `/createnewraffle`.
Stop with Ctrl+C.

## 4. Step 3 — Deploy on Oracle Cloud (Always Free)

### Choices made here (be deliberate as you go through)

- **Plan:** Oracle's **Always Free** tier includes an **Ampere A1 Flex (ARM)** VM —
  up to **4 OCPUs + 24 GB RAM** total (you can split this across up to 2 instances,
  e.g. 2 OCPU/12 GB + 2 OCPU/12 GB). It is free *for as long as you own the account*
  — no 30-day expiry, unlike the 30-day US$300 trial credits you also get.
- **Your credit/debit card** is only used to verify your identity (a small temporary
  authorization charge is the most you'll ever see), and to upsell paid services —
  just never upgrade the account or launch non-free shapes.
- Your **budget CPU/RAM**: BBbot uses ~80 MB and 1–2 % CPU. One OCPU/6 GB is plenty;
  you can always scale up to the free max later.

### 4.1 Sign up

1. Go to <https://www.oracle.com/cloud/free/> → **Start for free**.
2. Enter your details, the card, and a phone/email for verification.
3. **Choose your Home Region carefully** — it cannot be changed. This is the data
   center everything you create lives in. Pick the one closest to you **or** one
   known to have A1 capacity (some popular regions often report "out of host
   capacity", see troubleshooting below).

### 4.2 Create the instance

1. After signup you land in the OCI console (choose **"Set up your tenancy"/"Create compute instance"** or navigate: ☰ menu → **Compute → Instances → Create instance**).
2. Name: `bbbot`.
3. **Image:** `Ubuntu 24.04` (canonical-images link; keep "Minimal" or default — both fine).
4. **Shape:** click *Change shape* → **Ampere (A1)** → `VM.Standard.A1.Flex` →
   **1 OCPU / 6 GB RAM** to start (or 2/12 if you want headroom). Save.
5. **Networking:** keep the default new VCN and subnet (public). The bot doesn't
   need any inbound ports except the existing SSH (22).
6. **SSH keys:** choose *Generate a key pair* → **Save Private Key**
   (a `.key`/PEM file). Keep this file safe — it's your only way in.
7. **Create** and wait ~1–2 min. On the instance page copy the **Public IP address**.

> **"Out of host capacity"** — very common for free A1 capacity. Fixes:
> - try again in a day (capacity comes and goes),
> - try a different **Availability Domain**,
> - try 1 OCPU instead of 4 (smaller slices are easier to find),
> - worst case, temporarily create a free `VM.Standard.E2.1.Micro` (tiny AMD box)
>   to have *something* running while you retry A1.
> There are also free-tier *kicker scripts* (a cron loop that re-tries the API until
> an instance is available) — search GitHub for "ocpu out of capacity oracle always free".

### 4.3 Prepare the server

From a terminal on your PC (Windows PowerShell works):

```bash
ssh -i /path/to/yourkey.key -o IdentitiesOnly=yes ubuntu@<PUBLIC_IP>
```

(run `chmod 600` on a .key file on Linux/macOS if it complains).

On the server:

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv git

# make the app directory
sudo mkdir -p /opt/bbbot && sudo chown ubuntu: /opt/bbbot
```

### 4.4 Get the code there

**Option A — Git (recommended):** push `BBbot/` to a GitHub/GitLab repo (keep
`.env` out of it — it's in `.gitignore`), then:

```bash
git clone https://github.com/<you>/BBbot.git /opt/bbbot
```

**Option B — scp from your PC:**

```bash
scp -i /path/to/yourkey.key -r "D:\projects\others\BBbot\*" ubuntu@<PUBLIC_IP>:/opt/bbbot/
```

### 4.5 Install & smoke-test

```bash
cd /opt/bbbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env          # paste your BotFather token into BOT_TOKEN, Ctrl+O, Ctrl+X

python bot.py      # watch it start; then Ctrl+C
```

You should see `BBbot is up and running`. If Telegram says auth fails, re-check
the token.

### 4.6 Keep it running with systemd

```bash
sudo cp /opt/bbbot/bbbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bbbot
sudo systemctl status bbbot
```

Follow live logs (Ctrl+C to exit):

```bash
journalctl -u bbbot -f
```

The `bbbot.service` unit file included here assumes code in `/opt/bbbot` run as
user `ubuntu`. If you created a different user or path, edit `User=`/
`WorkingDirectory=`/`ExecStart=` accordingly.

Now unplug/kill whatever you like — the bot restarts on boot and re-schedules
any running raffle draws automatically after a restart.

### 4.7 Updating the bot code later

```bash
cd /opt/bbbot
git pull                    # or scp the changed files
. .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart bbbot
```

### 4.8 Back up

Everything the bot "knows" is one file: `/opt/bbbot/data/bbbot.db`. Occasionally:

```bash
cp /opt/bbbot/data/bbbot.db ~/backups/bbbot-$(date +%F).db
```

(or push it into private GitHub once in a while). Also take an OCI *manual boot
volume backup* from the instance page for disaster recovery.

### 4.9 Optional hardening (5 minutes, recommended)

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

The bot only makes *outgoing* connections, so nothing else needs opening.
Optionally `sudo apt install fail2ban` and don't share the SSH key.

If one day you switch the bot to webhooks instead of polling, you'd open 443/TCP
in the subnet's security list too — not needed for polling.

---

## 5. Checklist

- [ ] Bot created with @BotFather, token saved, privacy disabled, /setcommands done
- [ ] Group is a supergroup with Topics enabled, bot joined and promoted to admin
- [ ] `.env` has the token; `python bot.py` runs locally and answers commands
- [ ] Oracle account verified, A1 Flex instance created (Ubuntu 24.04, 1 OCPU+)
- [ ] SSH works; code uploaded to `/opt/bbbot`; venv deps installed
- [ ] Smoke-test with `python bot.py` succeeded
- [ ] systemd unit installed and enabled; `journalctl -u bbbot` shows it alive
- [ ] Subscribed a test feed with `/subscribe` and saw a post appear in the topic

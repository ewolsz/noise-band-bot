# Noise band bot

One QQQ option trade a day at most, on an Alpaca **paper** account, run from GitHub Actions.

- Rule: opening range (9:30 to 9:45), a noise band from the last 20 sessions plus 25%, checks every 10 minutes from 10:00 to 2:00 New York time. Past the range, outside the band and on the same side of VWAP: buy puts (down) or calls (up), nearest strike, next trading day's expiry.
- Exit: after a real run, sell when QQQ gives back half of its best gain. Otherwise sell at 3:45 PM.
- Settings: `config/noise_band.json`. Do not change them while the paper test is running.
- Off switch: a file called `STOP` in the top folder.
- Secrets needed: `ALPACA_KEY`, `ALPACA_SECRET` (this bot's own paper account), `NTFY_TOPIC`.

The rule failed its backtest on QQQ shares (2016 to 2020). The option version has never been backtested. This is a forward paper check, to be judged after about 100 trades.

Paper only. Not financial advice.

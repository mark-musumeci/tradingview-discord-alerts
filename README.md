# TradingView Discord alerts

Posts new US stock top stories from [TradingView](https://www.tradingview.com/markets/stocks-usa/news/?market=topstories) to a Discord channel. GitHub Actions checks the feed every 5 minutes.

The workflow needs a repository secret named `DISCORD_WEBHOOK_URL`. `seen.json` tracks the stories already handled.

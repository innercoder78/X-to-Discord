# X to Discord

X to Discord watches one X account and sends its new posts to a Discord channel.

It runs through GitHub Actions and uses a Discord webhook. No Discord bot or continuously running server is needed.

## Features

- Sends posts in the order they were published
- Includes the post text, author, time, first image, and original link
- Skips replies, Quote Posts, reposts, and posts containing mentions
- Prevents older posts from being sent during the first run
- Stores all private information securely with GitHub Actions secrets
- Supports manual and automatic workflow triggers

## Setup

Create a GitHub environment named `monitor-state`.

Add this environment secret:

- `X_POST_ID`  
  Set it to `0` before the first run.

Add these repository secrets:

- `X_SOURCE_TOKEN`  
  The X session token used to access X.

- `X_MONITORED_ACCOUNT`  
  The username to monitor, without the `@`.

- `DISCORD_WEBHOOK`  
  The webhook for the destination Discord channel.

- `GH_UPDATE_TOKEN`  
  A GitHub token that allows the monitor to update `X_POST_ID`.

Never place these values directly in the repository.

## Running It

The monitor can be started manually from the repository’s **Actions** page.

It can also be triggered automatically with a scheduling service such as [cron-job.org](https://cron-job.org/).

On the first run, the monitor remembers the newest available post without sending older posts. Future runs send only qualifying posts published after that point.

## Important Notice

This project uses an unofficial third-party library to access X. Changes made by X may cause it to stop working, and automated access may result in restrictions on the X account being used.

Using a separate X account for monitoring is recommended.

## Disclaimer

This project is not affiliated with X, Twitter, Discord, GitHub, or cron-job.org.

Use it responsibly and at your own risk.

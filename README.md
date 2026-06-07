# X to Discord

X to Discord watches one X account and sends its new posts to a Discord channel.

It runs through GitHub Actions and uses a Discord webhook. No Discord bot or continuously running server is needed.

## Features

* Sends posts in the order they were published
* Includes the post text, author, time, first image, and original link
* Skips replies, Quote Posts, reposts, and posts containing mentions
* Prevents older posts from being sent during the first run
* Safely recovers posts after missed checks without silently skipping them
* Keeps account names, post IDs, post links, tokens, and webhook details out of public files and logs
* Uses locked dependency versions for consistent workflow runs
* Supports manual runs and external scheduling through cron-job.org

## Setup

### GitHub Environment

Create a GitHub environment named:

```text
monitor-state
```

Inside that environment, create this secret:

* `X_POST_ID`
  Set it to `0` before the first run.

`X_POST_ID` should exist only inside the `monitor-state` environment. Do not also create it as a repository secret.

### Repository Secrets

Add these repository secrets:

* `X_SOURCE_TOKEN`
  The X `auth_token` cookie used to access X.

* `X_MONITORED_ACCOUNT`
  The username to monitor, with or without the leading `@`.

* `DISCORD_WEBHOOK`
  The webhook URL for the destination Discord channel.

* `GH_UPDATE_TOKEN`
  A fine-grained GitHub token used to update `X_POST_ID`.

Limit `GH_UPDATE_TOKEN` to this repository and grant it only:

```text
Environments: Read and write
```

Never place any of these values directly in the repository.

## Running It

The monitor can be started manually from the repository’s **Actions** page.

It can also be triggered externally through a scheduling service such as [cron-job.org](https://cron-job.org/).

The workflow itself does not use GitHub’s scheduled workflow feature.

On the first run, the monitor remembers the newest available post without sending older posts. Future runs send only qualifying posts published after that point.

If several checks are missed, the monitor searches additional timeline pages. It stops safely rather than advancing the saved post ID when it cannot confirm that every unseen post was recovered.

## Privacy

The public repository and workflow logs do not display:

* The source X account
* The monitored X account
* X post IDs or post links
* X authentication data
* Discord webhook details
* GitHub token values

The original X link is visible only in the destination Discord channel.

Successful workflow runs display only:

```text
Monitor completed
```

Failed runs display only:

```text
Monitor failed
```

## Important Notice

This project uses an unofficial third-party library to access X. Changes made by X may cause it to stop working, and automated access may result in restrictions on the X account being used.

Using a separate, unimportant X account for monitoring is recommended.

## Disclaimer

This project is not affiliated with X, Twitter, Discord, GitHub, or cron-job.org.

Use it responsibly and at your own risk.

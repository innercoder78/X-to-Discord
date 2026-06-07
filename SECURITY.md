# Security Policy

This repository is intended to be safe for public use, but it depends on private credentials that must never be committed or exposed.

- Treat `X_SOURCE_TOKEN` like a password.
- Treat `DISCORD_WEBHOOK` like a credential. Anyone with the webhook URL may be able to post to the destination channel.
- Treat `GH_UPDATE_TOKEN` like a password. It should be a fine-grained GitHub token limited to this repository.
- `GH_UPDATE_TOKEN` needs only the `Environments: Read and write` permission so the workflow can update the cursor secret.
- `X_POST_ID` belongs only in the `monitor-state` GitHub environment as an environment secret.
- Real credentials and real X data must never be committed, included in examples, or retained in local files.
- If any credential is exposed, revoke and replace it immediately.
- Carefully review workflow changes from untrusted contributors before merging.
- Never use the privileged target variant of pull-request workflows for this project.
- Fork pull requests must never receive repository or environment secrets.
- Keep repository write access restricted.
- Protect the default branch when practical.
- Do not add required reviewers or waiting timers to the `monitor-state` environment unless manual approval is intended for every monitor run.
- Review public workflow logs after the first live run to confirm that no private account, post, webhook, or token data appears.
- Tweety uses an unofficial, reverse-engineered X interface.
- Use a separate, unimportant source X account for `X_SOURCE_TOKEN` when possible.
- X may restrict accounts that use unofficial automated access.

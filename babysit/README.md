# Babysit

You start a long job such as a training run, build, or migration, and Hermes watches its log with zero model calls in the watch loop. It keeps one chat message updated in place and wakes the agent when it detects a non-finite value, out-of-memory error, traceback, stall, watched process ending, or a match for your alert regex.

## Example chat

```text
You: babysit_watch log_path="/tmp/train.log" pid=1234 alert_patterns=["validation failed"]
Hermes: Watching train.log: waiting for log output
Hermes: train.log: alert: validation failed on batch 42
        Elapsed: 81s | Lines seen: 240
```

## Why

Hermes core `watch_patterns` are for rare signals. It delivers at most one matching notification every 15 seconds. Three consecutive rate-limit windows disable pattern watching. A lifetime cap of 8 delivered matches also disables it and switches the process to completion notification. Babysit watches a log directly and can use progress, stall, and alert patterns while the job runs, with its own wake budget so a noisy log never becomes a stream of agent turns.

## How it works

Babysit opens an existing log file once and reads up to 1 MiB of complete lines every 2 seconds through that open handle. It tracks progress lines and updates its original chat message at most once every 5 seconds, when the log has grown or an event fired. An event seen inside that window is shown at the next one. Where each edit would add a new line instead of replacing one (the classic CLI), only event messages are edited in.

Non-finite values, out-of-memory errors, and tracebacks each wake the agent once per watch. Alert patterns and stalls wake it at most once every 15 seconds and 20 times per watch, and the next wake carries a count of the events folded into it. A stall fires after the configured quiet period once the log has produced output, and fires again after more output arrives. If you supply a PID, Babysit reports the process ending when that PID disappears, unless the log already contains a recognized exit or finished status line.

Log text reaches the agent as quoted data labelled untrusted, cut to 200 characters with control characters removed.

Each watch runs in a daemon thread and ends when you stop it, when its session is finalized (`/new` or exit), when the plugin unloads, when its process ends, or after 72 hours, which wakes the agent. The plugin runs at most 8 watches at once.

## Tools and command

- `babysit_watch` starts a watch. It requires `log_path`, the absolute path to an existing regular file. Optional parameters are `pid` (positive integer or null), `progress_regex` (string or null), `stall_minutes` (number, default 10), and `alert_patterns` (array of regex strings, default empty). The result carries the `watch_id` and whether the in-place status message is live.
- `babysit_list` returns the current session's active watches with their IDs, file names, PIDs, line counts, last lines, and the last delivery problem if there was one.
- `babysit_stop` takes a required `watch_id` and stops that watch if the current session started it.
- `/babysit` lists the current session's active watches.

## Setup

Install Babysit as a user plugin, then enable it:

```sh
hermes plugins enable babysit
```

Waking a session requires this setting in the active profile's `config.yaml`:

```yaml
plugins:
  entries:
    babysit:
      allow_gateway_injection: true
```

Without that setting, Hermes refuses the injection. Babysit then posts the event into the chat through the plugin context and `babysit_list` shows the reason.

## What it touches

Babysit reads the selected log file through one read-only handle, opened without following symlinks and checked again from the handle itself, so replacing the path afterwards changes nothing it reads. It accepts absolute paths only and refuses files inside the active profile's Hermes home, the Hermes root that holds every profile, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.config`, `/etc`, `/proc`, `/sys` and `/dev`, files with credential-looking names such as `.env`, `id_*`, `*.pem`, `*.key` and `auth.json`, hard-linked files, and anything that is not a regular file. It never writes to the log, makes no network requests, and spawns no child processes. It creates one daemon thread per watch. Hermes reads the active profile's plugin setting to check injection permission. The watch loop makes no model calls; detected events are sent to the current session, which may start an agent turn.

## Tests

Run from the repository root:

```sh
python -m unittest discover -s babysit/tests -t .
```

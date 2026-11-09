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

Hermes core `watch_patterns` are for rare signals. It delivers at most one matching notification every 15 seconds. Three consecutive rate-limit windows disable pattern watching. A lifetime cap of 8 delivered matches also disables it and switches the process to completion notification. Babysit watches a log directly and can use progress, stall, and alert patterns while the job runs.

## How it works

Babysit opens an existing log file and reads complete lines every 2 seconds. It tracks progress lines, updates its original chat message no more often than every 5 seconds, and sends an event when a detector fires. Non-finite values, out-of-memory errors, and tracebacks each fire once per watch. Alert patterns fire on each matching line. A stall fires after the configured quiet period and can fire again after more log output arrives. If you supply a PID, Babysit reports the process ending when that PID disappears, unless the log already contains a recognized exit or finished status line.

Each watch runs in a daemon thread and stops when you stop it, when its process-ending condition is reached, or after 72 hours. The plugin accepts at most 8 active watches.

## Tools and command

- `babysit_watch` starts a watch. It requires `log_path`, the path to an existing regular file. Optional parameters are `pid` (positive integer or null), `progress_regex` (string or null), `stall_minutes` (number, default 10), and `alert_patterns` (array of regex strings, default empty).
- `babysit_list` returns active watches with their IDs, file names, PIDs, line counts, and last lines.
- `babysit_stop` takes a required `watch_id` and stops that watch if it is active.
- `/babysit` lists active watches.

## Setup

Install Babysit as a user plugin, then enable it:

```sh
hermes plugins enable babysit
```

Gateway session injection requires this setting in the active profile's `config.yaml`:

```yaml
plugins:
  entries:
    babysit:
      allow_gateway_injection: true
```

Without that setting, Hermes rejects gateway injection. Babysit then falls back to posting the event through the plugin context. The setting is not required for classic CLI injection.

## What it touches

Babysit reads the selected log file. Hermes also reads the active profile's plugin setting to check gateway injection permission. Babysit refuses paths inside `/etc`, `~/.ssh`, or the active Hermes home. It creates one daemon thread per watch and does not write to the log. It makes no network requests. The watch loop makes no model calls; detected events are sent to the current session, which may start an agent turn.

## Tests

Run from the repository root:

```sh
python -m unittest discover -s babysit/tests
```

# teams-cli

Light command-line client for Microsoft Teams chats (work/school accounts). No Electron, no browser after login.

It talks to the same private chat service as the Teams web client (protocol taken from
[purple-teams](https://github.com/EionRobb/purple-teams)), not to Microsoft Graph. The API is undocumented and may
change; using it may be against your organisation's policy.

## Requirements

- Python 3.11+, standard library only
- `watch` also needs `websockets` (`pip install --user websockets`) and must be run with `python3 -P`
  (`-I` hides user site-packages)
- Optional: `secret-tool` (libsecret) for keyring storage

## Usage

```
teams_cli.py login                          # opens the browser; paste the final blank-page URL back
teams_cli.py chats [--json]                 # recent conversations, numbered
teams_cli.py read <n|id-fragment> [-n 20] [--json]
teams_cli.py watch [chat ...] [--json]      # live incoming messages (Trouter websocket)
teams_cli.py send <n|id-fragment> "text"    # dry run: only prints what would be sent
teams_cli.py send <n|id-fragment> "text" --yes   # really sends
```

Chat numbers come from the last `chats` run. `0` is usually "Notes (me)", the chat with yourself, a safe place to test.

## Login

`login` opens the Microsoft sign-in page. After signing in you land on a blank page; copy its full address
(Ctrl+L, Ctrl+C), paste it into the terminal and press Enter. In a shell without terminal input (for example `!` in
Claude Code) use `login --url '<copied url>'`. The one-time code in that URL expires within minutes.

## Storage

- Tokens (refresh, access, skype) are stored as separate GNOME keyring items (`secret-tool`, service `teams-cli`).
  A token is only removed from the file after it has been read back from the keyring.
- Without a keyring they stay in `~/.config/teams-cli/state.json` (mode 0600) and a warning is printed.
- The same file holds non-secret state: chat list, names, endpoint id (must stay stable, otherwise Teams reports
  "Endpoint limit exceeded").
- Tokens are never printed.

## Safety

- `send` is a dry run unless `--yes` is given.
- `watch` registers as an extra Teams endpoint (like another device); it sends no activity or read receipts.

## Output

Plain text by default; `--json` for scripts (`chats` and `read` print one JSON array, `watch` prints one object per
line). Errors go to stderr, a failure exits non-zero.

## Layout

- `teams_cli.py`: the CLI
- `tools/teams_perf.py`: read-only render benchmark of teams-for-linux over CDP (wheel scroll only)
- `tools/graph_probe.py`: showed that the Teams Graph token has no `Chat.Read*` scopes (HTTP 403)

## Not done

Attachments and cards are shown as placeholders; replies, reactions, starting new 1:1 chats, marking messages read.

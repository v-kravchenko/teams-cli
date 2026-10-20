# teams-cli

Light command-line client for Microsoft Teams chats (work/school accounts). No Electron, no browser after login.

It talks to the same private chat service as the Teams web client (protocol taken from
[purple-teams](https://github.com/EionRobb/purple-teams)), not to Microsoft Graph. The API is undocumented and may
change; using it may be against your organisation's policy.

## Requirements

- Python 3.11+, standard library only
- `watch` also needs `websockets` (`pip install --user websockets`); it is found in user site-packages even under `python3 -I`
- Optional: `secret-tool` (libsecret) for keyring storage

## Usage

Install the `teams` command: `ln -s "$PWD/teams_cli.py" ~/.local/bin/teams` (the file is executable, no flags needed).
Below `teams_cli.py` and `teams` are the same.

```
teams_cli.py login                          # opens the browser; paste the final blank-page URL back
teams_cli.py chats [--json|--ids]           # recent conversations: kind, unread count, preview
teams_cli.py members <chat> [--json]        # participants (partial for large chats)
teams_cli.py read <chat> [-n 20] [--before ID] [--json]
teams_cli.py watch [chat ...] [--json]      # live events (Trouter websocket)
teams_cli.py download <chat> <id> [-o DIR]  # save image attachments (files: prints the SharePoint url)

# writes: dry run unless --yes
teams_cli.py send <chat> "text" [--reply-to ID] [--mention NAME ...] [--image PATH] [--json] --yes
teams_cli.py edit <chat> <id> "new text" --yes
teams_cli.py delete <chat> <id> --yes
teams_cli.py react <chat> <id> <like|heart|laugh|...> [--remove] --yes
teams_cli.py mark-read <chat> [id] --yes    # moves the read marker (default: newest message)
```

`--mention NAME` needs `@NAME` in the text; NAME is part of a name of someone who wrote in the chat recently.

Chat numbers come from the last `chats` run and shift with chat activity, so they only work for reads (`read`, `members`, `watch`, `download`). Write commands (`send`, `edit`, `delete`, `react`, `mark-read`) require the full chat id (`chats --ids`, `48:notes` for the self chat). `0` is usually "Notes (me)", the chat with yourself, a safe place to test.

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
line). A message is always `id, chat_id, time, from, from_id, type, text, html, attachments, mentions, reactions,
reply_to, edited, deleted`. In `--json` mode errors are `{"error": "..."}` on stdout with exit code 1.

`watch --json` events (`event` field): `message`, `message_updated`, `message_deleted`, `typing`, `typing_stop`, `read`
(`chat_id`, `read_up_to`), `system`, `status` (`connected`, `disconnected`, `stalled`, `auth_failed`).

## Layout

- `teams_cli.py`: the CLI
- `tools/teams_perf.py`: read-only render benchmark of teams-for-linux over CDP (wheel scroll only)
- `tools/graph_probe.py`: showed that the Teams Graph token has no `Chat.Read*` scopes (HTTP 403)

## Not done

Non-image files (SharePoint, other auth), cards, starting new 1:1 chats, channel threads, search, presence.

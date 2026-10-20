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
teams_cli.py login [--url URL|-]            # print the sign-in URL; --url completes the login
teams_cli.py chats [--json|--ids]           # recent conversations: kind, unread count, preview
teams_cli.py chats --all [--refresh]        # every conversation incl. channels (cached; unread is a dot)
teams_cli.py teams [--json] [--refresh]     # teams -> channels tree, unread dots (cached for 15 min)
teams_cli.py members <chat> [--json]        # participants (partial for large chats)
teams_cli.py people <name|email> [-n 10] [--json]  # directory search; prints mri and the 1:1 chat id
teams_cli.py presence [me|oid|mri ...] [--json]    # availability, activity, out of office
teams_cli.py search "text" [-n 20] [--json]        # server-side message search in all chats
teams_cli.py read <chat> [-n 20] [--before ID] [--refresh] [--json]
teams_cli.py watch [chat ...] [--json]      # live events (Trouter websocket)
teams_cli.py download <chat> <id> [-o DIR]  # save image attachments (files: prints the SharePoint url)

# writes: dry run unless --yes
teams_cli.py send <chat> "text" [--reply-to ID] [--mention NAME ...] [--image PATH] [--json] --yes
teams_cli.py edit <chat> <id> "new text" --yes
teams_cli.py delete <chat> <id> --yes
teams_cli.py react <chat> <id> <like|heart|laugh|...> [--remove] --yes
teams_cli.py mark-read <chat> [id] --yes    # moves the read marker (default: newest message)

teams_cli.py logout                         # remove tokens and the cache
teams_cli.py cache [clear]                  # clear the cache only
```

`--mention NAME` needs `@NAME` in the text; NAME is part of a name of someone who wrote in the chat recently.

Chat numbers come from the last `chats` run and shift with chat activity, so they only work for reads (`read`, `members`, `watch`, `download`). Write commands (`send`, `edit`, `delete`, `react`, `mark-read`) require the full chat id (`chats --ids`, `48:notes` for the self chat). `0` is usually "Notes (me)", the chat with yourself, a safe place to test.

## Login

`teams login` does not open a browser: it prints the Microsoft sign-in URL. Sign in, land on the blank page, copy its full address and complete the login in a second command:

```
teams login --url '<the copied URL>'
```

A GUI can pass `--url -` and write the URL to stdin instead. The one-time code in that URL expires within minutes.

## Storage

- Tokens (refresh, access, skype; presence, substrate and aggregator on first `presence`/`search`/`teams`) are stored as separate GNOME keyring items (`secret-tool`, service `teams-cli`). A token is only removed from the file after it has been read back from the keyring.
- Without a keyring they stay in `~/.config/teams-cli/state.json` (file 0600, directory 0700) and a warning is printed.
- The same file holds non-secret state that must survive restarts: the Trouter `endpoint` id (a new one hits "Endpoint limit exceeded"), region, chat host and token expiry times.
- The cache lives in `~/.cache/teams-cli/` (0700). It is plain JSON, readable only by your user (files 0600). JSON is enough at this size (3.4 MB parses in ~30 ms); a database would only pay off if full message history were cached.
  - `csa.json` — teams, channels and chats from the chat service aggregator (one request, ~5 s, ~2.3 MB), trimmed to
    what `teams` and `chats --all` show. Refetched after 15 min or with `--refresh`.
  - `conversations.json` — the full conversation list (1217 conversations, ~3.4 MB), only used when the aggregator fails.
  - `chatinfo.json` — chat ids for list numbers, chat titles and peer names.
  - `messages/<hash>.json` — the last 50 messages per chat, used only as an offline fallback for `read` (and as the first paint for a GUI). `watch` keeps it current.
  - `profiles.json` — directory display names, cached for 7 days.
- `read` always fetches fresh messages. If the network fails it prints a warning and serves the cached page; `--refresh` disables the fallback.
- `teams logout` and `teams cache clear` remove the cache; the next `chats`/`teams` refetches it (one slow fetch, ~6 s).
- Tokens are never printed.

## Security

- `send` is a dry run unless `--yes` is given; `watch` registers as an extra Teams endpoint and sends no read receipts.
- Tokens go to the keyring; without one they stay in `state.json` (file 0600, directory 0700). Tokens are never printed or cached.
- State and cache files are 0600 in 0700 directories, written atomically; a corrupt or newer cache is ignored and refetched.
- Server-provided text is stripped of terminal control characters before printing (`--json` is raw). Images are downloaded only from `https://*.asm.skype.com`, and saved as 0600.
- The cache is plain text: any process running as you can read it. That is the same trust level as the keyring; use full-disk encryption if data at rest matters. `teams logout` / `teams cache clear` remove it.
- Residual risks: the API is undocumented and may be blocked by policy; same-user malware or root can read the tokens and the cache.

## Limits

Chat writes (`send`, `edit`, `delete`, `react`, `mark-read`) are rate-limited to **3 calls per 5 seconds** per
account; going faster returns HTTP 429 `API calls quota exceeded! 3Per05Secs`. There is no throttling in the CLI, so
a loop over many writes needs its own pause (~2 s) and a retry on 429. Deleting messages is a soft delete; the chat
keeps showing `(deleted)` in place of the text.

## Output

Plain text by default; `--json` for scripts (`chats` and `read` print one JSON array, `watch` prints one object per
line). A message is always `id, chat_id, time, from, from_id, type, text, html, attachments, mentions, reactions,
reply_to, thread_id, edited, deleted`. `thread_id` is the root message of a channel thread (null for the root
itself and in chats). `search` results have `id, chat_id, thread_id, chat, from, time, text` (`id` is derived from the send time, which
is how Teams numbers messages). In `--json` mode errors are `{"error": "..."}` on stdout with exit code 1.

`chats` rows carry an exact `unread` count (capped, see `unread_capped`). `chats --all` rows have no `n`/number and
`unread` is `true` (the server's read flag), `false`, or `null` when the server has no flag for that conversation — no
per-chat requests are made there. Untitled chats are named like the client: the 1:1 peer, or the first three members of
a group (`Ann, Ben, Cid +2`); names come from the directory (cached for 7 days). `teams --json` is
`[{id, name, favorite, archived, members, channels: [{id, name, kind, unread, pinned, muted}]}]` with `kind` =
`general`, `channel` or `private`. Channels follow the client's order: General, pinned, then by name; channels you have
not joined are left out. If the aggregator fails, the tree is built from the conversation list without the flags.

`watch --json` events (`event` field): `message`, `message_updated`, `message_deleted`, `typing`, `typing_stop`, `read`
(`chat_id`, `read_up_to`), `system`, `status` (`connected`, `disconnected`, `stalled`, `auth_failed`).

## Tests

```
python3 -m unittest discover -s tests -v
```

The suite is offline: API responses are fixtures. Read-only live checks are opt-in:

```
TEAMS_LIVE=1 python3 -m unittest discover -s tests -p 'test_live.py' -v
```

## Layout

- `teams_cli.py`: the CLI
- `tests/`: offline unit tests and fixtures
- `tools/teams_perf.py`: read-only render benchmark of teams-for-linux over CDP (wheel scroll only)
- `tools/graph_probe.py`: showed that the Teams Graph token has no `Chat.Read*` scopes (HTTP 403)

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Not affiliated with Microsoft.

## Not done

Non-image files (SharePoint, other auth), cards, starting a 1:1 chat that does not exist yet, replying in a channel
thread, full member lists of large chats.

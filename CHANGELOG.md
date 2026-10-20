# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/); the release
workflow publishes the section of the pushed tag as the release notes.

## [0.2.0] - 2026-10-20

### Added

- `teams` reads the tree from the chat service aggregator (CSA) in one request: real team names, member counts,
  favourite and archived teams, and per channel `unread`, `pinned` and `muted` flags (text output: a `•` dot and tags).
- Channels follow the client's order: General, pinned channels, then by name. Archived teams are listed last.
- `chats --all` reads from the aggregator too: untitled chats are named like the client (the 1:1 peer, or
  `Ann, Ben, Cid +2` for groups), and `unread` is the server's read flag, so it is no longer `null`.

### Changed

- `teams` and `chats --all` share one cache, `~/.cache/teams-cli/csa.json`, refreshed after 15 min or with
  `--refresh` (the `teams` tree was cached for 24 h). The full conversation list is only fetched when the
  aggregator fails.
- `chats --all` lists every joined channel, not only channels with activity, and leaves out system streams
  (`48:drafts`, `48:calllogs`, ...) and channels of teams you have left.

### Security

- The aggregator token (`csa_token`) is stored in the keyring with the other tokens and removed by `logout`.

## [0.1.0] - 2026-10-20

First release.

- Login without a browser after sign-in; tokens in the GNOME keyring (0600 file fallback).
- Read: `chats` (unread counts, previews), `chats --all`, `teams`, `read`, `members`, `people`, `presence`,
  `search`, `download`, `watch` (live events over the Trouter websocket).
- Write (dry run unless `--yes`): `send` (reply, mentions, image), `edit`, `delete`, `react`, `mark-read`.
- `--json` output for every command; local JSON cache in `~/.cache/teams-cli`.

[0.2.0]: https://github.com/v-kravchenko/teams-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/v-kravchenko/teams-cli/releases/tag/v0.1.0

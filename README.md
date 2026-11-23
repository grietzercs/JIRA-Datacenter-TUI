# jira-tui (`jiratui`)

A keyboard-driven terminal UI for **Jira Server / Data Center** (REST API 2 + Agile API 1.0). You can:
browse **sprints** and the **backlog** of a project's boards with label / person / saved custom
filters, run **JQL searches** across all projects with autocomplete and history, open an **issue**
(description, comments, links, sub-tasks, Bitbucket pull requests) and transition / comment / assign it, and **create issues**
from a form generated from Jira's create metadata.

This tool was created as a way to more quickly interact with Jira Datacenter endpoints with less overhead. The paradigm for design was a developer who wears many hats in a "startup" like environment where stories _can_ be created and edited, but greater sprint actions such as sprint goals or closing sprints *should* not be taken. Therefore, this TUI is designed around just that: anything the individual developer needs, not managing a team.

## Stack

Python 3.9+ with [Textual](https://textual.textualize.io/) (TUI), [httpx](https://www.python-httpx.org/)
(async HTTP) and [Rich](https://rich.readthedocs.io/) (rendering, including Jira wiki markup). The macOS
system `python3` is enough, and everything installs into a project-local `.venv`. Textual includes a
headless test driver (Pilot), which the test suite uses for the login → sprints → filters → backlog →
JQL → issue → create flows.

## Install and run

```sh
cd jira-tui
make run          # creates .venv on first run, installs, launches (passes ./.env if present)
make demo         # offline demo against a built-in fake Jira DC server
make test         # run the test suite
```

Without `make`:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/jiratui                    # or: .venv/bin/python -m jiratui
```

### Pointing it at a server

Type the base URL into the login screen, for example `https://jira.example.com` or
`https://host.example.com/jira` (a context path is kept). Pasted browser or REST URLs
(`/browse/PLAT-1`, `/secure/Dashboard.jspa`, `/rest/api/2/...`) are trimmed, and only
`http(s)://` is accepted. You can also preset it:

```sh
.venv/bin/jiratui --url https://jira.example.com --user jdoe
JIRA_URL=https://jira.example.com .venv/bin/jiratui
```

## Authentication

| Method | Fields | Header sent |
|---|---|---|
| **Bearer (PAT)** | Jira DC/Server personal access token (Jira **8.14+**); username optional (checked against `/myself`) | `Authorization: Bearer <PAT>` |
| **Basic** | username + password **or** username + token | `Authorization: Basic base64(user:secret)` |

**Detect** (Ctrl+T) probes `/rest/api/2/serverInfo` **without credentials**. It reports the version
and deployment type, marks Bearer as unsupported on Jira older than 8.14 (or on Cloud), and preselects
the best method. You can still override the choice; if Bearer then fails on an old server, the error
says why.

**Connect** (Ctrl+S) runs the same probe, then calls `/rest/api/2/myself`, which proves the
credentials. Errors are reported specifically:

- 401 (bad or expired token, or wrong Bearer/Basic choice)
- 403
- `X-Seraph-LoginReason: AUTHENTICATED_FAILED`, which Jira can send even with HTTP 200 on
  anonymously readable resources
- CAPTCHA lockout: `X-Authentication-Denied-Reason: CAPTCHA_CHALLENGE` /
  `AUTHENTICATION_DENIED`, with a hint to log in once in a browser or use a PAT
- timeouts
- TLS verification failures, with a hint about internal CAs
- DNS failures and refused connections
- redirects to an SSO or login page (redirects are never followed)
- HTML pages instead of JSON (the page `<title>` is shown)

### Ways to supply credentials

1. Type them into the login form. The token field is masked.
2. Environment variables, picked up automatically or with **Ctrl+E**: `JIRA_URL`,
   `JIRA_AUTH` (`bearer`|`basic`), `JIRA_USER`/`JIRA_USERNAME`, `JIRA_PAT`/`JIRA_TOKEN`,
   `JIRA_PASSWORD`, `JIRA_CA_BUNDLE`.
3. `--env-file path/to/.env`. `make run` passes `./.env` automatically. See `.env.example`.
4. The macOS Keychain, **opt-in**: tick "Save token in macOS Keychain". It is stored under
   service `jira-tui` via the `keyring` library, so the token never appears on a command line.
   Next time the token is pre-filled.

There is deliberately **no `--token` flag**, because tokens on the command line end up in shell
history and `ps`.

"Remember server URL and username" writes only non-secret settings (plus the last project) to
`~/.config/jira-tui/config.json` (mode 0600; `$XDG_CONFIG_HOME` is respected). `jiratui --forget`
deletes that file. Keychain items can be removed in Keychain Access by searching for `jira-tui`.

## TLS

- Verification is **on** by default, using the `certifi` bundle.
- **Also trust macOS Keychain CAs** is on by default on macOS. Disable it with `--no-keychain-cas`.
- **Custom CA bundle**: `--ca-bundle ~/certs/corp.pem`, `JIRA_CA_BUNDLE`, or the TLS options panel.
- **Disable verification**: `--insecure` or the checkbox. It shows loud warnings and is never
  remembered between runs.

## Keybindings

| Where | Keys |
|---|---|
| Global | **Ctrl+Q** quit · **F1** / **?** help · **Esc** cancel the running request, else go back (asks before logging out) · Tab/Shift+Tab move focus · Enter open/select |
| Login | **Ctrl+S** connect · **Ctrl+T** detect auth methods · **Ctrl+E** load from environment · Esc cancel |
| Main | **F2** Sprints · **F3** Backlog · **F4** Issues/JQL (also 1/2/3 when a table is focused) · **Ctrl+N** create issue |
| Any list | ↑/↓/PgUp/PgDn (the next page loads automatically near the end) · **m** next page · **r** reload/retry · **Enter** open issue |
| Sprints & Backlog | **p** project · **b** board · **l** labels · **a** people · **f** custom filters · **F** new custom filter · **X** clear filters |
| Sprints | **x** show/hide closed sprints · **s** focus the sprint list |
| Issues (JQL) | **Enter** run · **→** accept completion · **Ctrl+R** history · **Ctrl+O** saved queries · **Ctrl+S** save query · **Ctrl+T** insert helper · **Ctrl+B** order by · **Ctrl+Y** issue types · **Ctrl+L** focus input |
| Issue | **t** transition · **n** comment · **a** assign · **g** go to epic/parent/sub-task/link · **R** refresh |
| Create | **Ctrl+P** project · **Ctrl+T** issue type · **Ctrl+S** create · **Ctrl+O** open the created issue |
| Pickers | type to filter · Enter choose · Space toggle · **Ctrl+S** apply · **Ctrl+X** clear · Ctrl+R retry loading |

## Views

- **Sprints**: project → boards of that project (`/rest/agile/1.0/board?projectKeyOrId=`, the first
  scrum board is preselected) → sprints (active, then future; closed with `x`, most recent first) →
  issues. Columns: key, type, summary, status, assignee, priority, story points, labels. The story-points
  field is found via the board's estimation configuration, falling back to `/rest/api/2/field`
  ("Story Points" custom field).
- **Backlog**: scrum boards use `/rest/agile/1.0/board/{id}/backlog`. Kanban boards and projects
  without a board (or "(no board)" in the board picker) use the JQL equivalent:
  `project = X AND statusCategory != Done`.
- **Issues / JQL**: search across all projects and issue types. The query is sent with `POST /search`,
  so long JQL never hits URL limits.
- **Issue detail**: all the usual fields plus sprint, story points, epic link, parent, Jira wiki
  markup rendered as Rich text (headings, lists, tables, code/quote/panel blocks, links, mentions),
  sub-tasks, links and comments.
- **Create**: project → issue type → a generated form. Create metadata comes from the DC 8.4+
  `/issue/createmeta/{project}/issuetypes[/{id}]` endpoints, falling back to the legacy
  `/issue/createmeta?expand=projects.issuetypes.fields`. The form shows summary, description, assignee,
  priority, labels, components, story points, epic link and sprint (when the create screen has them),
  plus any other field that is required and has no default. Required fields are checked before
  sending. Jira's `errors` map is shown under each field and `errorMessages` at the top.

## Filters & JQL

The Sprints and Backlog views share one filter model and one set of components:

- **Labels**: labels actually used in the project, gathered from a bounded search
  (`project = X AND labels is not EMPTY`, up to 1000 issues). If the project has none, the list comes
  from `/jql/autocompletedata/suggestions?fieldName=labels`. Multi-select.
- **People**: assignable users (`/rest/api/2/user/assignable/search?project=X`) plus **Unassigned**.
  Multi-select.
- **Custom filters**: named, saved filters. Each has structured parts (status / issue type /
  priority / text contains) and an optional raw JQL fragment such as `priority = High AND status != Done`.
  The JQL is checked by the server before saving. Filters are scoped to the project or global, and
  stored in `~/.config/jira-tui/filters.json` (respects `XDG_CONFIG_HOME`, mode 0600). Your Jira
  **favourite filters** (`/rest/api/2/filter/favourite`) are listed too, read-only.

Combination rule: **AND across categories, OR within** labels and people. For example:

```
sprint = 12 AND labels in ("ux/ui", "backend") AND (assignee in ("mary ann") OR assignee is EMPTY)
  AND status = "In Progress" AND (priority = High OR priority = Highest) ORDER BY Rank ASC
```

Filtering is server-side, and the effective JQL is always shown under the filter bar. Every value is
double-quoted with `\` and `"` escaped, so labels like `ux/ui`, `release:2.0`, `o'reilly` and usernames
like `mary ann` or `j.smith@corp` are safe. Custom fields are referenced as `cf[NNNNN]`. A fragment with a
top-level `OR` is parenthesised, and an `ORDER BY` inside a fragment is dropped.

In the Issues view, completion uses `/rest/api/2/jql/autocompletedata` (field names, operators,
functions) and `.../suggestions` (values). The best match is shown inline (→ accepts it) and more
candidates are listed below the input. Invalid JQL shows each of Jira's `errorMessages`. History
(last 100) and saved queries are kept in `~/.config/jira-tui/queries.json`.

## Security notes

- Tokens are never logged. An opt-in debug log
  (`--debug-log FILE` or `JIRATUI_DEBUG_LOG`, file mode 0600, gitignored by `*.log`) records method,
  path, parameters (including JQL), status and timing. Authorization/Cookie headers are replaced with
  `***REDACTED***`, and every typed secret, plus its Basic base64 form, is scrubbed from every log line
  and error message.
- Mutating requests send `X-Atlassian-Token: no-check`. Only GETs and searches are retried
  automatically. Create, comment, transition and assign are never retried.
- `.env`, `*.log`, `*.pem`/`*.key`, venvs and caches are gitignored. `--demo` stores its filters
  and history in a temporary directory, never in your real config.

## Troubleshooting

- **401 with Bearer on an older Jira**: PATs need Jira Server/DC 8.14+. Press Detect; use Basic if
  it says PATs are not supported.
- **"CAPTCHA_CHALLENGE"**: too many failed Basic logins. Log in once in a browser, or switch to a PAT.
- **"Server redirected to …/login" or "HTML page instead of JSON"**: the URL hits an SSO page or a proxy.
  Use the Jira base URL, including its context path (e.g. `/jira`).
- **TLS errors**: enable "Trust macOS Keychain CAs" or pass `--ca-bundle`.
- **Sprint JQL rejected**: if the instance rejects `sprint = N`, the Sprints view falls back to
  `/rest/agile/1.0/sprint/{id}/issue` with the filters as its `jql` parameter, and says so.
- **Anything odd**: run with `--debug-log ~/jiratui.log` (redacted) and look at the requests.

## Tests

```sh
make test     # or: .venv/bin/python -m pytest -q
```

Everything runs offline against `jiratui/testing/fake_server.py`, an in-memory fake Jira DC
(`httpx.MockTransport`) with a small JQL engine (`fake_jql.py`), plus `respx`. The suite covers:

- **API client**: URL normalisation; auth headers (and no credentials on the probe); PAT version
  detection; Bearer/Basic login; Seraph/CAPTCHA headers; error mapping (400–500, HTML, SSO
  redirects, timeouts, TLS, DNS); bounded retries (searches retried, creates not); all three paging
  styles; boards/sprints/backlog; createmeta (new and legacy); create/transition/comment/assign.
- **JQL builder**: quoting/escaping, field refs, OR-wrapping, quote-aware ORDER BY handling, filter
  combination, and round-trips through the fake JQL engine, including labels and usernames with
  special characters.
- **Persistence**: `filters.json` and `queries.json` (XDG, 0600, project vs global scope), config, keychain.
- **Redaction**, a real debug-log check, and the wiki renderer.
- **Textual Pilot**: login errors and auth detection; sprints with label/person/custom filters;
  custom filter create/edit/delete and Jira favourites; backlog (Agile and JQL equivalent); JQL search
  (autocomplete, errors, paging, type filter, order by, helpers, saved queries, history); issue detail
  actions; create (local and server field errors, legacy createmeta, sub-task parent); Esc-cancel,
  UI timeout, 500 retry, logout.

## File structure

```
jiratui/
  api/        REST and Agile client, auth headers, error mapping, paging, TLS setup, response models
  tui/        Textual app and screens (login, main, boards, issue, create, JQL search, filters)
  testing/    In-memory fake Jira server and JQL engine used by tests and `make demo`
  config.py   Settings, .env parsing, saved config
  filters.py  Custom filter model and storage
  jql.py      JQL builder (quoting, field references, filter combination)
  wiki.py     Jira wiki markup renderer
  keychain.py Opt-in macOS Keychain storage
  redact.py   Secret scrubbing for logs and errors
  logsetup.py Opt-in debug log
tests/        Offline test suite (unit, fake server, Textual Pilot)
```

## Contributing

| Adding | Goes in |
|---|---|
| A Jira REST or Agile call or response model | `jiratui/api/` |
| JQL generation or filter logic | `jiratui/jql.py`, `jiratui/filters.py` |
| A screen or widget | `jiratui/tui/` |
| A new endpoint on the fake server | `jiratui/testing/fake_server.py` |
| A test | `tests/test_<area>.py` |

Rules:

- `api/` never imports from `tui/`.
- Every network call needs a timeout; only GETs and searches are retried. Create, comment, transition and assign are never retried.
- Quote every value placed into JQL through `jql.py`.
- Anything user-typed that is secret must pass through `redact.py` before it is logged or shown.
- Tests run offline; do not add tests that need a real server.

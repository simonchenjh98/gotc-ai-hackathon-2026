# Frontend HTML rendering review

The November 18 source review found **221 markup writes in 33 JavaScript files**. Counting assignments and method calls individually gives 25 empty writes, 47 fixed literals and 149 expressions that compose markup. A single source line can contain several writes. The count includes assignments to `innerHTML` and `outerHTML` and calls to `insertAdjacentHTML`.

## Review method

Followed the setters and their upstream template helpers, including helpers that return markup without assigning it. Checked interpolation placement in element text, preformatted blocks, textarea content and quoted attributes. Reviewed computed navigation and file destinations separately because HTML encoding alone does not validate a URL. Only synthetic fixture values were used.

The rendered interface retains intentionally structured table cells, forms, badges and buttons. The platform text encoder `esc` and the directory encoder `escape` cover ampersand, angle brackets and both quote characters. The upload result link now uses a validated same-origin material URL and assigns the filename through `textContent`. The user-selected registration and review data stays in DOM properties or passes through an encoder wherever it is inserted in text or quoted attributes.

## Source inventory

All paths below are relative to `frontend/static/`. Each line number refers to the rendering operation, including writes that only clear an old view. The three counts are empty / fixed / composed.

| File | Counts | Source lines | Reviewed data boundary |
| --- | --- | --- | --- |
| `platform/admin/api.js` | 1 / 4 / 18 | 59, 65, 90, 91, 95, 96, 100, 103, 107, 109, 117, 119, 133, 134, 141, 145, 155, 183, 186, 187, 219, 240 | The API console accepts encoded cells for badges and action buttons. Its four table callers encode model names, account names, URLs and error labels, and convert counters to numbers. Cards encode each value. Provider selectors use known platform keys. |
| `platform/admin/directory.js` | 5 / 14 / 24 | 106, 143, 156, 177, 185, 186, 194, 195, 218, 233, 264, 265, 308, 314, 315, 320, 321, 344, 358, 362, 364, 372, 374, 381, 387, 390, 410, 450, 451, 459, 475, 482, 484, 501, 505, 511, 538 | Directory, import, audit and historical review renderers encode identities, free text, audit JSON and form values. Directory role attributes are encoded. Export and template destinations are fixed application routes with encoded query arguments. |
| `platform/admin/event.js` | 0 / 3 / 7 | 17, 23, 30, 38, 49, 50, 69, 73, 80, 94 | Organizer identity, team labels, role attributes and feedback use HTML encoding. Stage labels and controls come from fixed stage settings. Team numbers stay inside text nodes in the rendered table. |
| `platform/admin/settings.js` | 1 / 2 / 1 | 20, 22, 30, 34 | Settings comparisons format known fields, then encode text and field values. Conflict controls are first-party markup. |
| `platform/admin/submissions.js` | 0 / 1 / 9 | 3, 12, 16, 19, 26, 69, 76, 79 | Submission and roster detail cards encode text and link fields. Openable material links are filtered by the common material URL policy. Counts derive from local aggregation. |
| `platform/claims/application.js` | 0 / 0 / 1 | 63 | Claim status, notices and editable fields compose the claim form helpers. The form encodes names, track, school and receipt values. Status messages use textContent. |
| `platform/claims/review.js` | 0 / 1 / 5 | 36, 50, 52, 75, 79, 83 | Organizer forms encode declarations, IDs, roster fields and returned credentials. Table selectors and buttons use quoted attributes. File results render encoded preview data. |
| `platform/finals/dashboard.js` | 1 / 0 / 3 | 55, 81, 85, 110 | Summary and award helpers encode team/award labels and restrict counts to finite nonnegative integers. Stable markup is reused between polling updates. |
| `platform/finals/judges.js` | 0 / 2 / 4 | 72, 125, 127, 132, 150 | Roster choices, candidate names and scored dimension labels are escaped. Control state is fixed markup or a number. Raw input remains in form controls. |
| `platform/finals/organizer.js` | 0 / 0 / 2 | 57, 70 | Rosters encode identifiers and display labels. Checkbox state and window controls come from booleans and known configurations. |
| `platform/finals/shared.js` | 0 / 1 / 4 | 96, 118, 142, 161, 212 | Shared panels compose the score and award renderers. Names, labels and notes use encoding, while summaries format numbers. Cached markup originates in those same renderers. |
| `platform/gateway/keys.js` | 4 / 1 / 6 | 9, 10, 31, 54, 58, 62, 72, 84, 89, 90 | API key labels, announcements and snippets are encoded on display. Max-key count is numeric. Empty writes on logout, disposition or invalid identity remove prior private output. |
| `platform/gateway/logs.js` | 0 / 0 / 3 | 56, 68, 72 | Structured calls, replies, reasoning, headers and raw JSON use text encoding inside pre elements. Dialog controls are literal markup. A JSON download uses a generated Blob URL. |
| `platform/judging/audience.js` | 0 / 1 / 1 | 11, 18 | Vote limit is numeric. Team number, name and project are encoded. Call-to-action text follows known voting states. |
| `platform/judging/boards.js` | 0 / 4 / 8 | 26, 27, 31, 32, 33, 39, 40, 43, 44, 45 | Board rows encode team numbers, ranks and labels. Counts and averages are formatted numbers. Connection-state markup is fixed and network messages are encoded. |
| `platform/judging/judges.js` | 0 / 2 / 14 | 6, 8, 12, 13, 17, 21, 30, 46, 53, 55, 57, 68, 73, 80, 85, 93 | Pickers encode identifiers, team numbers, projects and score labels. Materials pass through the common material renderer. Textareas receive escaped feedback. |
| `platform/navigation.js` | 0 / 1 / 0 | 33 | The route loading state is fixed text. The router allows named entries only. |
| `platform/roadshow/login.js` | 0 / 0 / 1 | 55 | The name-login template is first-party markup. Availability, server errors and in-flight status update via textContent. |
| `platform/roadshow/roster.js` | 0 / 0 / 1 | 42 | Roster names are encoded in table and textarea contexts. Schedule messages are encoded. List size is numeric. |
| `platform/roadshow/scoring.js` | 0 / 2 / 5 | 83, 123, 125, 126, 131, 183 | Inputs, summaries and material cards use escaped team IDs, labels and notes. Widget state is derived locally. Scores are explicitly formatted. |
| `platform/routes.js` | 0 / 0 / 1 | 28 | The closed-scoreboard view composes fixed text and one internal link. |
| `platform/shell.js` | 0 / 1 / 5 | 63, 120, 137, 148, 165, 217 | The header encodes the active display name. Heading and empty-state callers supply fixed copy or escaped values, with an intentional first-party score counter in the judge subtitle. Identity choices from the service use Option(label, value). |
| `platform/submissions/history.js` | 0 / 0 / 3 | 8, 9 | History helpers encode request IDs, project names and revisions, including quoted container IDs. Repository targets use approved repository URL/render helpers. |
| `platform/submissions/materials.js` | 0 / 0 / 1 | 108 | The material and repository renderers encode labels, URLs and free text. Approved URL schemes and repository shape are checked before rendering a link. Backup URLs must match the same-origin team/version/file route. |
| `platform/team/delegation.js` | 0 / 0 / 1 | 28 | Names and IDs in a delegated team picker are encoded. The operator notice checks the active team before rendering its encoded name. |
| `platform/team/recovery.js` | 2 / 3 / 4 | 32, 38, 41, 42, 43, 44, 46, 49 | Local drafts are treated as user input. Comparison and receipt templates encode restored text. Stored versions are numeric and candidate buttons use local indexes. |
| `platform/team/repository.js` | 0 / 0 / 2 | 36, 49 | Repository notices encode provider and server messages. Repository/join anchors use checked HTTPS URLs. Hints and action text come from fixed state mappings. |
| `platform/team/score.js` | 0 / 1 / 1 | 6, 28 | Dimension and feedback strings are encoded, including anonymous comments and award text. Published and pending statuses select fixed markup. |
| `platform/team/submission.js` | 10 / 0 / 6 | 49, 53, 54, 55, 57, 61, 62, 63, 64, 79 | Fields use a local specification, with values encoded in quoted attributes and textareas. Versions are numeric. Preview, conflict and receipt panels delegate to material and history helpers. |
| `registration/admin.js` | 1 / 1 / 1 | 7, 13, 29 | Contact and roster fields from the service are encoded before insertion. Revoked sessions clear private roster content. |
| `registration/directory.js` | 0 / 0 / 2 | 11, 16 | Public team and member records are encoded, including display numbers and target size. Profile/claim links derive from the deployment-selected registration endpoint. Input and status updates use DOM properties. |
| `registration/profile.js` | 0 / 2 / 4 | 12, 16, 22, 36, 56, 77 | Roster rows and profile form values use HTML encoding. Server notices update through textContent. Loading, permissions and locked states select fixed copy. |
| `site/header.js` | 0 / 0 / 1 | 14 | Navigation labels and route keys are first-party constants. Links are generated with URL from the page base or the build-selected HTTPS origin. |

## Regression coverage

The directory tests exercise markup-like names, team numbers and roster sizes. Rendering-boundary tests cover organizer metrics, badge attributes and history identifiers. Submission upload tests exercise successful and stale receipts and file links. Existing judge, claim, repository, administration and gateway suites cover their corresponding role views. The build tests verify the HTML and lazy loader link to digest-matching scripts and styles on both integrated and separately hosted deployments. Run `node --test tests/frontend/*.test.mjs` from the repository root.

This review covers the current source. A new helper that accepts pre-rendered HTML, interpolates an unquoted attribute, or assigns a navigation destination needs its own context-specific review.

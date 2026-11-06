# Scoring team directory integration

Roadshow and finals use the same stable team IDs as the organizer team directory and public team registration. Judge selectors show each stage's saved roster; reading a judge page never enrolls a team or grants judging access.

## Confirmed behavior

- Roadshow organizers can read the directory in the roadshow console, select all eligible teams or a subset, then save without opening scoring or changing judge permissions.
- Finals organizers explicitly select advancing teams from that directory. The user confirmed that finals judges must only see this saved finalist roster.
- Directory additions become available as candidates; they do not silently enter finals. Existing eligibility checks and roster locks remain in force.
- Judge pages poll every two seconds for their saved stage roster and scoring-window state. Clean pages refresh team numbers, names, eligibility and selection. Unsaved or in-flight scores and nominations are preserved, with a visible notice when reloading is needed.
- Current-run IDs, edit revisions and login ownership guard writes. A stale request must not update another round or overwrite concurrent organizer work.
- Closed/frozen rosters and existing records cannot be cleared by directory loading. No score formula, nomination rule or award order changes.
- Finals restores the organizer-approved direct seat selection. The later shared-invitation gate reused the administrator secret and is removed; seat selection never creates an administrator session. Historical token rows remain inert.

## Implementation and verification

The roadshow admin response adds a minimal directory and roster selection. A dedicated team-only write delegates to the existing roster validation while preserving the current judge grants. Finals retains its existing explicit roster API. Responses expose team identity and eligibility, without participant contacts or unsubmitted materials.

Tests cover directory additions, unchanged grants, finalist-only access, stale revisions and rounds, ineligible teams, frozen rosters, unauthorized requests and automatic selector refresh with unsaved-input protection. Production setup loads only eligible roadshow teams, retains the requested real-name judge, leaves finalists for the organizer to select and keeps both scoring windows closed. Functional score tests use disposable data.

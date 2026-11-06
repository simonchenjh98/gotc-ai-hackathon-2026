# Participant entry and final schedule implementation plan

**Goal:** Publish the organizer's final November 7–8 schedule, enforce the November 8 13:00 Asia/Shanghai submission deadline, and make the participant path easier to follow.

**Architecture:** Keep the existing static site and authenticated platform. Improve labels, layout, and navigation around the current claim/team/score routes. Preserve authentication, scoring, submission state, recovery, and gateway behavior. The maintenance chat owns bug fixes; integrate its latest main before releasing.

**Tech stack:** Static HTML/CSS/JavaScript, Python service, SQLite settings with revision checks, GitHub Pages and the existing cloud deployment.

## Approved scope

The user requested direct implementation and supplied the authoritative schedule poster. No extra design approval is needed. Use a small additive participant section and clear shortcuts rather than a new application or rewritten submission flow.

1. Update `index.html` with the complete poster schedule, visible cutoff, primary participant CTA, a three-step guide, and current update date.
2. Update `styles.css` for the participant section, deadline callout, responsive layout and print.
3. Update `downloads/gotc-hackathon.ics`, `downloads/submission-checklist.txt`, and the source participant guide for consistent times and entry labels. Preserve calendar event UIDs and add an explicit deadline event.
4. Simplify `platform.html` navigation and `platform.js` team presentation; provide direct buttons to AI tools, repository, material form and receipt. Do not change form submission or identity logic. Style through `platform.css`.
5. Configure only `submissionDeadline` through the revision-protected admin API, preserving the open/closed state and all other settings. Read it back and log out.
6. Validate static links, JavaScript, existing frontend/backend/deployment checks and focused new navigation behavior. Check desktop/mobile layouts where browser tooling is available.
7. Commit and release via the existing repository and deployment workflow, coordinating with maintenance changes; verify live pages and deadline after release.

## Schedule source

Saturday: 08:00–08:30 sign-in; 08:45–10:00 opening ceremony (亦馨剧场); 10:00–12:00 team development (篮球馆); 12:00–13:00 lunch break; 13:00–17:00 second development session; 15:30–16:30 concurrent open-source lecture (图书馆); 17:00–18:30 evening break; 18:30–21:00 evening development; depart 21:00.

Sunday: venue opens for development at 08:00; 09:30–10:30 concurrent open-source experience sharing (图书馆); 12:30–13:00 project submission, cutoff 13:00; 13:00–14:00 research/practice sharing of four ongoing projects (图书馆); 14:00–15:30 roadshow; 16:00–17:00 final showcase; 17:15–17:45 awards; 17:45–18:00 group photo and certificates; depart/end 18:00. Do not infer unprovided Sunday lunch times, advancement formulas, or room assignments.

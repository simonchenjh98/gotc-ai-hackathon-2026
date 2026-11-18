# Named organizer accounts

Organizer login uses an individual username, a generated access code and a six-digit authenticator code. Usernames are case-insensitive ASCII identifiers. Organizers can enter or leave a team's workbench from an authenticated organizer session. The participant login accepts team credentials only.

## Migrate an existing event

Until the first named account is activated, the existing organizer credential works on the organizer portal. Use the service's installed Python environment and private event directory. Prepare and verify a recoverable private event backup before rollout. The following commands operate locally on the specified files.

1. Run `python -m backend.gotc.admin_auth prepare --username organizer.example --name "Example Organizer" --out /private/path/enrollment.json`. The tool exclusively creates a mode-0600 file with the username, display name, access code and Base32 authenticator seed. Transfer the code to the organizer's password manager and enroll the seed in their authenticator using SHA-1, six digits and a 30-second interval.
2. Run `python -m backend.gotc.admin_auth activate --data-dir /private/event --enrollment /private/path/enrollment.json` and enter the current authenticator code at the hidden prompt. A failed verification leaves the current organizer accounts and sessions intact. Successful verification activates the named account, disables all legacy organizer accounts and revokes their sessions, including delegated team sessions, in one transaction. The code used for enrollment is consumed. Wait for a new authenticator code before the first portal login.
3. Sign in on the organizer portal with the username, access code and new authenticator code. After verifying access, retire the temporary enrollment file and any obsolete shared organizer credential through the organization's private credential-management procedure. Keep any shared judge configuration that still serves an event requirement.

Repeat the two commands for each additional organizer. Previously enrolled organizers keep their own credentials. A duplicate username is rejected. Keep an independent named recovery organizer or retain protected shell access to provision a replacement. If an organizer's authenticator is lost, provision a distinct replacement locally and revoke the old account and its sessions with `python -m backend.gotc.admin_auth revoke --data-dir /private/event --username old.organizer`. The tool refuses to revoke the last active named organizer. Treat the event database and its backups as credentials because they contain each account's TOTP seed and the last accepted time step. Restoring an earlier database snapshot can roll back that replay state. The application verifies a one-step clock tolerance in either direction and consumes each accepted step once.

## Rollback

If enrollment verification fails before activation commits, fix the authenticator time or enrollment details and retry with the existing organizer portal still available. After activation, use another named organizer or local provisioning for recovery. An older application cannot enforce the second factor. Rehearse a code downgrade offline against the paired pre-migration backup first. Restoring that backup also restores its old account and session state and loses changes made since the snapshot. Restrict access during recovery and invalidate restored organizer sessions before reopening.

The time-based code follows RFC 6238. [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238)

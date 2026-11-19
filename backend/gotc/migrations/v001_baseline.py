"""Accept known GOTC schemas after their historical upgrades; retain operator tables."""
# This is a snapshot of the unnumbered release contract, independent of the
# current model definitions. Later migrations may add columns and operator
# installations may carry additional objects.
COLUMNS = {'accounts': 'id role name salt code_hash active created_at',
 'admin_data_meta': 'key value',
 'admin_team_sessions': 'token_hash team_id',
 'api_async_tasks': 'id team_id key_id request_id model path remote_id remote_scope upstream_url '
                    'upstream_key pricing_snapshot request_snapshot quantities_snapshot payload '
                    'response_headers state settled usage_id created_at updated_at next_poll_at attempts '
                    'last_error billing',
 'api_keys': 'id team_id name key_hash masked active created_at last_used_at',
 'api_logs': 'usage_id headers request response request_bytes response_bytes',
 'api_usage': 'id key_id team_id at model input_tokens output_tokens cache_write_tokens cache_read_tokens '
              'cost_micro status path stream duration_ms user_agent incomplete estimated error_type '
              'error_phase error_owner upstream_status error_message cache_write_1h_tokens requested_model '
              'first_token_ms client_ip upstream_request_id request_id currency billing_status '
              'billing_details',
 'audit': 'id at actor_id role action target metadata',
 'claim_auto_config': 'id revision enabled source_name source_sha256',
 'claim_auto_people': 'id name school normalized_name normalized_school eligible notes',
 'claim_declarations': 'request_id request_revision declaration_json',
 'claim_memberships': 'person_id team_id request_id',
 'claim_people': 'id name school normalized_name normalized_school notes needs_review revision',
 'claim_requests': 'id secret_hash pin_hash name school normalized_name normalized_school created_at '
                   'expires_at status revision pin_attempts team_id captain_id account_id '
                   'account_fingerprint approval_digest review_note issued_at',
 'credential_overrides': 'account_id rotated_at',
 'finals_access_tokens': 'token_hash code_tag expires_at',
 'finals_config': 'id run_id revision open roster_locked judges_created publication_json '
                  'publication_fingerprint published_at awards_json awards_fingerprint eligibility_revision',
 'finals_judges': 'run_id judge_id',
 'finals_nominations': 'run_id judge_id revision status nominations_json updated_at',
 'finals_scores': 'run_id judge_id team_id revision status scores_json total_tenths feedback updated_at',
 'finals_teams': 'run_id team_id',
 'gateway_config': 'id data updated_at',
 'import_previews': 'id account_id session_hash kind plan_json created_at expires_at consumed',
 'judging_grants': 'run_id judge_id',
 'judging_review_history': 'id run_id judge_id team_id version at actor_id action before_json after_json '
                           'reason',
 'judging_review_validity': 'run_id judge_id team_id invalidated reason updated_at',
 'judging_reviews': 'run_id judge_id team_id version draft_feedback submitted_feedback submitted_at recused '
                    'updated_at',
 'judging_roster': 'run_id team_id snapshot_json',
 'judging_runs': 'id stage created_at frozen_at reason',
 'judging_stages': 'stage run_id revision open deadline',
 'material_uploads': 'id team_id kind project_name filename size sha256 created_at',
 'repository_backup_migrations': 'name',
 'repository_backups': 'team_id version request_id status sha attempts next_at error artifact_id '
                       'manifest_json',
 'repository_bindings': 'team_id provider organization slug branch usernames_json state operation_id '
                        'external_id revision protection message updated_at',
 'repository_enterprise_invites': 'organization username state',
 'repository_preview_seals': 'preview_id seal_id',
 'repository_previews': 'id team_id session_hash binding_revision sha expires_at',
 'repository_seal_namespaces': 'prefix team_id external_id organization slug prefix_rule_id',
 'repository_seals': 'id prefix binding_revision sha exact_rule_id sealed_at',
 'repository_submission_seals': 'team_id version request_id seal_id received_at',
 'roadshow_access_config': 'id revision key_digest',
 'roadshow_access_roster': 'judge_id normalized_name enabled nonce credential_fingerprint claimed_at '
                           'created_at',
 'roadshow_scores': 'run_id judge_id team_id revision status scores_json total_tenths feedback updated_at',
 'roadshow_scoring_config': 'id revision publication_json publication_run_id publication_revision '
                            'published_at',
 'score_feedback_visibility': 'judge_id team_id revision',
 'score_write_revisions': 'judge_id team_id revision',
 'scores': 'judge_id team_id scores_json total_tenths feedback status updated_at',
 'sessions': 'token_hash account_id expires_at',
 'settings': 'key value',
 'settings_write_revisions': 'group_id revision',
 'submission_history': 'team_id submission_version materials_json submitted_at request_id origin',
 'submission_material_checks': 'team_id submission_version field url status revision checked_at actor_id '
                               'actor_name note',
 'submission_records_meta': 'key value',
 'submission_requests': 'team_id request_id payload_sha256 action base_version outcome receipt_json '
                        'http_status created_at',
 'submissions': 'team_id version draft_json submitted_json submitted_version first_submitted_at submitted_at '
                'updated_at',
 'team_accounts': 'account_id team_id',
 'team_api': 'team_id active created_at',
 'team_details': 'team_id status status_reason booth roadshow_order revision',
 'team_profile_visibility': 'team_id directory_visible',
 'team_recycle_events': 'id team_id deleted_at deleted_by delete_reason original_status '
                        'original_status_reason team_snapshot_json roster_snapshot_json restored_at '
                        'restored_by restore_reason restored_name',
 'team_recycle_memberships': 'event_id person_id request_id name school normalized_name normalized_school',
 'team_registrations': 'team_id source_key captain school phone target_size members_json summary secret_hash '
                       'revision flags_json raw_json created_at',
 'teams': 'id number name project track updated_at',
 'votes': 'account_id team_id created_at',
 'write_conflicts_meta': 'key value'}
TABLES = frozenset(COLUMNS)


TRIGGERS = frozenset("""
finals_judge_active_update finals_judge_name_update finals_team_metadata_update
finals_team_status_delete finals_team_status_insert finals_team_status_update
material_uploads_no_delete material_uploads_no_update registration_team_changed
repository_seal_namespace_identity repository_seals_no_rewrite repository_submission_seals_no_delete
repository_submission_seals_no_update roadshow_access_stage_revision roadshow_scoring_judge_metadata
roadshow_scoring_judging_grants_delete roadshow_scoring_judging_grants_insert
roadshow_scoring_judging_grants_update roadshow_scoring_judging_roster_delete
roadshow_scoring_judging_roster_insert roadshow_scoring_judging_roster_update
roadshow_scoring_roadshow_scores_delete roadshow_scoring_roadshow_scores_insert
roadshow_scoring_roadshow_scores_update roadshow_scoring_stage roadshow_scoring_team_metadata
roadshow_scoring_team_status_delete roadshow_scoring_team_status_insert
roadshow_scoring_team_status_update score_feedback_after_insert score_feedback_after_update
score_feedback_before_delete submission_history_no_delete submission_history_no_update
submission_requests_no_delete submission_requests_no_update
""".split())


def validate_source(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    anchors = {"accounts": {"id", "role", "salt", "code_hash", "active"},
               "sessions": {"token_hash", "account_id", "expires_at"}, "teams": {"id", "number", "name"},
               "settings": {"key", "value"}}
    if tables and any(name not in tables or not required <= {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
                      for name, required in anchors.items()):
        raise RuntimeError("Unrecognized event database; restore or select an existing GOTC data directory")


def verify(db, *, check_foreign_keys=True):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    missing = TABLES.difference(tables)
    if missing:
        raise RuntimeError("Event baseline is missing required tables: " + ", ".join(sorted(missing)))
    for table, required in COLUMNS.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        missing_columns = set(required.split()) - columns
        if missing_columns:
            fields = ", ".join(sorted(missing_columns))
            raise RuntimeError(f"Event baseline is missing required columns in {table}: {fields}")
    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
    if TRIGGERS - triggers:
        raise RuntimeError("Event baseline is missing required rollback or integrity triggers")
    if check_foreign_keys and db.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Event baseline contains a foreign-key violation")

# 06 - Optimistic Quota Rolling Instance Creation Strategy

Status: **SPEC**

Governs: multi-VPC rolling VM health checks, optimistic quota apply, stop-on-quota behavior, instance lifecycle validation, and rolling-lifecycle report events.

## 1. Goal

Validate VM creation while using optimistic quota apply only:

- Do not perform Compute Instance quota prechecks.
- Assume quota is sufficient before apply.
- Proceed directly to Terraform plan/apply after non-quota validation.
- If the provider returns quota exceeded, stop immediately and wait for explicit user instruction.
- Do not auto-reclaim, auto-delete, auto-recover, auto-recreate, auto-retry, or continue to the next image after quota exceeded.

## 2. Platform Reality

The `fpt-corp/fptcloud` provider exposes no reliable compute, VPC, or volume storage quota read surface for this workflow. The backend enforces VPC storage quota at instance create time.

Binding quota policy:

- `quota_precheck=disabled`
- `quota_assumption=assume_sufficient`
- `quota_exceeded_action=stop_and_wait_for_user`
- Provider apply is the authoritative quota check.
- No quota value is guessed or used to block apply.

## 3. Rolling Execution

For each selected VPC/image:

1. Resolve VPC, subnet, storage policy, image, flavor, password, hostname, and network inputs.
2. Record the optimistic quota assumption fields.
3. Create exactly one VM per Terraform apply.
4. On success, retain the created resource and continue normally.
5. On quota exceeded, retain created/failed resources, stop the run, and report the blocked state.

Required quota-block fields:

- `classification=instance_storage_quota_exceeded` or `classification=instance_quota_exceeded`
- `stop_on_quota_exceeded=True`
- `run_status=blocked_waiting_user_confirmation`
- `remaining_images_not_attempted=<list>`
- `user_action_required=True`

## 4. Cleanup And Recovery

Automatic cleanup and recovery are disabled after quota exceeded:

- Do not delete anything automatically.
- Do not run `compute.reclaim-health-check-instance`.
- Do not run `compute.recover-instance-quota`.
- Do not run `compute.recreate-instance`.
- Keep successfully created resources unless the user explicitly confirms cleanup.
- Keep failed resources/state for inspection unless the user explicitly confirms cleanup.

Manual cleanup or recovery, if implemented later, must require explicit user confirmation and must keep the existing tag/name proof gates for any destructive action.

## 5. Logging

`run-log.html` and `log.html` must clearly show:

- `quota_precheck=disabled`
- `quota_assumption=assume_sufficient`
- `quota_exceeded_action=stop_and_wait_for_user`
- `user_action_required=True` when quota exceeded
- `run_status=blocked_waiting_user_confirmation` when quota exceeded
- `remaining_images_not_attempted=<list>` when quota exceeded

The runner must not emit automatic `instance.deleted`, `quota.recovered`, or `instance.recreated` events after quota exceeded.

## 6. Validation

Required tests:

- A normal run reaches `compute.create-instance` without running `compute.inspect-instance-quota` or `compute.validate-instance-quota`.
- Quota apply failure is classified as `instance_storage_quota_exceeded` when provider text indicates VPC storage quota exhaustion.
- After quota exceeded, no later image is attempted.
- After quota exceeded, no reclaim, recover, recreate, retry, fallback, or cleanup function is called automatically.
- Reports include all optimistic quota and user-action-required fields.

## 7. Traceability

| Req | Summary |
|---|---|
| FR-014 | Multi-VPC rolling VM validation. |
| FR-015 | One-at-a-time create and post-create validation. |
| FR-016 | Optimistic quota apply and stop-on-quota waiting for user confirmation. |
| FR-017 | Tag/name scoped deletion safety for any future user-confirmed destructive action. |
| FR-018 | Permanently-unavailable image skip. |
| FR-019 | Rolling-lifecycle report events with optimistic quota fields. |
| NFR-012 | Zero automatic deletion/recovery/retry after quota exceeded. |
| C-012 | No quota API; quota precheck disabled. |
| C-013 | Any future deletion requires explicit user confirmation and Terraform-backed safety. |
| C-014 | Rolling tunables live in `constants.ROLLING_INSTANCE_STRATEGY`. |

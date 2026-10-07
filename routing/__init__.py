"""Routing: capability registry, best-execution scoring, route preview/execute, deployments, execution safety.

    capabilities  what each provider's API allows vs what OpenGrid implements
    scoring       rank_listings(): the transparent weighted best-execution model
    engine        preview() / route() / approve() / reject() / create_validation_route()
    control       execution control plane: mode, env ceiling, provider flags, kill switches
    quotes        persisted, expiring quotes; re-validation before launch
    guards        per-account cost guards
    idempotency   Idempotency-Key replay store
    adapters/     one execution adapter per provider API (results.py: the result types)
    deployments   the deployment state machine, the single provision call, stop / terminate, observation
    credentials   launch-time credential resolution and the credential pinned to each deployment
    audit         route_requests + routing_decisions
    transactions  execution records and the billing usage hook
    tracker       background job polling live deployments

Contract for other modules (stable names; methodology/execution-safety.md):

    control.effective_mode() -> 'DISABLED'|'PREVIEW_ONLY'|'SUPERVISED'|'LIVE'   (env ceiling applied)
    control.provider_flags(provider) -> dict   adapter_status, supervised_enabled, live_enabled, killed, ...
    control.launch_permission(provider, *, purpose='customer'|'validation') -> (allowed, mode_used, reason)
    control.mark_validated(provider, deployment_id, evidence: dict, by: str) -> dict
        call ONLY after a complete validation cycle (launched -> observed running -> termination confirmed
        by the provider -> cost reconciled). It does not enable launches.

    deployments.transition(dep, to, reason=None, evidence=None, *, actor='system'|'reconciler'|..., actor_id=None,
                           s=None) -> bool
        dep: a Deployment row with `s` (your session, row locked) or a deployment id (own transaction).
        Raises deployments.IllegalTransition. Writes a deployment_events row. Moving to 'terminated'
        syncs the execution record and triggers billing.
    deployments.ALLOWED_TRANSITIONS, LIVE_STATES, TERMINAL_STATES, UNCERTAIN_STATES, PRE_LAUNCH_STATES, ACTIVE_STATES
    deployments.credentials_for(deployment) -> dict    the PINNED credentials, or raises
        routing.credentials.CredentialsUnavailable (never another key)
    deployments.adapter_for(deployment) -> Adapter      built with the pinned credentials
    deployments.mark_credentials_unavailable(dep_id, exc)
    deployments.observe(dep_id, InstanceState, checked_at=, actor='reconciler', extra_evidence=) -> dict
        apply a provider read through the state machine (stale / forbidden transitions recorded, ignored;
        a single not_found never terminates)
    deployments.refresh(dep_id) / live_ids()            used by routing/tracker.py

    quotes.get(quote_id, who=None) -> dict | None
    guards.check(account_id, *, provider, gpu_count, price_per_gpu_hour, est_total_cost, region=None,
                 region_group=None, purpose='customer', max_runtime_minutes=None) -> list[violation]

    engine.create_validation_route(provider, listing_id_or_None, *, by) -> route_request_id
        a purpose='validation' route, pending admin approval, validation caps; approved via
        engine.approve(route_request_id, who, quote_id=...) / POST /v1/route/{id}/approve.

Adapter hooks the core relies on: Adapter.CREDENTIAL_PROVIDER (credentials resolved for that provider's
API), Adapter.SSH_KEY_REGISTRATION (may register launch.ssh_public_key under og-<deployment_id>),
Adapter.CAPABILITIES.billing_unit / minimum_commitment / stopped_billing (quotes, stop), adapter.log_context.
Provider calls are logged on logger "opengrid.provider" with extra={provider, op, status, latency_ms,
deployment_id, route_request_id}; secrets never.
"""

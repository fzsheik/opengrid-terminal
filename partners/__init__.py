"""Design-partner mode: partner profiles, per-deployment feedback, and the onboarding state machine.

    partners.profiles     partner_profiles rows; operator one-step onboarding (account + profile + key)
    partners.feedback     deployment_feedback: one editable verdict per deployment
    partners.onboarding   which onboarding steps an account has done, computed from the system

Tables live in store/metrics.py; endpoints in api/partners.py.
"""

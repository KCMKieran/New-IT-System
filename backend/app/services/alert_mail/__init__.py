"""Alert Mail Center (OPT-0043) service package.

- registry:  MAIL_SOURCES — the source registry mapping alert modules
  (rule_id bands in alert_events) onto loaders/evaluators/templates.
- service:   business logic behind /api/v1/alert-mail (CRUD, outbox,
  test-send, resend); the dispatcher itself stays in
  app/services/alert_mail_dispatcher.py (v1 module, generalized in v2).
"""

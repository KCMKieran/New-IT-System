"""Identity-provider adapters for the auth layer (auth design P3).

Everything under ``providers/`` answers exactly one question — "which person is
behind this browser?" — and then hands the answer to ``auth_service.login()``,
which owns sessions, provisioning and audit. Keeping that seam means a second
provider (email OTP, P6) is a new file rather than a change to the session layer.
"""

"""Identity and access (Phase 5A, D68–D70): who someone is, and what Gaia lets them do.

* ``passwords`` — the one local account's password (argon2id), tokens and codes;
* ``audit`` — the hash-chained ``audit.auth_event`` log;
* ``privileges`` — from grants to what a person may do, with separation of duties;
* ``ldap`` — Active Directory / LDAP(S): sign-in, roles, sync, the connection probe;
* ``service`` — sign-in, sessions, grants, directories, the local account;
* ``deps`` — FastAPI dependencies: the signed-in principal, role checks, CSRF.
"""
